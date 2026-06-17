import pytest

from nanoagent.ai.provider import clear_providers
from nanoagent.ai.providers.mock import create_mock_model, register_mock
from nanoagent.agent.agent import Agent
from nanoagent.agent.result import StopReason


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
