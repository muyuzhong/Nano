import pytest

from nanoagent.ai import (
    AssistantMessage,
    StopReason,
    StreamDone,
    StreamStart,
    TextDelta,
    TextEnd,
    TextStart,
    Usage,
    accumulate,
)


async def _gen(events):
    for e in events:
        yield e


@pytest.mark.asyncio
async def test_accumulate_returns_done_message():
    msg = AssistantMessage(
        content=[], model="m", provider="mock", api="mock", usage=Usage(), stop_reason=StopReason.STOP
    )
    out = await accumulate(_gen([StreamStart(), StreamDone(message=msg)]))
    assert out is msg


@pytest.mark.asyncio
async def test_accumulate_folds_text_when_no_done_message():
    from nanoagent.ai.accumulator import StreamAccumulator

    acc = StreamAccumulator(model_id="m", provider="mock", api="mock")
    for e in [
        StreamStart(),
        TextStart(content_index=0),
        TextDelta(content_index=0, delta="he"),
        TextDelta(content_index=0, delta="llo"),
        TextEnd(content_index=0, text="hello"),
    ]:
        acc.add(e)
    assert acc.message.content[0].text == "hello"
