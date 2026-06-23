from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Union

from nanoagent.ai import Model, UserMessage
from nanoagent.agent.control import AbortSignal, ControlSource
from nanoagent.agent.events import (
    AgentEnd,
    AgentEvent,
    MessageEnd,
    MessageStart,
    MessageUpdate,
    ToolExecutionEnd,
    ToolExecutionStart,
)
from nanoagent.agent.loop import AgentLoopConfig, agent_loop
from nanoagent.agent.messages import AgentMessage, ConvertToLlm, default_convert_to_llm
from nanoagent.agent.result import RunResult, StopReason
from nanoagent.agent.tools import AgentTool


@dataclass
class PendingToolCall:
    """A tool call the loop has started but not yet finished executing."""

    tool_call_id: str
    tool_name: str
    args: dict[str, Any]


@dataclass
class AgentState:
    system_prompt: list[str]
    model: Model
    tools: list[AgentTool] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)
    is_streaming: bool = False
    streaming_message: AgentMessage | None = None
    pending_tool_calls: dict[str, PendingToolCall] = field(default_factory=dict)
    error_message: str | None = None


class AgentBusyError(RuntimeError):
    pass


Listener = Callable[[AgentEvent], Union[None, Awaitable[None]]]


class Agent:
    """Stateful wrapper around agent_loop: holds session, exposes prompt()."""

    def __init__(
        self,
        model: Model,
        *,
        system_prompt: list[str] | None = None,
        tools: list[AgentTool] | None = None,
        convert_to_llm: ConvertToLlm | None = None,
        max_turns: int = 10,
        control: ControlSource | None = None,
        stream_fn: Callable[..., Any] | None = None,
    ):
        self.state = AgentState(
            system_prompt=list(system_prompt or []), model=model, tools=list(tools or [])
        )
        self._convert_to_llm = convert_to_llm or default_convert_to_llm
        self._max_turns = max_turns
        self._control = control
        self._stream_fn = stream_fn
        self._listeners: set[Listener] = set()
        self._signal: AbortSignal | None = None
        self._steering: list[AgentMessage] = []
        self._idle = asyncio.Event()
        self._idle.set()  # no run in flight yet

    def subscribe(self, fn: Listener) -> Callable[[], None]:
        """Register an event listener. Listeners may be sync or return an awaitable."""
        self._listeners.add(fn)
        return lambda: self._listeners.discard(fn)

    async def _emit(self, event: AgentEvent) -> None:
        # Await async listeners inline so prompt() (and wait_for_idle) does not
        # return until every listener — especially the agent_end listener — has settled.
        for fn in list(self._listeners):
            result = fn(event)
            if inspect.isawaitable(result):
                await result

    def _reduce(self, event: AgentEvent) -> None:
        """Fold a single event into state so listeners observe up-to-date state."""
        if isinstance(event, MessageStart):
            if event.generated:
                self.state.streaming_message = event.message
        elif isinstance(event, MessageEnd):
            self.state.messages.append(event.message)
            self.state.streaming_message = None
        elif isinstance(event, MessageUpdate):
            self.state.streaming_message = event.message
        elif isinstance(event, ToolExecutionStart):
            self.state.pending_tool_calls[event.tool_call_id] = PendingToolCall(
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_name,
                args=event.args,
            )
        elif isinstance(event, ToolExecutionEnd):
            self.state.pending_tool_calls.pop(event.tool_call_id, None)
        elif isinstance(event, AgentEnd):
            self.state.error_message = event.result.error

    def set_model(self, m: Model) -> None:
        self.state.model = m

    def set_tools(self, t: list[AgentTool]) -> None:
        self.state.tools = list(t)

    def set_system_prompt(self, s: list[str]) -> None:
        self.state.system_prompt = list(s)

    def abort(self, reason: Any = None) -> None:
        if self._signal is not None:
            self._signal.abort(reason)

    def steer(self, m: AgentMessage) -> None:
        self._steering.append(m)

    async def _get_steering(self) -> list[AgentMessage]:
        out, self._steering = self._steering, []
        return out

    async def wait_for_idle(self) -> None:
        """Block until any in-flight run (and its listeners) has fully settled."""
        await self._idle.wait()

    async def prompt(self, input: str | AgentMessage | list[AgentMessage]) -> RunResult:
        if self.state.is_streaming:
            raise AgentBusyError("agent is already processing")
        if isinstance(input, str):
            prompts: list[AgentMessage] = [UserMessage(content=input)]
        elif isinstance(input, list):
            prompts = list(input)
        else:
            prompts = [input]

        cfg = AgentLoopConfig(
            model=self.state.model,
            convert_to_llm=self._convert_to_llm,
            max_turns=self._max_turns,
            control=self._control,
            get_steering_messages=self._get_steering,
            stream_fn=self._stream_fn,
        )
        self._signal = AbortSignal()
        self.state.is_streaming = True
        self.state.error_message = None
        self._idle.clear()
        result = RunResult(reason=StopReason.ERROR)
        try:
            async for event in agent_loop(
                prompts=prompts,
                system_prompt=self.state.system_prompt,
                messages=self.state.messages,
                tools=self.state.tools,
                config=cfg,
                signal=self._signal,
            ):
                # Reduce before emitting so listeners see current state.
                self._reduce(event)
                await self._emit(event)
                if isinstance(event, AgentEnd):
                    result = event.result
        finally:
            self.state.is_streaming = False
            self.state.streaming_message = None
            self._signal = None
            self._idle.set()
        return result
