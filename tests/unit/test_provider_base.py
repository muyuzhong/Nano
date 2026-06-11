"""Provider 抽象层的协议与异常层级测试。"""

import pytest

from providers.base import (
    ModelProvider,
    ModelRequest,
    ProviderAuthError,
    ProviderError,
    ProviderServerError,
    ProviderTimeoutError,
    RateLimitError,
    TextDelta,
)
from runtime.blocks import Message


def test_cannot_instantiate_abstract_provider():
    with pytest.raises(TypeError):
        ModelProvider()


def test_error_hierarchy():
    assert issubclass(RateLimitError, ProviderError)
    assert issubclass(ProviderTimeoutError, ProviderError)
    assert issubclass(ProviderServerError, ProviderError)
    assert issubclass(ProviderAuthError, ProviderError)
    assert RateLimitError(retry_after=7.5).retry_after == 7.5


def test_default_token_estimate():
    class Dummy(ModelProvider):
        async def stream(self, request):
            yield TextDelta(text="x")

    est = Dummy().count_tokens_estimate([Message.user("hello"), Message.user("world")])
    assert est >= 2


def test_model_request_defaults():
    req = ModelRequest(system="s", messages=[], tools=[], model="m")
    assert req.max_tokens == 4096
