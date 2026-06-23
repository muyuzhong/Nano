import pytest

from nanoagent.utils import new_id


def test_new_id_unique_and_prefixed():
    ids = {new_id("msg") for _ in range(1000)}
    assert len(ids) == 1000
    assert all(i.startswith("msg_") for i in ids)


def test_new_id_rejects_non_string_prefix():
    with pytest.raises(TypeError, match="prefix must be str"):
        new_id(123)  # type: ignore[arg-type]
