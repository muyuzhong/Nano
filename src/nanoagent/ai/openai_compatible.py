"""OpenAI-compatible chat completions / responses provider adapter。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from json import JSONDecodeError, dumps, loads
from typing import Any, Protocol

import httpx

from nanoagent.agent.messages import AgentMessage, AssistantMessage, ToolResultMessage, UserMessage
from nanoagent.agent.tools import AgentTool, ToolCall
from nanoagent.agent.types import JSONValue
from nanoagent.ai.env import OpenAICompatibleConfig
from nanoagent.ai.events import (
    ProviderErrorEvent,
    ProviderEvent,
    ProviderResponseEndEvent,
    ProviderResponseStartEvent,
    ProviderTextDeltaEvent,
    ProviderThinkingDeltaEvent,
    ProviderToolCallEvent,
)
from nanoagent.ai.provider import CancellationToken
from nanoagent.ai.retry import provider_retry_event, retry_delay_seconds, wait_for_retry

_RESPONSES_ONLY_PREFIXES: tuple[str, ...] = ("gpt-5.5", "gpt-5.4")


def _use_responses_api(model: str) -> bool:
    """判断模型是否必须走 Responses API。"""
    normalized = model.strip().lower()
    if "codex" in normalized:
        return True
    return any(normalized.startswith(prefix) for prefix in _RESPONSES_ONLY_PREFIXES)


class OpenAICompatibleProvider:
    """OpenAI-compatible provider adapter。

    默认走 ``/chat/completions``；对已知需要 Responses API 的模型，自动切换到
    ``/responses``，上层仍只看到统一的 ProviderEvent 流。
    """

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        """关闭由本 provider 创建的 HTTP client。"""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """把一次模型响应流式转换为 provider-neutral 事件。"""
        if _use_responses_api(model):
            return self._stream_responses(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                signal=signal,
            )
        return self._stream_chat_completions(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            signal=signal,
        )

    def _stream_chat_completions(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """用 chat-completions endpoint 产生统一事件流。"""
        payload = _build_chat_payload(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            reasoning_effort=self._config.reasoning_effort,
            reasoning_effort_parameter=self._config.reasoning_effort_parameter,
        )
        return self._stream(
            model=model,
            url=f"{self._config.base_url.rstrip('/')}/chat/completions",
            payload=payload,
            parser_factory=_ChatStreamParser,
            signal=signal,
        )

    def _stream_responses(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """用 Responses API endpoint 产生统一事件流。"""
        payload = _build_responses_payload(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            reasoning_effort=self._config.reasoning_effort,
        )
        return self._stream(
            model=model,
            url=f"{self._config.base_url.rstrip('/')}/responses",
            payload=payload,
            parser_factory=_ResponsesStreamParser,
            signal=signal,
        )

    def _stream(
        self,
        *,
        model: str,
        url: str,
        payload: Mapping[str, JSONValue],
        parser_factory: Callable[[], _StreamParser],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """共享 HTTP POST、SSE 读取、重试和取消处理。"""

        async def iterator() -> AsyncIterator[ProviderEvent]:
            client = self._get_client()
            headers = {
                **dict(self._config.headers or {}),
                "Authorization": f"Bearer {self._config.api_key}",
            }

            attempt = 0
            while True:
                parser = parser_factory()
                try:
                    async with client.stream("POST", url, json=payload, headers=headers) as response:
                        if response.status_code >= 400:
                            body = await response.aread()
                            if self._should_retry(attempt, status_code=response.status_code):
                                delay = retry_delay_seconds(
                                    attempt,
                                    max_delay_seconds=self._config.max_retry_delay_seconds,
                                )
                                yield provider_retry_event(
                                    attempt=attempt,
                                    max_retries=self._config.max_retries,
                                    delay_seconds=delay,
                                    reason=f"HTTP {response.status_code}",
                                    data={
                                        "status_code": response.status_code,
                                        "body": body.decode(errors="replace"),
                                    },
                                )
                                attempt += 1
                                if not await wait_for_retry(delay, signal=signal):
                                    return
                                continue
                            yield ProviderErrorEvent(
                                message=(
                                    f"Provider request failed with status {response.status_code}"
                                ),
                                data={
                                    "body": body.decode(errors="replace"),
                                    "attempts": attempt + 1,
                                },
                            )
                            return

                        yield ProviderResponseStartEvent(model=model)

                        async for line in response.aiter_lines():
                            if signal is not None and signal.is_cancelled():
                                return
                            event = _parse_sse_line(line)
                            if event is None:
                                continue

                            events, stop = parser.feed(event)
                            for parser_event in events:
                                yield parser_event
                            if stop:
                                break

                        if parser.fatal:
                            return
                        for parser_event in parser.finalize():
                            yield parser_event
                        return
                except httpx.HTTPError as exc:
                    if not parser.emitted_content and self._should_retry(attempt):
                        delay = retry_delay_seconds(
                            attempt,
                            max_delay_seconds=self._config.max_retry_delay_seconds,
                        )
                        yield provider_retry_event(
                            attempt=attempt,
                            max_retries=self._config.max_retries,
                            delay_seconds=delay,
                            reason="network error",
                            data={"error": str(exc), "error_type": type(exc).__name__},
                        )
                        attempt += 1
                        if not await wait_for_retry(delay, signal=signal):
                            return
                        continue
                    yield ProviderErrorEvent(message=str(exc), data={"attempts": attempt + 1})
                    return

        return iterator()

    def _get_client(self) -> httpx.AsyncClient:
        """懒加载 HTTP client，方便测试注入 MockTransport。"""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        return self._client

    def _should_retry(self, attempt: int, *, status_code: int | None = None) -> bool:
        """只对未超过上限的瞬时失败重试。"""
        if attempt >= self._config.max_retries:
            return False
        return status_code is None or _is_transient_status(status_code)


class _StreamParser(Protocol):
    """单个 endpoint 的 SSE 解析器接口。"""

    emitted_content: bool
    fatal: bool

    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        """消费一条 SSE data payload，并返回事件与是否停止。"""
        ...

    def finalize(self) -> list[ProviderEvent]:
        """返回尾部工具调用和 response_end 事件。"""
        ...


class _ChatStreamParser:
    """OpenAI ``/chat/completions`` SSE chunk parser。"""

    def __init__(self) -> None:
        self.emitted_content = False
        self.fatal = False
        self._content_parts: list[str] = []
        self._tool_call_builders: dict[int, _ToolCallBuilder] = {}
        self._finish_reason: str | None = None

    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        if event == "[DONE]":
            return [], True

        chunk = _loads_object(event)
        if chunk is None:
            self.fatal = True
            return [ProviderErrorEvent(message="Provider returned invalid JSON chunk")], True

        choice = _first_choice(chunk)
        if choice is None:
            return [], False

        self._finish_reason = choice.get("finish_reason") or self._finish_reason
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            return [], False

        events: list[ProviderEvent] = []
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.emitted_content = True
            self._content_parts.append(content)
            events.append(ProviderTextDeltaEvent(delta=content))

        thinking = _thinking_delta_text(delta)
        if thinking:
            self.emitted_content = True
            events.append(ProviderThinkingDeltaEvent(delta=thinking))

        for tool_call_delta in _tool_call_deltas(delta):
            self.emitted_content = True
            index = int(tool_call_delta.get("index", 0))
            builder = self._tool_call_builders.setdefault(index, _ToolCallBuilder())
            builder.add_delta(tool_call_delta)

        return events, False

    def finalize(self) -> list[ProviderEvent]:
        tool_calls = [
            builder.build(index) for index, builder in sorted(self._tool_call_builders.items())
        ]
        events: list[ProviderEvent] = [
            ProviderToolCallEvent(tool_call=tool_call) for tool_call in tool_calls
        ]
        events.append(
            ProviderResponseEndEvent(
                message=AssistantMessage(content="".join(self._content_parts), tool_calls=tool_calls),
                finish_reason=self._finish_reason,
            )
        )
        return events


class _ResponsesStreamParser:
    """OpenAI ``/responses`` SSE event parser。"""

    def __init__(self) -> None:
        self.emitted_content = False
        self.fatal = False
        self._content_parts: list[str] = []
        self._tool_call_builders: dict[str, _ResponsesToolCallBuilder] = {}
        self._status: str | None = None

    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        if event == "[DONE]":
            return [], False

        chunk = _loads_object(event)
        if chunk is None:
            return [], False

        chunk_type = chunk.get("type")
        if not isinstance(chunk_type, str):
            return [], False

        if chunk_type in ("response.output_text.delta", "response.refusal.delta"):
            delta = chunk.get("delta")
            if isinstance(delta, str) and delta:
                self.emitted_content = True
                self._content_parts.append(delta)
                return [ProviderTextDeltaEvent(delta=delta)], False
        elif chunk_type in (
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        ):
            delta = chunk.get("delta")
            if isinstance(delta, str) and delta:
                self.emitted_content = True
                return [ProviderThinkingDeltaEvent(delta=delta)], False
        elif chunk_type == "response.output_item.added":
            _register_responses_item(
                self._tool_call_builders,
                chunk.get("item"),
                output_index=chunk.get("output_index"),
            )
        elif chunk_type == "response.function_call_arguments.delta":
            item_id = chunk.get("item_id")
            if isinstance(item_id, str):
                builder = self._tool_call_builders.setdefault(item_id, _ResponsesToolCallBuilder())
                builder.add_arguments_delta(chunk.get("delta"))
                self.emitted_content = True
        elif chunk_type == "response.function_call_arguments.done":
            item_id = chunk.get("item_id")
            if isinstance(item_id, str):
                builder = self._tool_call_builders.setdefault(item_id, _ResponsesToolCallBuilder())
                builder.set_final(arguments=chunk.get("arguments"))
        elif chunk_type == "response.output_item.done":
            _finalize_responses_item(
                self._tool_call_builders,
                chunk.get("item"),
                output_index=chunk.get("output_index"),
            )
        elif chunk_type in ("response.completed", "response.incomplete"):
            self._status = _responses_finish_reason(chunk)
            return [], True
        elif chunk_type == "response.failed":
            self.fatal = True
            return [_responses_failure_event(chunk)], True
        elif chunk_type == "error":
            self.fatal = True
            return [ProviderErrorEvent(message=_responses_error_message(chunk), data={"event": chunk})], True

        return [], False

    def finalize(self) -> list[ProviderEvent]:
        tool_calls = [
            builder.build(index)
            for index, builder in enumerate(_ordered_builders(self._tool_call_builders))
        ]
        events: list[ProviderEvent] = [
            ProviderToolCallEvent(tool_call=tool_call) for tool_call in tool_calls
        ]
        events.append(
            ProviderResponseEndEvent(
                message=AssistantMessage(content="".join(self._content_parts), tool_calls=tool_calls),
                finish_reason=_normalize_finish_reason(self._status, has_tool_calls=bool(tool_calls)),
            )
        )
        return events


class _ToolCallBuilder:
    """累积 chat-completions 分片里的 function call。"""

    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments_parts: list[str] = []

    def add_delta(self, delta: Mapping[str, Any]) -> None:
        """合并一段 tool_call delta。"""
        call_id = delta.get("id")
        if isinstance(call_id, str):
            self.id = call_id

        function = delta.get("function")
        if not isinstance(function, Mapping):
            return

        name = function.get("name")
        if isinstance(name, str):
            self.name = name

        arguments = function.get("arguments")
        if isinstance(arguments, str):
            self.arguments_parts.append(arguments)

    def build(self, index: int) -> ToolCall:
        """把累积参数解析成 ToolCall，非法 JSON 保留原始文本。"""
        arguments_text = "".join(self.arguments_parts)
        arguments = _loads_object(arguments_text) if arguments_text else {}
        if arguments is None:
            arguments = {"_raw_arguments": arguments_text}
        return ToolCall(id=self.id or f"tool-call-{index}", name=self.name, arguments=arguments)


class _ResponsesToolCallBuilder:
    """累积 Responses API 的 ``function_call`` output item。"""

    def __init__(
        self,
        *,
        call_id: str = "",
        name: str = "",
        output_index: int = 0,
    ) -> None:
        self.call_id = call_id
        self.name = name
        self.output_index = output_index
        self.arguments_parts: list[str] = []
        self.arguments_final: str | None = None

    def add_arguments_delta(self, delta: object) -> None:
        """追加参数增量文本。"""
        if isinstance(delta, str):
            self.arguments_parts.append(delta)

    def set_final(
        self,
        *,
        call_id: str | None = None,
        name: str | None = None,
        arguments: object = None,
        output_index: int | None = None,
    ) -> None:
        """记录 output item 的最终字段。"""
        if call_id:
            self.call_id = call_id
        if name:
            self.name = name
        if isinstance(arguments, str):
            self.arguments_final = arguments
        if output_index is not None:
            self.output_index = output_index

    def build(self, index: int) -> ToolCall:
        """把 Responses API 累积结果转成 ToolCall。"""
        arguments_text = (
            self.arguments_final
            if self.arguments_final is not None
            else "".join(self.arguments_parts)
        )
        arguments = _loads_object(arguments_text) if arguments_text else {}
        if arguments is None:
            arguments = {"_raw_arguments": arguments_text}
        return ToolCall(id=self.call_id or f"tool-call-{index}", name=self.name, arguments=arguments)


def _build_chat_payload(
    *,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    reasoning_effort: str | None = None,
    reasoning_effort_parameter: str = "reasoning_effort",
) -> dict[str, JSONValue]:
    """构造 OpenAI chat-completions 请求体。"""
    payload: dict[str, JSONValue] = {
        "model": model,
        "stream": True,
        "messages": [_system_message(system), *[_message_to_openai(message) for message in messages]],
    }
    if reasoning_effort is not None:
        if reasoning_effort_parameter == "reasoning.effort":
            payload["reasoning"] = {"effort": reasoning_effort}
        else:
            payload["reasoning_effort"] = reasoning_effort
    if tools:
        payload["tools"] = [_tool_to_openai(tool) for tool in tools]
    return payload


def _build_responses_payload(
    *,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    reasoning_effort: str | None = None,
) -> dict[str, JSONValue]:
    """构造 OpenAI Responses API 请求体。"""
    payload: dict[str, JSONValue] = {
        "model": model,
        "stream": True,
        "store": False,
        "instructions": system,
        "input": _messages_to_responses_input(messages),
    }
    effort = _normalize_responses_effort(reasoning_effort)
    if effort is not None:
        payload["reasoning"] = {"effort": effort, "summary": "auto"}
    if tools:
        payload["tools"] = [_tool_to_responses(tool) for tool in tools]
    return payload


def _normalize_responses_effort(reasoning_effort: str | None) -> str | None:
    """把内部 reasoning effort 映射到 Responses API 字段。"""
    if reasoning_effort is None:
        return None
    normalized = reasoning_effort.strip().lower()
    if normalized in ("", "none"):
        return None
    return normalized


def _messages_to_responses_input(messages: list[AgentMessage]) -> list[JSONValue]:
    """把 provider-neutral transcript 消息转成 Responses API input。"""
    items: list[JSONValue] = []
    for message in messages:
        if isinstance(message, UserMessage):
            items.append({"role": "user", "content": message.content})
        elif isinstance(message, AssistantMessage):
            if message.content:
                items.append({"role": "assistant", "content": message.content})
            for tool_call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": dumps(tool_call.arguments),
                    }
                )
        elif isinstance(message, ToolResultMessage):
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
    return items


def _tool_to_responses(tool: AgentTool) -> dict[str, JSONValue]:
    """把 AgentTool 声明转成 Responses API function tool。"""
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.input_schema),
    }


def _register_responses_item(
    builders: dict[str, _ResponsesToolCallBuilder],
    item: object,
    *,
    output_index: object,
) -> None:
    """登记 Responses API 新增的 function_call item。"""
    if not isinstance(item, Mapping) or item.get("type") != "function_call":
        return
    item_id = item.get("id")
    if not isinstance(item_id, str):
        return
    raw_arguments = item.get("arguments")
    builder = builders.setdefault(item_id, _ResponsesToolCallBuilder())
    builder.set_final(
        call_id=_str_or_none(item.get("call_id")),
        name=_str_or_none(item.get("name")),
        arguments=raw_arguments if isinstance(raw_arguments, str) and raw_arguments else None,
        output_index=_int_or_none(output_index),
    )


def _finalize_responses_item(
    builders: dict[str, _ResponsesToolCallBuilder],
    item: object,
    *,
    output_index: object,
) -> None:
    """用最终 output item 补齐 Responses API function call。"""
    if not isinstance(item, Mapping) or item.get("type") != "function_call":
        return
    item_id = item.get("id")
    if not isinstance(item_id, str):
        return
    builder = builders.setdefault(item_id, _ResponsesToolCallBuilder())
    builder.set_final(
        call_id=_str_or_none(item.get("call_id")),
        name=_str_or_none(item.get("name")),
        arguments=item.get("arguments"),
        output_index=_int_or_none(output_index),
    )


def _ordered_builders(
    builders: dict[str, _ResponsesToolCallBuilder],
) -> list[_ResponsesToolCallBuilder]:
    """按 output_index 保持 Responses API 工具调用顺序。"""
    return [
        builder for _, builder in sorted(builders.items(), key=lambda pair: pair[1].output_index)
    ]


def _responses_finish_reason(chunk: Mapping[str, Any]) -> str | None:
    """从 Responses API 终止事件中提取状态。"""
    response = chunk.get("response")
    if isinstance(response, Mapping):
        status = response.get("status")
        if isinstance(status, str):
            return status
    return None


def _normalize_finish_reason(status: str | None, *, has_tool_calls: bool) -> str:
    """把 Responses API 状态映射成 chat-completions 风格 finish_reason。"""
    if has_tool_calls:
        return "tool_calls"
    if status == "incomplete":
        return "length"
    return "stop"


def _responses_failure_event(chunk: Mapping[str, Any]) -> ProviderErrorEvent:
    """把 Responses API failed 事件转换成统一错误事件。"""
    message = "Provider response failed"
    response = chunk.get("response")
    if isinstance(response, Mapping):
        error = response.get("error")
        if isinstance(error, Mapping):
            error_message = error.get("message")
            if isinstance(error_message, str) and error_message:
                message = error_message
    return ProviderErrorEvent(message=message, data={"event": dict(chunk)})


def _responses_error_message(chunk: Mapping[str, Any]) -> str:
    """提取 Responses API error 事件的人类可读消息。"""
    message = chunk.get("message")
    if isinstance(message, str) and message:
        return message
    error = chunk.get("error")
    if isinstance(error, Mapping):
        nested = error.get("message")
        if isinstance(nested, str) and nested:
            return nested
    return "Provider stream error"


def _str_or_none(value: object) -> str | None:
    """把非空字符串保留为字符串，否则返回 None。"""
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    """把整数保留为整数，否则返回 None。"""
    return value if isinstance(value, int) else None


def _system_message(system: str) -> dict[str, JSONValue]:
    """构造 chat-completions system message。"""
    return {"role": "system", "content": system}


def _message_to_openai(message: AgentMessage) -> dict[str, JSONValue]:
    """把 provider-neutral transcript message 转成 OpenAI chat message。"""
    if isinstance(message, UserMessage):
        return {"role": "user", "content": message.content}

    if isinstance(message, AssistantMessage):
        item: dict[str, JSONValue] = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            item["tool_calls"] = [
                _tool_call_to_openai(tool_call) for tool_call in message.tool_calls
            ]
        return item

    if isinstance(message, ToolResultMessage):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "name": message.name,
            "content": message.content,
        }

    raise TypeError(f"Unsupported message type: {type(message)!r}")


def _tool_to_openai(tool: AgentTool) -> dict[str, JSONValue]:
    """把 AgentTool 声明转成 chat-completions function tool。"""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.input_schema),
        },
    }


def _tool_call_to_openai(tool_call: ToolCall) -> dict[str, JSONValue]:
    """把 ToolCall 转成 OpenAI assistant tool_call。"""
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {"name": tool_call.name, "arguments": dumps(tool_call.arguments)},
    }


def _parse_sse_line(line: str) -> str | None:
    """解析一行 SSE data；非 data 行返回 None。"""
    line = line.strip()
    if not line or not line.startswith("data:"):
        return None
    return line.removeprefix("data:").strip()


def _loads_object(value: str) -> dict[str, JSONValue] | None:
    """只接受 JSON object；非法 JSON 或非对象值返回 None。"""
    try:
        loaded = loads(value)
    except JSONDecodeError:
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


def _first_choice(chunk: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """取 chat-completions chunk 的第一个 choice。"""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return None
    return choice


def _tool_call_deltas(delta: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """提取一组合法 tool_call delta。"""
    tool_calls = delta.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    return [tool_call for tool_call in tool_calls if isinstance(tool_call, Mapping)]


def _thinking_delta_text(delta: Mapping[str, Any]) -> str:
    """兼容不同 OpenAI-like 服务的 reasoning 字段名。"""
    for field_name in ("reasoning_content", "reasoning", "thinking"):
        value = delta.get(field_name)
        if isinstance(value, str) and value:
            return value
    return ""


def _is_transient_status(status_code: int) -> bool:
    """判断 HTTP 状态码是否属于可重试的瞬时失败。"""
    return status_code in {408, 409, 425, 429} or status_code >= 500
