import httpx
import pytest
from providers.base import MessageEnd, ModelRequest, ProviderAuthError, RateLimitError, TextDelta
from providers.openai_compat import OpenAICompatProvider
from runtime.blocks import Message


async def test_text_stream():
    body = 'data: {"model":"m","choices":[{"delta":{"content":"你"}}]}\n\n' \
           'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n' \
           'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n' \
           'data: [DONE]\n\n'
    provider = OpenAICompatProvider("key", "https://example.com", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)))
    events = [e async for e in provider.stream(ModelRequest("s", [Message.user("hi")], [], "m"))]
    assert any(isinstance(e, TextDelta) for e in events)
    assert isinstance(events[-1], MessageEnd) and events[-1].usage.output_tokens == 3


@pytest.mark.parametrize("status,error", [(429, RateLimitError), (401, ProviderAuthError)])
async def test_status_is_normalized(status, error):
    provider = OpenAICompatProvider("key", "https://example.com",
                                    transport=httpx.MockTransport(lambda r: httpx.Response(status, content="bad", headers={"retry-after": "1"})))
    with pytest.raises(error):
        async for _ in provider.stream(ModelRequest("s", [Message.user("hi")], [], "m")):
            pass
