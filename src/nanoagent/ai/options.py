from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class StreamOptions:
    """Injection port: harness decides key/base_url/sampling.

    The framework never decides which provider or which key is used.
    `signal` is duck-typed (only `.aborted` is read) so `ai` need not depend on `agent`.
    """

    api_key: str | None = None
    base_url: str | None = None
    signal: Any = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning: str | None = None

    def __post_init__(self) -> None:
        if self.max_tokens is not None and self.max_tokens < 0:
            raise ValueError("max_tokens must be non-negative")
