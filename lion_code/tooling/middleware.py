"""ToolRuntime 的固定横切执行管线。"""

from __future__ import annotations

import inspect
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol

from ..hooks import HookOutcome, run_pre_tool_use_hooks
from .context import ToolContext
from .permission import PermissionDecision, PermissionPolicy
from .result_store import ResultStore
from .types import JSONValue, LionTool, ToolResult


NextCall = Callable[[], Awaitable[ToolResult]]


class ToolMiddleware(Protocol):
    """统一 Middleware 契约；post 阶段按声明顺序处理执行结果。"""

    phase: Literal["pre", "post"]

    async def handle(
        self,
        *,
        tool: LionTool,
        context: ToolContext,
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        call_next: NextCall,
    ) -> ToolResult: ...


class CancellationMiddleware:
    phase: Literal["pre"] = "pre"

    async def handle(self, *, context, call_next, **_):
        if context.cancellation_fn and context.cancellation_fn():
            return ToolResult(content="Tool call cancelled.", is_error=True)
        return await call_next()


class PreToolHookMiddleware:
    phase: Literal["pre"] = "pre"

    async def handle(
        self,
        *,
        tool,
        context,
        arguments,
        call_next,
        **_,
    ):
        if not context.hooks:
            return await call_next()

        hook_chain = await run_pre_tool_use_hooks(
            context.hooks,
            tool.name,
            dict(arguments),
            confirm_trust=context.confirm_hook_trust,
        )
        if hook_chain.outcome is HookOutcome.ALLOW:
            return await call_next()

        terminal = hook_chain.terminal_result
        if terminal is None:
            return ToolResult(
                content=(
                    "Tool call blocked because the hook system returned an invalid result.\n\n"
                    "The hook system failed. Do not interpret this as user intent."
                ),
                is_error=True,
            )
        reason = terminal.reason or "No reason was provided."
        if terminal.outcome is HookOutcome.DENY:
            content = (
                f'Action denied by hook "{terminal.hook_id}":\n{reason}\n\n'
                "A configured policy rejected this action. Adjust the action."
            )
        else:
            content = (
                f'Tool call blocked because hook "{terminal.hook_id}" failed:\n'
                f"{reason}\n\n"
                "The hook system failed. Do not interpret this as user intent."
            )
        return ToolResult(content=content, is_error=True)


class PermissionMiddleware:
    phase: Literal["pre"] = "pre"

    def __init__(self, policy: PermissionPolicy):
        self.policy = policy

    async def _decision(
        self,
        tool: LionTool,
        context: ToolContext,
        arguments: Mapping[str, JSONValue],
    ) -> PermissionDecision:
        hard = self.policy.check_hard_boundaries(
            tool=tool,
            arguments=arguments,
            mode=context.permission_mode,
            plan_file_path=context.plan_file_path,
        )
        if hard is not None:
            return hard

        if context.permission_mode != "auto":
            return self.policy.check(
                tool=tool,
                arguments=arguments,
                mode=context.permission_mode,
                plan_file_path=context.plan_file_path,
            )

        if is_auto_fast_path(tool):
            return PermissionDecision("allow")
        if context.auto_permission_fn is None:
            return PermissionDecision(
                "deny",
                f"{tool.name} (auto-mode classifier unavailable)",
            )
        raw = await context.auto_permission_fn(tool.name, arguments)
        action = raw.get("action") if isinstance(raw, dict) else None
        if action not in {"allow", "deny", "confirm"}:
            return PermissionDecision("deny", "Invalid auto-mode permission result")
        return PermissionDecision(action, str(raw.get("message", "")))

    async def handle(
        self,
        *,
        tool,
        context,
        arguments,
        call_next,
        **_,
    ):
        decision = await self._decision(tool, context, arguments)
        if decision.action == "deny":
            return ToolResult(
                content=f"Action denied: {decision.message}",
                is_error=True,
            )

        if decision.action == "confirm":
            if context.confirm_fn is None:
                return ToolResult(
                    content="Confirmation unavailable.",
                    is_error=True,
                )
            cacheable = context.permission_mode != "auto"
            if not cacheable or decision.message not in context.confirmed_paths:
                approved = await context.confirm_fn(decision.message)
                if not approved:
                    return ToolResult(
                        content="User denied this action.",
                        is_error=True,
                    )
                if cacheable:
                    context.confirmed_paths.add(decision.message)

        return await call_next()


def _resolve_file_path(context: ToolContext, raw_path: object) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = context.cwd / path
    try:
        return path.resolve()
    except (OSError, ValueError):
        return None


class ReadFreshnessMiddleware:
    phase: Literal["pre"] = "pre"

    async def handle(
        self,
        *,
        tool,
        context,
        arguments,
        call_next,
        **_,
    ):
        path = _resolve_file_path(context, arguments.get("file_path"))
        capabilities = tool.capabilities

        if capabilities.requires_read_before_write and path and path.exists():
            key = str(path)
            if key not in context.read_file_state:
                return ToolResult(
                    content=(
                        "Error: You must read this file before editing. "
                        "Use read_file first to see its current contents."
                    ),
                    is_error=True,
                )
            try:
                current_mtime = os.path.getmtime(path)
            except OSError:
                current_mtime = None
            if current_mtime != context.read_file_state[key]:
                return ToolResult(
                    content=(
                        f"Warning: {arguments.get('file_path')} was modified externally "
                        "since your last read. Please read_file again before editing."
                    ),
                    is_error=True,
                )

        result = await call_next()
        if result.is_error or path is None:
            return result

        if (
            capabilities.tracks_read_freshness
            or capabilities.requires_read_before_write
        ):
            try:
                context.read_file_state[str(path)] = os.path.getmtime(path)
            except OSError:
                pass
        return result


class ResultPolicyMiddleware:
    phase: Literal["post"] = "post"

    def __init__(self, store: ResultStore):
        self.store = store

    async def handle(self, *, tool, call_next, **_):
        return self.store.process(tool, await call_next())


class AuditMiddleware:
    phase: Literal["post"] = "post"

    async def handle(
        self,
        *,
        tool,
        context,
        arguments,
        call_next,
        **_,
    ):
        result = await call_next()
        if context.audit_fn:
            audit_result = context.audit_fn(tool, arguments, result)
            if inspect.isawaitable(audit_result):
                await audit_result
        return result


def can_run_parallel(tool: LionTool) -> bool:
    """只有显式声明为并发安全的只读工具可并行。"""
    return (
        tool.execution_mode == "parallel"
        and tool.capabilities.concurrency_safe
        and tool.capabilities.read_only
    )


def is_auto_fast_path(tool: LionTool) -> bool:
    """Auto Mode 仅跳过无外部副作用的只读工具分类。"""
    return tool.capabilities.read_only and not tool.capabilities.external_side_effect
