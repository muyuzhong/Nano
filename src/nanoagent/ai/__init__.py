"""nanoagent.ai — provider abstraction + wire message model + streaming."""

from nanoagent.ai.accumulator import StreamAccumulator, accumulate
from nanoagent.ai.events import (
    AssistantMessageEvent,
    StreamDone,
    StreamError,
    StreamStart,
    TextDelta,
    TextEnd,
    TextStart,
    ThinkingDelta,
    ThinkingEnd,
    ThinkingStart,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
)
from nanoagent.ai.messages import (
    AssistantContent,
    AssistantMessage,
    Context,
    ImageContent,
    Message,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserContent,
    UserMessage,
    Usage,
)
from nanoagent.ai.stop_reason import StopReason

__all__ = [
    # stop reason
    "StopReason",
    # content blocks
    "TextContent",
    "ThinkingContent",
    "ImageContent",
    "ToolCall",
    "AssistantContent",
    "UserContent",
    "Usage",
    # messages
    "UserMessage",
    "AssistantMessage",
    "ToolResultMessage",
    "Message",
    "Context",
    # events
    "AssistantMessageEvent",
    "StreamStart",
    "TextStart",
    "TextDelta",
    "TextEnd",
    "ThinkingStart",
    "ThinkingDelta",
    "ThinkingEnd",
    "ToolCallStart",
    "ToolCallDelta",
    "ToolCallEnd",
    "StreamDone",
    "StreamError",
    # accumulator
    "StreamAccumulator",
    "accumulate",
]
