from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from nanoagent.agent import AgentTool, AssistantMessage, ToolCall, UserMessage
from nanoagent.ai.env import OpenAICompatibleConfig, openai_compatible_config_from_env
from nanoagent.ai.events import (
    ProviderResponseEndEvent,
    ProviderResponseStartEvent,
    ProviderTextDeltaEvent,
    ProviderToolCallEvent,
)
from nanoagent.ai.fake import FakeProvider
from nanoagent.ai.openai_compatible import OpenAICompatibleProvider, _build_chat_payload


async def _noop_executor(arguments, signal=None):
    return None


class _Cancellation:
    def __init__(self) -> None:
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled


def test_provider_events_are_strict_models():
    with pytest.raises(ValidationError):
        ProviderTextDeltaEvent(delta="hi", unexpected=True)

    end = ProviderResponseEndEvent(
        message=AssistantMessage(content="done"),
        finish_reason="stop",
    )

    assert ProviderResponseStartEvent(model="gpt-test").type == "response_start"
    assert end.type == "response_end"
    assert end.message.content == "done"


@pytest.mark.asyncio
async def test_fake_provider_replays_scripted_streams_and_records_calls():
    tool = AgentTool(
        name="echo",
        description="Echo input.",
        input_schema={"type": "object"},
        executor=_noop_executor,
    )
    provider = FakeProvider(
        [
            [
                ProviderResponseStartEvent(model="gpt-test"),
                ProviderTextDeltaEvent(delta="hi"),
            ]
        ]
    )

    events = [
        event
        async for event in provider.stream_response(
            model="gpt-test",
            system="be brief",
            messages=[UserMessage(content="hello")],
            tools=[tool],
        )
    ]

    assert [event.type for event in events] == ["response_start", "text_delta"]
    assert provider.calls[0][0] == "gpt-test"
    assert provider.calls[0][2][0].content == "hello"
    assert provider.calls[0][3][0].name == "echo"


@pytest.mark.asyncio
async def test_fake_provider_stops_when_signal_is_cancelled():
    signal = _Cancellation()
    provider = FakeProvider(
        [
            [
                ProviderTextDeltaEvent(delta="first"),
                ProviderTextDeltaEvent(delta="second"),
            ]
        ]
    )

    events = []
    async for event in provider.stream_response(
        model="gpt-test",
        system="",
        messages=[],
        tools=[],
        signal=signal,
    ):
        events.append(event)
        signal.cancelled = True

    assert [event.delta for event in events] == ["first"]


def test_openai_compatible_config_from_env_reads_and_validates(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "4")
    monkeypatch.setenv("OPENAI_MAX_RETRY_DELAY_SECONDS", "2")

    config = openai_compatible_config_from_env()

    assert config.api_key == "sk-test"
    assert config.base_url == "https://example.test/v1"
    assert config.timeout_seconds == 3.5
    assert config.max_retries == 4
    assert config.max_retry_delay_seconds == 2.0


def test_openai_compatible_config_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        openai_compatible_config_from_env()


def test_build_chat_payload_maps_agent_messages_tools_and_reasoning():
    tool_call = ToolCall(id="call_1", name="echo", arguments={"text": "hi"})
    tool = AgentTool(
        name="echo",
        description="Echo input.",
        input_schema={"type": "object"},
        executor=_noop_executor,
    )

    payload = _build_chat_payload(
        model="gpt-test",
        system="system text",
        messages=[AssistantMessage(content="use tool", tool_calls=[tool_call])],
        tools=[tool],
        reasoning_effort="medium",
    )

    assert payload["messages"][0] == {"role": "system", "content": "system text"}
    assert payload["messages"][1]["tool_calls"][0]["function"]["name"] == "echo"
    assert payload["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert payload["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_openai_compatible_provider_streams_chat_completion_events():
    chunks = [
        {"choices": [{"delta": {"content": "he"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "echo", "arguments": '{"text"'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": ': "hi"}'}}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"content": "llo"}, "finish_reason": "tool_calls"}]},
    ]
    sse = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    sse += "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, text=sse)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        OpenAICompatibleConfig(api_key="sk-test", base_url="https://example.test/v1"),
        client=client,
    )

    events = [
        event
        async for event in provider.stream_response(
            model="gpt-test",
            system="system text",
            messages=[UserMessage(content="hello")],
            tools=[],
        )
    ]

    assert [event.type for event in events] == [
        "response_start",
        "text_delta",
        "text_delta",
        "tool_call",
        "response_end",
    ]
    assert isinstance(events[3], ProviderToolCallEvent)
    assert events[3].tool_call.arguments == {"text": "hi"}
    assert events[-1].message.content == "hello"
    assert events[-1].finish_reason == "tool_calls"

    await provider.aclose()
