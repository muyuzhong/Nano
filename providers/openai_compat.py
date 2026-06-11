"""OpenAI 兼容端点适配器：转换 tool_calls/tool 消息并翻译 chunk 流。"""
from __future__ import annotations
import json, os
import httpx
from providers.base import *
from runtime.blocks import ToolResultBlock, Usage

STOP_MAP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


def _map_status(status, headers, body):
    """把 OpenAI 兼容端点的 HTTP 状态归一化为运行时异常。"""
    if status == 429: return RateLimitError(float(headers.get("retry-after", 30)), body[:200])
    if status in (401, 403): return ProviderAuthError(body[:200])
    if status == 400: return ProviderBadRequestError(body[:200])
    return ProviderServerError(f"HTTP {status}: {body[:200]}")


class OpenAICompatProvider(ModelProvider):
    def __init__(self, api_key=None, base_url="https://api.deepseek.com", timeout=60.0, transport=None):
        self.api_key, self.base_url, self.timeout, self.transport = api_key or os.getenv("OPENAI_API_KEY", ""), base_url.rstrip("/"), timeout, transport

    def _serialize(self, system, messages):
        output = [{"role": "system", "content": system}] if system else []
        for message in messages:
            if message.role == "user":
                for block in message.content:
                    if isinstance(block, ToolResultBlock): output.append({"role": "tool", "tool_call_id": block.tool_use_id, "content": block.content})
                if message.get_text(): output.append({"role": "user", "content": message.get_text()})
            else:
                entry = {"role": "assistant", "content": message.get_text() or None}
                if message.get_tool_calls():
                    entry["tool_calls"] = [{"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.input, ensure_ascii=False)}} for call in message.get_tool_calls()]
                output.append(entry)
        return output

    async def stream(self, request):
        payload = {"model": request.model, "messages": self._serialize(request.system, request.messages), "max_tokens": request.max_tokens,
                   "stream": True, "stream_options": {"include_usage": True}}
        if request.tools: payload["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in request.tools]
        open_tools, order, finish, usage, started = {}, [], None, Usage(), False
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                async with client.stream("POST", f"{self.base_url}/chat/completions", json=payload, headers={"Authorization": f"Bearer {self.api_key}"}) as response:
                    if response.status_code != 200:
                        body = (await response.aread()).decode(errors="replace")
                        raise _map_status(response.status_code, response.headers, body)
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"): continue
                        raw = line[5:].strip()
                        if raw == "[DONE]": break
                        chunk = json.loads(raw)
                        if chunk.get("usage"): usage = Usage(chunk["usage"].get("prompt_tokens", 0), chunk["usage"].get("completion_tokens", 0))
                        if not chunk.get("choices"): continue
                        if not started: yield MessageStart(chunk.get("model", request.model)); started = True
                        choice = chunk["choices"][0]; delta = choice.get("delta") or {}
                        if delta.get("reasoning_content"): yield ThinkingDelta(delta["reasoning_content"])
                        if delta.get("content"): yield TextDelta(delta["content"])
                        for call in delta.get("tool_calls") or []:
                            index = call.get("index", 0)
                            if index not in open_tools:
                                tool_id = call.get("id") or f"call_{index}"; open_tools[index] = tool_id; order.append(tool_id)
                                yield ToolUseStart(tool_id, (call.get("function") or {}).get("name", ""))
                            arguments = (call.get("function") or {}).get("arguments")
                            if arguments: yield ToolInputDelta(open_tools[index], arguments)
                        finish = choice.get("finish_reason") or finish
        except httpx.TimeoutException as error: raise ProviderTimeoutError(str(error)) from error
        for tool_id in order: yield ToolUseEnd(tool_id)
        yield MessageEnd(STOP_MAP.get(finish, "end_turn"), usage)
