from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict

from nanoagent.agent.messages import AssistantMessage as AgentAssistantMessage
from nanoagent.agent.tools import ToolCall as AgentToolCall
from nanoagent.agent.types import JSONValue
from nanoagent.ai.messages import AssistantMessage, ToolCall


class ProviderResponseStartEvent(BaseModel):
    """provider 已经开始一次模型响应。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["response_start"] = "response_start"
    model: str


class ProviderRetryEvent(BaseModel):
    """provider adapter 正在重试一次临时失败的请求。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["retry"] = "retry"
    attempt: int
    max_attempts: int
    delay_seconds: float
    message: str
    data: dict[str, JSONValue] | None = None


class ProviderTextDeltaEvent(BaseModel):
    """provider 流式返回的一段普通文本增量。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text_delta"] = "text_delta"
    delta: str


class ProviderThinkingDeltaEvent(BaseModel):
    """provider 流式返回的一段 reasoning/thinking 增量。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["thinking_delta"] = "thinking_delta"
    delta: str


class ProviderToolCallEvent(BaseModel):
    """模型已经给出一个完整的工具调用请求。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"] = "tool_call"
    tool_call: AgentToolCall


class ProviderResponseEndEvent(BaseModel):
    """provider 已经完成一次模型响应，并给出最终 assistant 消息。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["response_end"] = "response_end"
    message: AgentAssistantMessage
    finish_reason: str | None = None


class ProviderErrorEvent(BaseModel):
    """provider 级错误，可由 agent 层按统一事件处理。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["error"] = "error"
    message: str
    data: dict[str, JSONValue] | None = None


type ProviderEvent = (
    ProviderResponseStartEvent
    | ProviderRetryEvent
    | ProviderTextDeltaEvent
    | ProviderThinkingDeltaEvent
    | ProviderToolCallEvent
    | ProviderResponseEndEvent
    | ProviderErrorEvent
)


@dataclass
class StreamStart:
    """一次 assistant 流开始。"""

    type: str = field(default="start", init=False)


@dataclass
class TextStart:
    """某个文本内容块开始。"""

    content_index: int
    type: str = field(default="text_start", init=False)


@dataclass
class TextDelta:
    """文本内容块的增量片段。"""

    content_index: int
    delta: str
    type: str = field(default="text_delta", init=False)


@dataclass
class TextEnd:
    """文本内容块结束，并给出最终文本。"""

    content_index: int
    text: str
    type: str = field(default="text_end", init=False)


@dataclass
class ThinkingStart:
    """thinking 内容块开始。"""

    content_index: int
    type: str = field(default="thinking_start", init=False)


@dataclass
class ThinkingDelta:
    """thinking 内容块的增量片段。"""

    content_index: int
    delta: str
    type: str = field(default="thinking_delta", init=False)


@dataclass
class ThinkingEnd:
    """thinking 内容块结束，并给出最终内容。"""

    content_index: int
    thinking: str
    type: str = field(default="thinking_end", init=False)


@dataclass
class ToolCallStart:
    """工具调用内容块开始。"""

    content_index: int
    type: str = field(default="toolcall_start", init=False)


@dataclass
class ToolCallDelta:
    """工具调用参数的原始增量片段。"""

    content_index: int
    delta: str
    type: str = field(default="toolcall_delta", init=False)


@dataclass
class ToolCallEnd:
    """工具调用内容块结束，并给出解析后的 ToolCall。"""

    content_index: int
    tool_call: ToolCall
    type: str = field(default="toolcall_end", init=False)


@dataclass
class StreamDone:
    """provider 正常结束，并给出最终 assistant 消息。"""

    message: AssistantMessage
    type: str = field(default="done", init=False)


@dataclass
class StreamError:
    """provider 出错结束，错误信息写入 message。"""

    message: AssistantMessage
    type: str = field(default="error", init=False)


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
