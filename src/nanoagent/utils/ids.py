import itertools
import secrets

_counter = itertools.count(1)


def new_id(prefix: str = "") -> str:
    """Process-unique id: monotonic counter + random suffix. No clock dependency."""
    if not isinstance(prefix, str):
        raise TypeError("prefix must be str")
    n = next(_counter)
    rand = secrets.token_hex(4)
    body = f"{n:012x}{rand}"
    return f"{prefix}_{body}" if prefix else body
