from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from lion_code.agent import Agent
from lion_code.tooling.types import LionTool, ToolResult


async def _execute(_context, _tool_call_id, _arguments, _on_update):
    return ToolResult(content="ok")


def _tool(name: str) -> LionTool:
    return LionTool(
        name=name,
        label=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        execute_fn=_execute,
    )


class _ChildAgent:
    created_with = None

    def __init__(self, **kwargs):
        type(self).created_with = kwargs
        self.run_once = AsyncMock(
            return_value={
                "text": "skill result",
                "tokens": {"input": 1, "output": 2},
            }
        )
        self.close = AsyncMock()


class TestSkillRegistryView(unittest.IsolatedAsyncioTestCase):
    async def test_fork_skill_selects_parent_registry_including_mcp(self):
        with patch("lion_code.agent.load_pre_tool_use_hooks", return_value=[]):
            parent = Agent(api_key="test-key")
        mcp_name = "mcp__docs__search"
        mcp_tool = _tool(mcp_name)
        parent.tool_registry.register(mcp_tool)
        skill_result = {
            "context": "fork",
            "allowed_tools": [mcp_name],
            "prompt": "Use the MCP search tool.",
        }

        with (
            patch("lion_code.skills.execute_skill", return_value=skill_result),
            patch("lion_code.agent.Agent", _ChildAgent),
            patch("lion_code.agent.print_sub_agent_start"),
            patch("lion_code.agent.print_sub_agent_end"),
        ):
            result = await parent._execute_skill_tool(
                {"skill_name": "research", "args": "find docs"}
            )

        kwargs = _ChildAgent.created_with
        child_registry = kwargs["tool_registry"]
        self.assertEqual(result, "skill result")
        self.assertEqual(
            [tool.name for tool in child_registry.all_tools()],
            [mcp_name],
        )
        self.assertIs(child_registry.resolve(mcp_name), mcp_tool)
        self.assertIs(
            kwargs["tool_environment"].mcp_manager,
            parent.tool_environment.mcp_manager,
        )
        self.assertFalse(kwargs["tool_environment"].owns_mcp_manager)


if __name__ == "__main__":
    unittest.main()
