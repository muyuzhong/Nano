"""统一工具执行入口。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .context import ToolContext
from .middleware import ToolMiddleware, can_run_parallel
from .registry import ToolRegistry
from .types import JSONValue, ToolResult, ToolUpdateCallback


class ToolRuntime:
    """解析并执行注册工具，把异常转换为结构化错误结果。"""

    def __init__(
        self,
        registry: ToolRegistry,
        context: ToolContext,
        middleware: Sequence[ToolMiddleware] = (),
    ) -> None:
        self.registry = registry
        self.context = context
        self.middleware = tuple(middleware)

    async def execute(
        self,
        *,
        tool_call_id: str,
        name: str,
        arguments: Mapping[str, JSONValue],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        try:
            tool = self.registry.resolve(name)
        except LookupError as exc:
            return ToolResult(content=str(exc), is_error=True)

        pre = [item for item in self.middleware if item.phase == "pre"]
        post = [item for item in self.middleware if item.phase == "post"]

        async def invoke(index: int) -> ToolResult:
            if index == len(pre):
                return await tool.execute(
                    self.context,
                    tool_call_id,
                    arguments,
                    on_update,
                )
            current = pre[index]
            return await current.handle(
                tool=tool,
                context=self.context,
                tool_call_id=tool_call_id,
                arguments=arguments,
                call_next=lambda: invoke(index + 1),
            )

        try:
            result = await invoke(0)
            for current in post:
                async def current_result(value=result) -> ToolResult:
                    return value

                result = await current.handle(
                    tool=tool,
                    context=self.context,
                    tool_call_id=tool_call_id,
                    arguments=arguments,
                    call_next=current_result,
                )
            return result
        except Exception as exc:
            return ToolResult(
                content=f"{type(exc).__name__}: {exc}",
                is_error=True,
            )

    def can_run_parallel(self, name: str) -> bool:
        """按 Registry 中的 Capability 判断工具是否可并行。"""
        try:
            return can_run_parallel(self.registry.resolve(name))
        except LookupError:
            return False
