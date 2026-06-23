import pytest

from nanoagent.ai import Context, StopReason, UserMessage
from nanoagent.ai.providers.mock import create_mock_model


@pytest.mark.asyncio
async def test_mock_streams_text_then_done():
    mock = create_mock_model(responses=[{"content": ["hello"]}])
    ctx = Context(messages=[UserMessage(content="hi")])
    events = [e async for e in mock.stream(mock, ctx, None)]
    assert events[0].type == "start"
    assert events[-1].type == "done"
    assert events[-1].message.content[0].text == "hello"
    assert events[-1].message.stop_reason is StopReason.STOP
    assert len(mock.calls) == 1


@pytest.mark.asyncio
async def test_mock_tool_call_sets_tool_use():
    mock = create_mock_model(
        responses=[{"content": [{"type": "toolCall", "name": "echo", "arguments": {"x": 1}}]}]
    )
    events = [e async for e in mock.stream(mock, Context(), None)]
    done = events[-1]
    assert done.message.stop_reason is StopReason.TOOL_USE
    assert done.message.content[0].name == "echo"


@pytest.mark.asyncio
async def test_mock_response_can_set_usage():
    mock = create_mock_model(
        responses=[
            {
                "content": ["ok"],
                "usage": {"input": 2, "output": 3, "total_tokens": 5},
            }
        ]
    )

    events = [e async for e in mock.stream(mock, Context(), None)]

    assert events[-1].message.usage.input == 2
    assert events[-1].message.usage.output == 3
    assert events[-1].message.usage.total_tokens == 5


@pytest.mark.asyncio
async def test_mock_model_copies_scripted_responses_on_init():
    responses = [{"content": ["original"]}]

    mock = create_mock_model(responses=responses)
    responses[0]["content"][0] = "mutated"
    events = [e async for e in mock.stream(mock, Context(), None)]

    assert events[-1].message.content[0].text == "original"
