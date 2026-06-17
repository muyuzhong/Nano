from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from nanoagent.ai import Model, UserMessage
from nanoagent.agent.control import AbortSignal, ControlSource
from nanoagent.agent.events import AgentEnd, AgentEvent
from nanoagent.agent.loop import AgentLoopConfig, agent_loop
from nanoagent.agent.messages import AgentMessage, ConvertToLlm, default_convert_to_llm
from nanoagent.agent.result import RunResult, StopReason
from nanoagent.agent.tools import AgentTool


@dataclass
class AgentState:
    system_prompt: list[str]
    model: Model
    tools: list[AgentTool] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)
    is_streaming: bool = False


class AgentBusyError(RuntimeError):
    pass


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
        self._listeners: set[Callable[[AgentEvent], None]] = set()
        self._signal: AbortSignal | None = None
        self._steering: list[AgentMessage] = []

    def subscribe(self, fn: Callable[[AgentEvent], None]) -> Callable[[], None]:
        self._listeners.add(fn)
        return lambda: self._listeners.discard(fn)

    def _emit(self, event: AgentEvent) -> None:
        for fn in list(self._listeners):
            fn(event)

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

    async def prompt(self, input: str | AgentMessage | list[AgentMessage]) -> RunResult:
        if self.state.is_streaming:
            raise AgentBusyError("agent is already processing")
        if isinstance(input, str):
            prompts: list[AgentMessage] = [UserMessage(content=input)]
        elif isinstance(input, list):
            prompts = input
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
                self._emit(event)
                if isinstance(event, AgentEnd):
                    for m in event.messages:
                        self.state.messages.append(m)
                    result = event.result
        finally:
            self.state.is_streaming = False
            self._signal = None
        return result
