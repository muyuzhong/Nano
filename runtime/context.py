"""真实 token 记账、Provider 重试策略与上下文组装压缩。"""
from __future__ import annotations

import asyncio
import json

from providers.base import ModelRequest, RETRYABLE_ERRORS, RateLimitError
from runtime.blocks import Message, TextBlock, ToolResultBlock, Usage, estimate_tokens

TRUNCATE_LIMIT = 200


class TokenLedger:
    """任务级预算使用真实 Usage 记账，达到任一上限即停止。"""
    def __init__(self, max_total_tokens=1_000_000, max_api_calls=100):
        self.max_total_tokens, self.max_api_calls = max_total_tokens, max_api_calls
        self.input_tokens = self.output_tokens = self.api_calls = 0
    def record(self, usage: Usage):
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.api_calls += 1
    @property
    def total_tokens(self): return self.input_tokens + self.output_tokens
    def budget_ok(self): return self.total_tokens < self.max_total_tokens and self.api_calls < self.max_api_calls


class RetryPolicy:
    """只重试归一化后的暂时性错误，并优先尊重服务端 Retry-After。"""
    def __init__(self, max_retries=3, initial_backoff=1.0, max_backoff=30.0, base=2.0, sleep=None):
        self.max_retries, self.initial_backoff, self.max_backoff, self.base = max_retries, initial_backoff, max_backoff, base
        self.sleep = sleep or asyncio.sleep
    def should_retry(self, error, attempt): return attempt <= self.max_retries and isinstance(error, RETRYABLE_ERRORS)
    def backoff_for(self, error, attempt):
        if isinstance(error, RateLimitError): return float(error.retry_after)
        return min(self.initial_backoff * self.base ** (attempt - 1), self.max_backoff)


def _truncate(messages, keep_recent):
    """优先截断较旧的大工具结果，因为它们通常体积最大且价值衰减最快。"""
    cutoff, output = max(0, len(messages) - keep_recent), []
    for index, message in enumerate(messages):
        blocks = []
        for block in message.content:
            if index < cutoff and isinstance(block, ToolResultBlock) and len(block.content) > TRUNCATE_LIMIT:
                block = ToolResultBlock(block.tool_use_id, f"[结果已截断，原长 {len(block.content)} 字符]", block.is_error, block.error_type)
            blocks.append(block)
        output.append(Message(message.role, blocks, message.message_id, message.timestamp, message.usage))
    return output


def _plain_user(message): return message.role == "user" and all(isinstance(block, TextBlock) for block in message.content)


def _snip(messages, keep_recent):
    """裁剪切口落在普通用户消息，避免留下孤立 tool_result。"""
    if len(messages) <= keep_recent + 2: return messages
    start = len(messages) - keep_recent
    while start < len(messages) and not _plain_user(messages[start]): start += 1
    if start >= len(messages):
        start = len(messages) - keep_recent
        # 找不到普通用户消息时，至少继续越过 tool_result，避免保留缺少
        # 对应 tool_use 的孤儿结果，破坏 Provider 历史协议。
        while start < len(messages) and any(isinstance(block, ToolResultBlock) for block in messages[start].content):
            start += 1
    summary = Message.user(f"[历史已压缩] 此处省略了 {start - 1} 条早期消息。")
    return [messages[0], summary] + messages[start:]


class ContextAssembler:
    """组装请求；超过阈值时依次截断工具结果、裁剪早期历史。"""
    def __init__(self, system_prompt, registry=None, model="mock-model", max_tokens=4096,
                 context_window=200_000, compact_threshold=.8, keep_recent=8):
        self.system_prompt, self.registry, self.model, self.max_tokens = system_prompt, registry, model, max_tokens
        self.context_window, self.compact_threshold, self.keep_recent = context_window, compact_threshold, keep_recent
    def _estimate(self, messages, tools):
        return sum(estimate_tokens(message) for message in messages) + len(json.dumps(tools, ensure_ascii=False)) // 4 + len(self.system_prompt) // 4
    def build(self, state):
        tools = self.registry.schemas() if self.registry else []
        messages, before = state.messages, self._estimate(state.messages, tools)
        info = None
        if before > self.context_window * self.compact_threshold:
            messages = _truncate(messages, self.keep_recent)
            if self._estimate(messages, tools) > self.context_window * self.compact_threshold:
                messages = _snip(messages, self.keep_recent)
            state.messages = messages
            info = (before, self._estimate(messages, tools))
        return ModelRequest(self.system_prompt, list(messages), tools, self.model, self.max_tokens), info
