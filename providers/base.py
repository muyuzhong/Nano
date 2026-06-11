"""Provider 接口、标准化流事件与归一化异常。

核心引擎只消费这里定义的事件和异常。各厂商的 SSE 字段、错误码和消息格式必须
在适配器边界内完成翻译，不能泄漏到运行时循环。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Union

from runtime.blocks import Message, Usage, estimate_tokens


@dataclass
class MessageStart:
    model: str


@dataclass
class TextDelta:
    text: str


@dataclass
class ThinkingDelta:
    thinking: str


@dataclass
class ToolUseStart:
    id: str
    name: str


@dataclass
class ToolInputDelta:
    # 显式携带 id，才能正确处理多个工具调用交错返回参数增量的情况。
    id: str
    partial_json: str


@dataclass
class ToolUseEnd:
    id: str


@dataclass
class MessageEnd:
    # 适配器应归一化为 end_turn、tool_use 或 max_tokens。
    stop_reason: str
    usage: Usage


StreamEvent = Union[
    MessageStart,
    TextDelta,
    ThinkingDelta,
    ToolUseStart,
    ToolInputDelta,
    ToolUseEnd,
    MessageEnd,
]


class ProviderError(Exception):
    """所有 Provider 异常的基类。"""


class RateLimitError(ProviderError):
    def __init__(self, retry_after: float = 30.0, message: str = "rate limited"):
        self.retry_after = retry_after
        super().__init__(message)


class ProviderTimeoutError(ProviderError):
    pass


class ProviderServerError(ProviderError):
    pass


class ProviderAuthError(ProviderError):
    """401/403 认证错误，不可重试。"""


class ProviderBadRequestError(ProviderError):
    """400 请求错误，不可重试。"""


# 重试策略只依赖归一化异常，不需要知道异常来自哪一家 Provider。
RETRYABLE_ERRORS = (RateLimitError, ProviderTimeoutError, ProviderServerError)


@dataclass
class ModelRequest:
    """运行时发送给 Provider 的完整单次请求。"""

    system: str
    messages: List[Message]
    tools: List[Dict]
    model: str
    max_tokens: int = 4096


class ModelProvider(ABC):
    """所有模型适配器必须实现的最小流式接口。"""

    @abstractmethod
    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """逐个产出已经标准化的流事件。"""

    def count_tokens_estimate(self, messages: List[Message]) -> int:
        """在请求发送前提供保守预算估算。"""
        return sum(estimate_tokens(message) for message in messages)
