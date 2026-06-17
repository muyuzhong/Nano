from __future__ import annotations

from typing import AsyncIterator

from nanoagent.ai.events import AssistantMessageEvent
from nanoagent.ai.messages import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
)
from nanoagent.ai.stop_reason import StopReason


class StreamAccumulator:
    """Fold incremental events into one AssistantMessage (consumer/UI helper)."""

    def __init__(self, model_id: str, provider: str, api: str):
        self._msg = AssistantMessage(
            content=[],
            model=model_id,
            provider=provider,
            api=api,
            usage=Usage(),
            stop_reason=StopReason.STOP,
        )

    @property
    def message(self) -> AssistantMessage:
        return self._msg

    def add(self, event: AssistantMessageEvent) -> None:
        t = event.type
        if t == "text_start":
            self._msg.content.append(TextContent(text=""))
        elif t == "text_end":
            self._msg.content[event.content_index] = TextContent(text=event.text)
        elif t == "thinking_start":
            self._msg.content.append(ThinkingContent(thinking=""))
        elif t == "thinking_end":
            self._msg.content[event.content_index] = ThinkingContent(thinking=event.thinking)
        elif t == "toolcall_start":
            self._msg.content.append(ToolCall(id="", name="", arguments={}))
        elif t == "toolcall_end":
            self._msg.content[event.content_index] = event.tool_call
        elif t in ("done", "error"):
            self._msg = event.message


async def accumulate(events: AsyncIterator[AssistantMessageEvent]) -> AssistantMessage:
    acc: StreamAccumulator | None = None
    async for event in events:
        if acc is None:
            acc = StreamAccumulator(model_id="", provider="", api="")
        acc.add(event)
    if acc is None:
        raise ValueError("stream produced no events")
    return acc.message
