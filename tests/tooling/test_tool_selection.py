from __future__ import annotations

import unittest
from unittest.mock import patch

from lion_code.subagent import get_sub_agent_config
from lion_code.tooling.registry import ToolRegistry
from lion_code.tooling.selection import ToolSelectionPolicy, select_tools
from lion_code.tooling.types import LionTool, ToolCapabilities, ToolResult


async def _execute(_context, _tool_call_id, _arguments, _on_update):
    return ToolResult(content="ok")


def _tool(
    name: str,
    *,
    read_only: bool = False,
    deferred: bool = False,
) -> LionTool:
    return LionTool(
        name=name,
        label=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        execute_fn=_execute,
        capabilities=ToolCapabilities(
            read_only=read_only,
            deferred=deferred,
        ),
    )


class TestToolSelection(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.registry.register(_tool("read_file", read_only=True))
        self.registry.register(_tool("write_file"))
        self.registry.register(_tool("agent"))
        self.registry.register(_tool("deferred_read", read_only=True, deferred=True))

    def test_explore_agent_receives_only_read_tools(self):
        child = select_tools(
            self.registry,
            get_sub_agent_config("explore").tool_policy,
        )

        self.assertEqual(
            [tool.name for tool in child.all_tools()],
            ["read_file", "deferred_read"],
        )

    def test_general_agent_cannot_spawn_nested_agent(self):
        child = select_tools(
            self.registry,
            get_sub_agent_config("general").tool_policy,
        )

        with self.assertRaises(LookupError):
            child.resolve("agent")

    def test_child_registry_activation_is_isolated(self):
        child = select_tools(self.registry, ToolSelectionPolicy())

        child.activate("deferred_read")

        self.assertTrue(child.is_active("deferred_read"))
        self.assertFalse(self.registry.is_active("deferred_read"))
        self.assertIs(
            child.resolve("deferred_read"),
            self.registry.resolve("deferred_read"),
        )

    def test_allowed_names_and_exclusions_are_combined(self):
        child = select_tools(
            self.registry,
            ToolSelectionPolicy(
                allowed_names=frozenset({"read_file", "agent"}),
                exclude_names=frozenset({"agent"}),
            ),
        )

        self.assertEqual(
            [tool.name for tool in child.all_tools()],
            ["read_file"],
        )

    def test_custom_agent_allowed_tools_support_mcp_names(self):
        mcp_name = "mcp__docs__search"
        self.registry.register(_tool(mcp_name))
        custom = {
            "custom": {
                "allowed_tools": [mcp_name],
                "system_prompt": "custom prompt",
            }
        }

        with patch("lion_code.subagent._discover_custom_agents", return_value=custom):
            config = get_sub_agent_config("custom")
        child = select_tools(self.registry, config.tool_policy)

        self.assertEqual(
            [tool.name for tool in child.all_tools()],
            [mcp_name],
        )


if __name__ == "__main__":
    unittest.main()
