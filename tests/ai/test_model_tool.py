import pytest

from nanoagent.ai import Model, ProviderError, StreamOptions, Tool


def test_model_defaults():
    m = Model(id="gpt", api="openai-completions", provider="openai")
    assert m.context_window == 200_000 and m.reasoning is False


def test_tool_holds_json_schema():
    t = Tool(name="echo", description="echo back", parameters={"type": "object"})
    assert t.parameters["type"] == "object"


def test_tool_copies_json_schema_on_init():
    schema = {"type": "object", "properties": {"text": {"type": "string"}}}

    t = Tool(name="echo", description="echo back", parameters=schema)
    schema["properties"]["text"]["type"] = "number"

    assert t.parameters["properties"]["text"]["type"] == "string"


def test_provider_error_structured():
    err = ProviderError("rate limited", status=429, code="rate_limit")
    assert err.status == 429 and err.code == "rate_limit"
    with pytest.raises(ProviderError):
        raise err


def test_stream_options_defaults():
    assert StreamOptions().temperature is None


def test_stream_options_rejects_negative_max_tokens():
    with pytest.raises(ValueError, match="max_tokens must be non-negative"):
        StreamOptions(max_tokens=-1)
