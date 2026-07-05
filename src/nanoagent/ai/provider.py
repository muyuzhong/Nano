from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from nanoagent.agent.messages import AgentMessage
from nanoagent.agent.tools import AgentTool
from nanoagent.ai.events import AssistantMessageEvent, ProviderEvent
from nanoagent.ai.messages import Context
from nanoagent.ai.model import Model
from nanoagent.ai.options import StreamOptions


class CancellationToken(Protocol):
    """provider 流式请求可接收的最小取消信号接口。"""

    def is_cancelled(self) -> bool:
        """返回当前响应流是否应该停止。"""
        ...


class ModelProvider(Protocol):
    """provider-neutral 的模型响应流接口。"""

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """把一次模型响应流式转换为 provider-neutral 事件。"""
        ...


class Provider(Protocol):
    """旧 runtime 使用的 provider adapter：把 Context 流式转换为 assistant 事件。"""

    def stream(
        self, model: Model, context: Context, options: StreamOptions | None
    ) -> AsyncIterator[AssistantMessageEvent]: ...


_REGISTRY: dict[str, Provider] = {}


def register_provider(api: str, provider: Provider) -> None:
    """注册旧 runtime provider；api 只是分发键，不代表默认模型选择。"""

    if api == "":
        raise ValueError("api must not be empty")
    _REGISTRY[api] = provider


def get_provider(api: str) -> Provider:
    """按 api 分发键获取旧 runtime provider。"""

    if api not in _REGISTRY:
        raise KeyError(f"no provider registered for api {api!r}")
    return _REGISTRY[api]


def registered_provider_apis() -> tuple[str, ...]:
    """返回当前已注册 api 的稳定快照，便于测试或 harness introspection。"""

    return tuple(sorted(_REGISTRY))


def clear_providers() -> None:
    """清空旧 registry，主要用于测试隔离。"""

    _REGISTRY.clear()


def stream(
    model: Model, context: Context, options: StreamOptions | None = None
) -> AsyncIterator[AssistantMessageEvent]:
    """按 model.api 分发；框架不选择 provider，也不发现 API key。"""
    return get_provider(model.api).stream(model, context, options)
