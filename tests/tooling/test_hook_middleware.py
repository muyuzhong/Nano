from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from lion_code.hooks import HookChainResult, HookOutcome, HookResult
from lion_code.tooling.context import ToolContext
from lion_code.tooling.middleware import PermissionMiddleware, PreToolHookMiddleware
from lion_code.tooling.registry import ToolRegistry
from lion_code.tooling.runtime import ToolRuntime
from lion_code.tooling.types import LionTool, ToolResult


class _FailingPolicy:
    def check_hard_boundaries(self, **_):
        raise AssertionError("permission should not run after hook denial")


class TestHookMiddleware(unittest.IsolatedAsyncioTestCase):
    async def test_hook_blocks_before_permission_execution(self):
        executed = []

        async def execute(_context, _call_id, _arguments, _on_update):
            executed.append(True)
            return ToolResult(content="executed")

        tool = LionTool(
            name="run_shell",
            label="run_shell",
            description="run_shell",
            parameters={"type": "object", "properties": {}},
            execute_fn=execute,
        )
        registry = ToolRegistry()
        registry.register(tool)
        context = ToolContext(
            session_id="session",
            cwd=Path.cwd(),
            controller=object(),
            registry=registry,
            permission_mode="default",
            plan_file_path=None,
            read_file_state={},
            hooks=[object()],
        )
        terminal = HookResult(
            hook_id="deny",
            outcome=HookOutcome.DENY,
            reason="blocked",
        )
        hook_runner = AsyncMock(
            return_value=HookChainResult(
                outcome=HookOutcome.DENY,
                terminal_result=terminal,
                executed=(terminal,),
            )
        )
        runtime = ToolRuntime(
            registry,
            context,
            [PreToolHookMiddleware(), PermissionMiddleware(_FailingPolicy())],
        )

        with patch(
            "lion_code.tooling.middleware.run_pre_tool_use_hooks",
            hook_runner,
        ):
            result = await runtime.execute(
                tool_call_id="call-1",
                name="run_shell",
                arguments={"command": "echo hi"},
            )

        self.assertTrue(result.is_error)
        self.assertIn("Action denied by hook", result.content)
        self.assertEqual(executed, [])


if __name__ == "__main__":
    unittest.main()
