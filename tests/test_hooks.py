"""PreToolUse Command Hook 的配置、协议和执行边界测试。"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from lion_code import hooks as hook_module
from lion_code.agent import Agent
from lion_code.hooks import load_pre_tool_use_hooks, run_pre_tool_use_hooks


def _python_command(script: Path) -> str:
    args = [sys.executable, str(script)]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def _hook(command: str, *, matcher: str = "*", timeout_ms: float = 1000) -> dict:
    return {
        "matcher": matcher,
        "command": command,
        "timeout_ms": timeout_ms,
        "label": "test-hook",
    }


class TestHookConfig(unittest.TestCase):
    def test_loads_user_hooks_before_project_hooks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            project = root / "project"
            user_settings = home / ".claude" / "settings.json"
            project_settings = project / ".claude" / "settings.json"
            user_settings.parent.mkdir(parents=True)
            project_settings.parent.mkdir(parents=True)
            user_settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {"matcher": "run_*", "command": "user-hook"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            project_settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "write_file",
                                    "command": "project-hook",
                                    "timeout_ms": 250,
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(hook_module.Path, "home", return_value=home),
                patch.object(hook_module.Path, "cwd", return_value=project),
            ):
                loaded = load_pre_tool_use_hooks()

        self.assertEqual([hook["command"] for hook in loaded], ["user-hook", "project-hook"])
        self.assertEqual(loaded[0]["timeout_ms"], 5000.0)
        self.assertEqual(loaded[1]["timeout_ms"], 250.0)

    def test_rejects_hook_without_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = root / ".claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                json.dumps({"hooks": {"PreToolUse": [{"matcher": "run_shell"}]}}),
                encoding="utf-8",
            )

            with (
                patch.object(hook_module.Path, "home", return_value=root / "home"),
                patch.object(hook_module.Path, "cwd", return_value=root),
            ):
                with self.assertRaisesRegex(ValueError, "command"):
                    load_pre_tool_use_hooks()


class TestCommandHooks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self._cwd_patch = patch.object(hook_module.Path, "cwd", return_value=self.root)
        self._cwd_patch.start()

    def tearDown(self):
        self._cwd_patch.stop()
        self._temp_dir.cleanup()

    def _write_script(self, name: str, source: str) -> str:
        script = self.root / name
        script.write_text(source, encoding="utf-8")
        return _python_command(script)

    async def test_allow_hook_receives_utf8_payload(self):
        command = self._write_script(
            "allow.py",
            """import json
from pathlib import Path
import sys

payload = json.load(sys.stdin.buffer)
Path("payload.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"action": "allow"}))
""",
        )

        denial = await run_pre_tool_use_hooks(
            [_hook(command, matcher="run_*")],
            "run_shell",
            {"command": "echo 你好"},
        )

        self.assertIsNone(denial)
        payload = json.loads((self.root / "payload.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["event"], "PreToolUse")
        self.assertEqual(payload["tool_name"], "run_shell")
        self.assertEqual(payload["tool_input"], {"command": "echo 你好"})
        self.assertEqual(payload["cwd"], str(self.root))

    async def test_non_matching_hook_is_not_started(self):
        denial = await run_pre_tool_use_hooks(
            [_hook("command-that-does-not-exist", matcher="write_file")],
            "run_shell",
            {"command": "echo hi"},
        )
        self.assertIsNone(denial)

    async def test_deny_stops_later_hooks(self):
        deny = self._write_script(
            "deny.py",
            'print("{\\"action\\":\\"deny\\",\\"reason\\":\\"blocked\\"}")',
        )
        later = self._write_script(
            "later.py",
            'from pathlib import Path\nPath("later-ran").touch()\nprint("{\\"action\\":\\"allow\\"}")',
        )

        denial = await run_pre_tool_use_hooks(
            [_hook(deny), _hook(later)],
            "run_shell",
            {"command": "echo hi"},
        )

        self.assertEqual(denial, "blocked")
        self.assertFalse((self.root / "later-ran").exists())

    async def test_process_failures_are_denied(self):
        invalid_json = self._write_script("invalid.py", 'print("not json")')
        nonzero = self._write_script(
            "nonzero.py",
            'import sys\nsys.stderr.write("boom")\nsys.exit(7)',
        )

        invalid_result = await run_pre_tool_use_hooks(
            [_hook(invalid_json)], "run_shell", {"command": "echo hi"}
        )
        nonzero_result = await run_pre_tool_use_hooks(
            [_hook(nonzero)], "run_shell", {"command": "echo hi"}
        )

        self.assertIn("invalid JSON", invalid_result)
        self.assertIn("exited with code 7: boom", nonzero_result)

    async def test_timeout_kills_process_tree_promptly(self):
        command = self._write_script(
            "sleep.py",
            "import time\ntime.sleep(10)\n",
        )
        started = time.monotonic()

        denial = await run_pre_tool_use_hooks(
            [_hook(command, timeout_ms=50)],
            "run_shell",
            {"command": "echo hi"},
        )

        self.assertIn("timed out after 50ms", denial)
        self.assertLess(time.monotonic() - started, 2)

    async def test_cancellation_reaps_process_tree(self):
        command = self._write_script(
            "cancel.py",
            "import time\ntime.sleep(10)\n",
        )
        task = asyncio.create_task(
            run_pre_tool_use_hooks(
                [_hook(command, timeout_ms=5000)],
                "run_shell",
                {"command": "echo hi"},
            )
        )
        await asyncio.sleep(0.1)
        started = time.monotonic()

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertLess(time.monotonic() - started, 2)


class TestAgentHookIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_hook_denial_stops_tool_router(self):
        with patch("lion_code.agent.load_pre_tool_use_hooks", return_value=[]):
            agent = Agent(api_key="test-key")

        hook_runner = AsyncMock(return_value="blocked by policy")
        tool_runner = AsyncMock(return_value="executed")
        with (
            patch("lion_code.agent.run_pre_tool_use_hooks", hook_runner),
            patch("lion_code.agent.execute_tool", tool_runner),
        ):
            result = await agent._execute_tool_call("run_shell", {"command": "echo hi"})

        self.assertEqual(result, "Action denied by PreToolUse hook: blocked by policy")
        hook_runner.assert_awaited_once()
        tool_runner.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
