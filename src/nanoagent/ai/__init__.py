"""nanoagent.ai：provider 抽象、wire message model 和 streaming 工具。"""

from nanoagent.ai.accumulator import StreamAccumulator, accumulate
from nanoagent.ai.events import (
    AssistantMessageEvent,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderResponseEndEvent,
    ProviderResponseStartEvent,
    ProviderRetryEvent,
    ProviderTextDeltaEvent,
    ProviderThinkingDeltaEvent,
    ProviderToolCallEvent,
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
from nanoagent.ai.errors import ProviderError
from nanoagent.ai.fake import FakeProvider
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
from nanoagent.ai.model import Model
from nanoagent.ai.openai_compatible import OpenAICompatibleProvider
from nanoagent.ai.options import StreamOptions
from nanoagent.ai.provider import (
    CancellationToken,
    ModelProvider,
    Provider,
    clear_providers,
    get_provider,
    register_provider,
    registered_provider_apis,
    stream,
)
from nanoagent.ai.stop_reason import StopReason
from nanoagent.ai.tools import Tool
from nanoagent.ai.types import JSONObject, JSONPrimitive, JSONValue

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
    # provider-neutral events
    "ProviderEvent",
    "ProviderResponseStartEvent",
    "ProviderRetryEvent",
    "ProviderTextDeltaEvent",
    "ProviderThinkingDeltaEvent",
    "ProviderToolCallEvent",
    "ProviderResponseEndEvent",
    "ProviderErrorEvent",
    # legacy stream events
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
    # model / tool / errors / options
    "Model",
    "Tool",
    "JSONPrimitive",
    "JSONValue",
    "JSONObject",
    "ProviderError",
    "StreamOptions",
    # provider
    "CancellationToken",
    "ModelProvider",
    "FakeProvider",
    "OpenAICompatibleProvider",
    "Provider",
    "register_provider",
    "registered_provider_apis",
    "get_provider",
    "clear_providers",
    "stream",
]
