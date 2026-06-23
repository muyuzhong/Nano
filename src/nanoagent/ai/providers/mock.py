from __future__ import annotations

import copy
from typing import AsyncIterator, Callable

from nanoagent.ai.events import (
    AssistantMessageEvent,
    StreamDone,
    StreamError,
    StreamStart,
    TextDelta,
    TextEnd,
    TextStart,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
)
from nanoagent.ai.messages import AssistantMessage, Context, TextContent, ToolCall, Usage
from nanoagent.ai.model import Model
from nanoagent.ai.options import StreamOptions
from nanoagent.ai.provider import register_provider
from nanoagent.ai.stop_reason import StopReason


class MockModel(Model):
    def __init__(
        self,
        *,
        id: str = "mock-model",
        provider: str = "mock",
        responses: list[dict] | None = None,
        handler: Callable[[Context], dict] | None = None,
    ):
        super().__init__(id=id, api="mock", provider=provider)
        self._responses = copy.deepcopy(responses or [])
        self._handler = handler
        self._idx = 0
        self.calls: list[Context] = []
        self._tc = 0

    def stream(
        self, model: Model, context: Context, options: StreamOptions | None
    ) -> AsyncIterator[AssistantMessageEvent]:
        return self._run(context)

    def _next_response(self, context: Context) -> dict:
        if self._idx < len(self._responses):
            r = self._responses[self._idx]
            self._idx += 1
            return r
        if self._handler is not None:
            return self._handler(context)
        raise AssertionError(f"mock exhausted at call {len(self.calls)}")

    async def _run(self, context: Context) -> AsyncIterator[AssistantMessageEvent]:
        self.calls.append(context)
        resp = self._next_response(context)
        msg = AssistantMessage.empty(self.id, self.provider, self.api)
        yield StreamStart()
        for i, block in enumerate(resp.get("content", [])):
            if isinstance(block, str):
                msg.content.append(TextContent(text=block))
                yield TextStart(content_index=i)
                yield TextDelta(content_index=i, delta=block)
                yield TextEnd(content_index=i, text=block)
            elif block.get("type") == "toolCall":
                self._tc += 1
                tc = ToolCall(
                    id=block.get("id") or f"mock-tc-{self._tc}",
                    name=block["name"],
                    arguments=dict(block["arguments"]),
                )
                msg.content.append(tc)
                yield ToolCallStart(content_index=i)
                yield ToolCallDelta(content_index=i, delta=str(tc.arguments))
                yield ToolCallEnd(content_index=i, tool_call=tc)
        has_tool = any(getattr(c, "type", None) == "toolCall" for c in msg.content)
        reason = resp.get("stop_reason") or (StopReason.TOOL_USE if has_tool else StopReason.STOP)
        msg.stop_reason = reason
        usage = resp.get("usage")
        if isinstance(usage, Usage):
            msg.usage = usage
        elif isinstance(usage, dict):
            msg.usage = Usage(
                input=usage.get("input", 0),
                output=usage.get("output", 0),
                total_tokens=usage.get("total_tokens", 0),
            )
        if resp.get("error"):
            msg.stop_reason = StopReason.ERROR
            msg.error_message = resp["error"]
            yield StreamError(message=msg)
            return
        yield StreamDone(message=msg)


def create_mock_model(
    *,
    responses: list[dict] | None = None,
    handler: Callable[[Context], dict] | None = None,
    id: str = "mock-model",
    provider: str = "mock",
) -> MockModel:
    return MockModel(id=id, provider=provider, responses=responses, handler=handler)


def register_mock() -> None:
    """Register the 'mock' api; dispatch to the MockModel's own stream."""

    class _MockDispatch:
        def stream(self, model, context, options):
            return model.stream(model, context, options)

    register_provider("mock", _MockDispatch())
