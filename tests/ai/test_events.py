from nanoagent.ai import StreamDone, TextDelta


def test_event_type_discriminators():
    assert TextDelta(content_index=0, delta="x").type == "text_delta"
    assert StreamDone.__name__ == "StreamDone"
