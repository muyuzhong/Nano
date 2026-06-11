from providers.base import ProviderAuthError, RateLimitError
from providers.mock import MockProvider
from runtime import events as ev
from runtime.context import RetryPolicy, TokenLedger
from runtime.engine import AgentLoop
from runtime.executor import ToolRegistry
from runtime.state import SessionState
from tests.helpers import EchoTool, FakeSleep, collect


def make_loop(tmp_path, script, tools=(), **kwargs):
    registry = ToolRegistry()
    for tool in tools: registry.register(tool)
    sleep = FakeSleep()
    return AgentLoop(MockProvider(script), registry, state=SessionState(transcript_dir=str(tmp_path)),
                     retry_policy=RetryPolicy(sleep=sleep), **kwargs), sleep


async def test_text_only_completes(tmp_path):
    loop, _ = make_loop(tmp_path, [MockProvider.text_turn("你好")])
    events = await collect(loop.run("hi"))
    assert events[-1].reason == "completed" and len(loop.state.messages) == 2


async def test_tool_roundtrip(tmp_path):
    loop, _ = make_loop(tmp_path, [MockProvider.tool_turn("echo", {"text": "hi"}, "t1"), MockProvider.text_turn("完成")], [EchoTool()])
    events = await collect(loop.run("hi"))
    assert events[-1].reason == "completed" and any(isinstance(e, ev.ToolResultReceived) for e in events)
    assert len(loop.state.messages) == 4


async def test_retryable_error_then_success(tmp_path):
    loop, sleep = make_loop(tmp_path, [RateLimitError(0), MockProvider.text_turn("恢复")])
    events = await collect(loop.run("hi"))
    assert events[-1].reason == "completed" and sleep.calls == [0]


async def test_non_retryable_error_ends_run(tmp_path):
    loop, _ = make_loop(tmp_path, [ProviderAuthError("bad")])
    assert (await collect(loop.run("hi")))[-1].reason == "provider_error"


async def test_broken_tool_json_fed_back_as_error(tmp_path):
    from providers.base import MessageEnd, MessageStart, ToolInputDelta, ToolUseEnd, ToolUseStart
    from runtime.blocks import Usage
    broken = [MessageStart("mock-model"), ToolUseStart("t1", "echo"),
              ToolInputDelta("t1", '{"text": '), ToolUseEnd("t1"),
              MessageEnd("tool_use", Usage(1, 1))]
    loop, _ = make_loop(tmp_path, [broken, MockProvider.text_turn("知道了")], [EchoTool()])
    events = await collect(loop.run("hi"))
    assert any(isinstance(event, ev.ToolResultReceived) and event.is_error for event in events)
    assert events[-1].reason == "completed"


async def test_token_budget_stops_loop(tmp_path):
    loop, _ = make_loop(tmp_path, [MockProvider.tool_turn("echo", {"text": "a"})], [EchoTool()], ledger=TokenLedger(max_api_calls=1))
    assert (await collect(loop.run("hi")))[-1].reason == "token_budget"


async def test_max_turns(tmp_path):
    loop, _ = make_loop(tmp_path, [MockProvider.tool_turn("echo", {"text": "a"})] * 2, [EchoTool()], max_turns=2)
    assert (await collect(loop.run("hi")))[-1].reason == "max_turns"
