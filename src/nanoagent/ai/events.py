from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from nanoagent.ai.messages import AssistantMessage, ToolCall


@dataclass
class StreamStart:
    type: str = "start"


@dataclass
class TextStart:
    content_index: int
    type: str = "text_start"


@dataclass
class TextDelta:
    content_index: int
    delta: str
    type: str = "text_delta"


@dataclass
class TextEnd:
    content_index: int
    text: str
    type: str = "text_end"


@dataclass
class ThinkingStart:
    content_index: int
    type: str = "thinking_start"


@dataclass
class ThinkingDelta:
    content_index: int
    delta: str
    type: str = "thinking_delta"


@dataclass
class ThinkingEnd:
    content_index: int
    thinking: str
    type: str = "thinking_end"


@dataclass
class ToolCallStart:
    content_index: int
    type: str = "toolcall_start"


@dataclass
class ToolCallDelta:
    content_index: int
    delta: str
    type: str = "toolcall_delta"


@dataclass
class ToolCallEnd:
    content_index: int
    tool_call: ToolCall
    type: str = "toolcall_end"


@dataclass
class StreamDone:
    message: AssistantMessage
    type: str = "done"


@dataclass
class StreamError:
    message: AssistantMessage
    type: str = "error"


AssistantMessageEvent = Union[
    StreamStart,
    TextStart,
    TextDelta,
    TextEnd,
    ThinkingStart,
    ThinkingDelta,
    ThinkingEnd,
    ToolCallStart,
    ToolCallDelta,
    ToolCallEnd,
    StreamDone,
    StreamError,
]
