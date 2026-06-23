import asyncio

import pytest
from pydantic import BaseModel

from nanoagent.ai import (
    AssistantMessage,
    StopReason as WireStopReason,
    TextContent,
    Usage,
    UserMessage,
)
from nanoagent.ai.provider import clear_providers
from nanoagent.ai.providers.mock import create_mock_model, register_mock
from nanoagent.agent.agent import Agent
from nanoagent.agent.result import StopReason
from nanoagent.agent.tools import AgentTool, AgentToolResult


class _NoopArgs(BaseModel):
    pass


class _NoopTool(AgentTool):
    name = "noop"
    description = "noop"
    parameters = _NoopArgs
    label = "Noop"

    async def execute(self, tool_call_id, params, signal=None):
        return AgentToolResult(content=[TextContent(text="ok")])


@pytest.mark.asyncio
async def test_prompt_returns_result_and_accumulates_history():
    clear_providers()
    register_mock()
    mock = create_mock_model(responses=[{"content": ["hi"]}, {"content": ["again"]}])
    agent = Agent(model=mock, system_prompt=["sys"])
    seen = []
    agent.subscribe(lambda e: seen.append(e.type))

    r1 = await agent.prompt("hello")
    assert r1.reason is StopReason.COMPLETED
    assert "agent_start" in seen and "agent_end" in seen

    r2 = await agent.prompt("more")
    assert r2.reason is StopReason.COMPLETED
    roles = [m.role for m in agent.state.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert len(mock.calls) == 2
    assert len(mock.calls[1].messages) >= 3


@pytest.mark.asyncio
async def test_prompt_list_input_is_snapshotted_for_run():
    clear_providers()
    register_mock()
    mock = create_mock_model(responses=[{"content": ["hi"]}])
    agent = Agent(model=mock)
    prompts = [UserMessage(content="first")]

    def mutate_prompts(event):
        if event.type == "agent_start":
            prompts.append(UserMessage(content="late"))

    agent.subscribe(mutate_prompts)
    await agent.prompt(prompts)

    user_messages = [m for m in agent.state.messages if m.role == "user"]
    assert [m.content for m in user_messages] == ["first"]


@pytest.mark.asyncio
async def test_steer_injects_queued_message_into_next_turn():
    clear_providers()
    register_mock()

    holder: dict = {"n": 0}

    def handler(context):
        holder["n"] += 1
        if holder["n"] == 1:
            # steer mid-run: should surface to the model at the next turn boundary
            holder["agent"].steer(UserMessage(content="STEER-ME"))
            return {"content": [{"type": "toolCall", "name": "noop", "arguments": {}}]}
        return {"content": ["done"]}

    mock = create_mock_model(handler=handler)
    agent = Agent(model=mock, tools=[_NoopTool()])
    holder["agent"] = agent

    result = await agent.prompt("go")
    assert result.reason is StopReason.COMPLETED
    assert len(mock.calls) == 2

    # the model must SEE the steered message on the second turn's context
    turn2_user_texts = [
        m.content
        for m in mock.calls[1].messages
        if m.role == "user" and isinstance(m.content, str)
    ]
    assert "STEER-ME" in turn2_user_texts

    # and it persists in agent state (surfaced via produced)
    assert any(
        getattr(m, "role", None) == "user" and getattr(m, "content", None) == "STEER-ME"
        for m in agent.state.messages
    )


@pytest.mark.asyncio
async def test_async_subscriber_is_awaited():
    clear_providers()
    register_mock()
    mock = create_mock_model(responses=[{"content": ["hi"]}])
    agent = Agent(model=mock)
    seen: list[str] = []

    async def listener(e):
        await asyncio.sleep(0)
        seen.append(e.type)

    agent.subscribe(listener)
    await agent.prompt("hello")
    # If the awaitable weren't awaited, the agent_end coroutine would never run.
    assert "agent_end" in seen


@pytest.mark.asyncio
async def test_state_updated_on_message_end():
    clear_providers()
    register_mock()
    mock = create_mock_model(responses=[{"content": ["hi"]}])
    agent = Agent(model=mock)
    observed: list[bool] = []

    def listener(e):
        if e.type == "message_end":
            observed.append(e.message in agent.state.messages)

    agent.subscribe(listener)
    await agent.prompt("hello")
    # Each message must already be in state by the time its message_end is emitted.
    assert observed and all(observed)


@pytest.mark.asyncio
async def test_streaming_message_set_during_update_and_cleared_after():
    clear_providers()
    register_mock()
    mock = create_mock_model(responses=[{"content": ["hi"]}])
    agent = Agent(model=mock)
    saw_streaming: list[bool] = []

    def listener(e):
        if e.type == "message_update":
            saw_streaming.append(agent.state.streaming_message is not None)

    agent.subscribe(listener)
    await agent.prompt("hello")
    assert any(saw_streaming)
    assert agent.state.streaming_message is None


@pytest.mark.asyncio
async def test_pending_tool_calls_tracked_during_execution():
    clear_providers()
    register_mock()
    mock = create_mock_model(
        responses=[
            {"content": [{"type": "toolCall", "name": "noop", "arguments": {}}]},
            {"content": ["done"]},
        ]
    )
    agent = Agent(model=mock, tools=[_NoopTool()])
    snap: dict = {}

    def listener(e):
        if e.type == "tool_execution_start":
            snap["start"] = e.tool_call_id in agent.state.pending_tool_calls
        elif e.type == "tool_execution_end":
            snap["end"] = e.tool_call_id in agent.state.pending_tool_calls

    agent.subscribe(listener)
    await agent.prompt("go")
    assert snap.get("start") is True
    assert snap.get("end") is False
    assert agent.state.pending_tool_calls == {}


@pytest.mark.asyncio
async def test_hook_error_clears_in_flight_state():
    clear_providers()
    register_mock()
    mock = create_mock_model(
        responses=[{"content": [{"type": "toolCall", "name": "noop", "arguments": {}}]}]
    )

    class BoomControl:
        async def request_approval(self, tool_call, tier):
            raise RuntimeError("approval boom")

    agent = Agent(model=mock, tools=[_NoopTool()], control=BoomControl())
    result = await agent.prompt("go")

    assert result.reason is StopReason.ERROR
    assert agent.state.pending_tool_calls == {}
    assert agent.state.streaming_message is None
    assert agent.state.is_streaming is False


@pytest.mark.asyncio
async def test_wait_for_idle_waits_for_active_run():
    clear_providers()
    register_mock()
    mock = create_mock_model(responses=[{"content": ["hi"]}])
    agent = Agent(model=mock)

    task = asyncio.create_task(agent.prompt("hello"))
    await asyncio.sleep(0)  # let prompt() begin and mark itself busy
    await agent.wait_for_idle()

    assert task.done()
    assert agent.state.is_streaming is False
    await task


@pytest.mark.asyncio
async def test_streaming_message_visible_from_assistant_message_start():
    clear_providers()
    register_mock()
    mock = create_mock_model(responses=[{"content": ["hi"]}])
    agent = Agent(model=mock)
    seen: list[bool] = []

    def listener(e):
        if e.type == "message_start" and getattr(e.message, "role", None) == "assistant":
            # streaming_message must already be the assistant message at its start,
            # not only once the first message_update arrives.
            seen.append(agent.state.streaming_message is e.message)

    agent.subscribe(listener)
    await agent.prompt("hello")
    assert seen == [True]


@pytest.mark.asyncio
async def test_assistant_prompt_is_not_exposed_as_streaming_output():
    clear_providers()
    register_mock()
    mock = create_mock_model(responses=[{"content": ["generated"]}])
    agent = Agent(model=mock)
    injected = AssistantMessage(
        content=[TextContent(text="history")],
        model="injected",
        provider="test",
        api="test",
        usage=Usage(),
        stop_reason=WireStopReason.STOP,
    )
    observed: list[tuple[str, bool]] = []

    def listener(e):
        if e.type == "message_start" and e.message.role == "assistant":
            observed.append((e.message.id, agent.state.streaming_message is e.message))

    agent.subscribe(listener)
    await agent.prompt(injected)

    assert observed[0] == (injected.id, False)
    assert observed[1][1] is True


@pytest.mark.asyncio
async def test_error_message_set_on_error_run_and_reset_next_run():
    clear_providers()
    register_mock()
    mock = create_mock_model(responses=[{"error": "boom"}, {"content": ["ok"]}])
    agent = Agent(model=mock)

    r1 = await agent.prompt("go")
    assert r1.reason is StopReason.ERROR
    assert agent.state.error_message == "boom"

    r2 = await agent.prompt("again")
    assert r2.reason is StopReason.COMPLETED
    assert agent.state.error_message is None
