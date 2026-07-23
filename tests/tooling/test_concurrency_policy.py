from __future__ import annotations

import unittest

from lion_code.tooling.middleware import can_run_parallel
from lion_code.tooling.types import LionTool, ToolCapabilities, ToolResult


async def _execute(_context, _tool_call_id, _arguments, _on_update):
    return ToolResult(content="ok")


def _tool(*, mode="parallel", **capabilities):
    return LionTool(
        name="tool",
        label="tool",
        description="tool",
        parameters={"type": "object", "properties": {}},
        execute_fn=_execute,
        capabilities=ToolCapabilities(**capabilities),
        execution_mode=mode,
    )


class TestConcurrencyPolicy(unittest.TestCase):
    def test_only_read_safe_tools_run_parallel(self):
        self.assertTrue(can_run_parallel(_tool(
            read_only=True,
            concurrency_safe=True,
        )))
        self.assertFalse(can_run_parallel(_tool(
            read_only=False,
            concurrency_safe=True,
        )))
        self.assertFalse(can_run_parallel(_tool(
            read_only=True,
            concurrency_safe=False,
        )))
        self.assertFalse(can_run_parallel(_tool(
            mode="sequential",
            read_only=True,
            concurrency_safe=True,
        )))


if __name__ == "__main__":
    unittest.main()
