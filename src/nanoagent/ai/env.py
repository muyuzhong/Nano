"""基于环境变量的 provider 配置辅助函数。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from os import environ

DEFAULT_OPENAI_COMPATIBLE_BASE_URL = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS = 60.0
DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES = 2
DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    """OpenAI-compatible chat completions endpoint 的运行配置。"""

    api_key: str
    base_url: str = DEFAULT_OPENAI_COMPATIBLE_BASE_URL
    headers: Mapping[str, str] | None = None
    timeout_seconds: float = DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES
    max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS
    reasoning_effort: str | None = None
    reasoning_effort_parameter: str = "reasoning_effort"


@dataclass(frozen=True, slots=True)
class AnthropicConfig:
    """Anthropic Messages API 的运行配置，先作为后续 provider 的配置占位。"""

    api_key: str
    base_url: str = DEFAULT_ANTHROPIC_BASE_URL
    headers: Mapping[str, str] | None = None
    timeout_seconds: float = DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES
    max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS
    thinking_budget_tokens: int | None = None


def openai_compatible_config_from_env(
    *,
    api_key_var: str = "OPENAI_API_KEY",
    base_url_var: str = "OPENAI_BASE_URL",
    timeout_seconds_var: str = "OPENAI_TIMEOUT_SECONDS",
    max_retries_var: str = "OPENAI_MAX_RETRIES",
    max_retry_delay_seconds_var: str = "OPENAI_MAX_RETRY_DELAY_SECONDS",
    default_timeout_seconds: float = DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
    default_max_retries: int = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES,
    default_max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
) -> OpenAICompatibleConfig:
    """从环境变量加载 OpenAI-compatible provider 配置。"""
    api_key = environ.get(api_key_var)
    if not api_key:
        msg = f"Missing required environment variable: {api_key_var}"
        raise RuntimeError(msg)

    timeout_seconds = _timeout_seconds_from_env(timeout_seconds_var, default_timeout_seconds)
    max_retries = _non_negative_int_from_env(max_retries_var, default_max_retries)
    max_retry_delay_seconds = _non_negative_float_from_env(
        max_retry_delay_seconds_var, default_max_retry_delay_seconds
    )
    return OpenAICompatibleConfig(
        api_key=api_key,
        base_url=environ.get(base_url_var, DEFAULT_OPENAI_COMPATIBLE_BASE_URL).rstrip("/"),
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        max_retry_delay_seconds=max_retry_delay_seconds,
    )


def _timeout_seconds_from_env(name: str, default: float) -> float:
    """读取正数 timeout；非法值转成清晰的 RuntimeError。"""
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        timeout_seconds = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable must be a number: {name}") from exc
    if timeout_seconds <= 0:
        raise RuntimeError(f"Environment variable must be greater than 0: {name}")
    return timeout_seconds


def _non_negative_int_from_env(name: str, default: int) -> int:
    """读取非负整数环境变量。"""
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable must be an integer: {name}") from exc
    if value < 0:
        raise RuntimeError(f"Environment variable must be 0 or greater: {name}")
    return value


def _non_negative_float_from_env(name: str, default: float) -> float:
    """读取非负浮点环境变量。"""
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable must be a number: {name}") from exc
    if value < 0:
        raise RuntimeError(f"Environment variable must be 0 or greater: {name}")
    return value
