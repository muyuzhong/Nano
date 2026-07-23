from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from lion_code.tooling.context import ToolContext
from lion_code.tooling.middleware import PermissionMiddleware
from lion_code.tooling.permission import PermissionPolicy, reset_permission_cache
from lion_code.tooling.registry import ToolRegistry
from lion_code.tooling.runtime import ToolRuntime
from lion_code.tooling.types import LionTool, ToolCapabilities, ToolResult


def _tool(name: str, capabilities: ToolCapabilities, executed: list[str]) -> LionTool:
    async def execute(_context, _tool_call_id, _arguments, _on_update):
        executed.append(name)
        return ToolResult(content="executed")

    return LionTool(
        name=name,
        label=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        execute_fn=execute,
        capabilities=capabilities,
    )


def _runtime(tool, *, mode, policy, confirm_fn=None):
    registry = ToolRegistry()
    registry.register(tool)
    context = ToolContext(
        session_id="session",
        cwd=policy.cwd,
        controller=object(),
        registry=registry,
        permission_mode=mode,
        plan_file_path=None,
        read_file_state={},
        confirm_fn=confirm_fn,
    )
    return ToolRuntime(registry, context, [PermissionMiddleware(policy)]), context


class TestPermissionMiddleware(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        reset_permission_cache()

    async def test_explicit_deny_beats_bypass(self):
        executed = []
        with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as cwd_dir:
            settings = Path(cwd_dir) / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text(
                json.dumps({"permissions": {"deny": ["run_shell"]}}),
                encoding="utf-8",
            )
            policy = PermissionPolicy(home=Path(home_dir), cwd=Path(cwd_dir))
            runtime, _ = _runtime(
                _tool("run_shell", ToolCapabilities(executes_process=True), executed),
                mode="bypassPermissions",
                policy=policy,
            )

            result = await runtime.execute(
                tool_call_id="call-1",
                name="run_shell",
                arguments={"command": "echo ok"},
            )

        self.assertTrue(result.is_error)
        self.assertEqual(executed, [])

    async def test_plan_mode_blocks_mutating_tool(self):
        executed = []
        policy = PermissionPolicy()
        runtime, context = _runtime(
            _tool("write_file", ToolCapabilities(mutates_workspace=True), executed),
            mode="plan",
            policy=policy,
        )
        context.plan_file_path = "plan.md"

        result = await runtime.execute(
            tool_call_id="call-1",
            name="write_file",
            arguments={"file_path": "other.md"},
        )

        self.assertTrue(result.is_error)
        self.assertEqual(executed, [])

    async def test_confirmation_runs_once_for_cached_reason(self):
        executed = []
        confirm = AsyncMock(return_value=True)
        policy = PermissionPolicy()
        runtime, _ = _runtime(
            _tool(
                "external",
                ToolCapabilities(requires_confirmation=True),
                executed,
            ),
            mode="default",
            policy=policy,
            confirm_fn=confirm,
        )

        for call_id in ("call-1", "call-2"):
            result = await runtime.execute(
                tool_call_id=call_id,
                name="external",
                arguments={},
            )
            self.assertFalse(result.is_error)

        confirm.assert_awaited_once()
        self.assertEqual(executed, ["external", "external"])


if __name__ == "__main__":
    unittest.main()
