from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from lion_code.tooling.context import ToolContext
from lion_code.tooling.middleware import ReadFreshnessMiddleware
from lion_code.tooling.registry import ToolRegistry
from lion_code.tooling.runtime import ToolRuntime
from lion_code.tooling.types import LionTool, ToolCapabilities, ToolResult


def _tool(name, capabilities, execute):
    return LionTool(
        name=name,
        label=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        execute_fn=execute,
        capabilities=capabilities,
    )


class TestReadFreshness(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, directory, tools):
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        context = ToolContext(
            session_id="session",
            cwd=Path(directory),
            controller=object(),
            registry=registry,
            permission_mode="default",
            plan_file_path=None,
            read_file_state={},
        )
        return ToolRuntime(registry, context, [ReadFreshnessMiddleware()]), context

    async def test_edit_requires_read(self):
        executed = []

        async def edit(_context, _call_id, _arguments, _on_update):
            executed.append(True)
            return ToolResult(content="edited")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.txt"
            path.write_text("before", encoding="utf-8")
            runtime, _ = self._runtime(
                directory,
                [_tool(
                    "edit_file",
                    ToolCapabilities(
                        mutates_workspace=True,
                        requires_read_before_write=True,
                    ),
                    edit,
                )],
            )

            result = await runtime.execute(
                tool_call_id="call-1",
                name="edit_file",
                arguments={"file_path": str(path)},
            )

        self.assertTrue(result.is_error)
        self.assertIn("must read this file", result.content)
        self.assertEqual(executed, [])

    async def test_external_modification_requires_reread(self):
        async def read(_context, _call_id, arguments, _on_update):
            return ToolResult(
                content=Path(str(arguments["file_path"])).read_text(encoding="utf-8")
            )

        async def edit(_context, _call_id, _arguments, _on_update):
            return ToolResult(content="edited")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.txt"
            path.write_text("before", encoding="utf-8")
            runtime, context = self._runtime(
                directory,
                [
                    _tool(
                        "read_file",
                        ToolCapabilities(
                            read_only=True,
                            tracks_read_freshness=True,
                        ),
                        read,
                    ),
                    _tool(
                        "edit_file",
                        ToolCapabilities(
                            mutates_workspace=True,
                            requires_read_before_write=True,
                        ),
                        edit,
                    ),
                ],
            )
            await runtime.execute(
                tool_call_id="call-1",
                name="read_file",
                arguments={"file_path": str(path)},
            )
            previous = context.read_file_state[str(path.resolve())]
            os.utime(path, (previous + 5, previous + 5))

            result = await runtime.execute(
                tool_call_id="call-2",
                name="edit_file",
                arguments={"file_path": str(path)},
            )

        self.assertTrue(result.is_error)
        self.assertIn("modified externally", result.content)


if __name__ == "__main__":
    unittest.main()
