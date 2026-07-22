"""Command Hook 配置加载与执行。"""

from __future__ import annotations

import asyncio
import fnmatch
import json
from pathlib import Path
from typing import Any


DEFAULT_HOOK_TIMEOUT_MS = 5000
MAX_HOOK_OUTPUT_BYTES = 64 * 1024
MAX_HOOK_ERROR_BYTES = 4096

HookConfig = dict[str, Any]


def _read_settings(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid settings file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Settings file must contain a JSON object: {path}")
    return value


def load_pre_tool_use_hooks() -> list[HookConfig]:
    """按用户级、项目级顺序加载并校验 PreToolUse Hook。"""
    loaded: list[HookConfig] = []
    paths = [
        Path.home() / ".claude" / "settings.json",
        Path.cwd() / ".claude" / "settings.json",
    ]

    for path in paths:
        settings = _read_settings(path)
        hooks = settings.get("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError(f"hooks must be a JSON object: {path}")
        entries = hooks.get("PreToolUse", [])
        if not isinstance(entries, list):
            raise ValueError(f"hooks.PreToolUse must be a JSON array: {path}")

        for index, entry in enumerate(entries):
            label = f"{path}:hooks.PreToolUse[{index}]"
            if not isinstance(entry, dict):
                raise ValueError(f"Hook must be a JSON object: {label}")

            matcher = entry.get("matcher", "*")
            command = entry.get("command")
            timeout_ms = entry.get("timeout_ms", DEFAULT_HOOK_TIMEOUT_MS)
            if not isinstance(matcher, str) or not matcher:
                raise ValueError(f"Hook matcher must be a non-empty string: {label}")
            if not isinstance(command, str) or not command.strip():
                raise ValueError(f"Hook command must be a non-empty string: {label}")
            if (
                isinstance(timeout_ms, bool)
                or not isinstance(timeout_ms, (int, float))
                or timeout_ms <= 0
            ):
                raise ValueError(f"Hook timeout_ms must be a positive number: {label}")

            loaded.append(
                {
                    "matcher": matcher,
                    "command": command,
                    "timeout_ms": float(timeout_ms),
                    "label": label,
                }
            )

    return loaded


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await process.communicate()
    except (BrokenPipeError, ConnectionResetError):
        await process.wait()


def _decode_diagnostic(data: bytes) -> str:
    return data[:MAX_HOOK_ERROR_BYTES].decode("utf-8", errors="replace").strip()


async def _run_command_hook(hook: HookConfig, payload: bytes, cwd: Path) -> str | None:
    label = hook["label"]
    try:
        process = await asyncio.create_subprocess_shell(
            hook["command"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )
    except Exception as exc:
        return f"{label} failed to start: {exc}"

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload),
            timeout=hook["timeout_ms"] / 1000,
        )
    except TimeoutError:
        await _kill_and_reap(process)
        return f"{label} timed out after {hook['timeout_ms']:g}ms"
    except asyncio.CancelledError:
        await _kill_and_reap(process)
        raise

    if process.returncode != 0:
        detail = _decode_diagnostic(stderr)
        suffix = f": {detail}" if detail else ""
        return f"{label} exited with code {process.returncode}{suffix}"
    if len(stdout) > MAX_HOOK_OUTPUT_BYTES:
        return f"{label} produced more than {MAX_HOOK_OUTPUT_BYTES} bytes"

    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"{label} returned invalid JSON: {exc}"
    if not isinstance(result, dict):
        return f"{label} must return a JSON object"

    action = result.get("action")
    if action == "allow":
        return None
    if action == "deny":
        reason = result.get("reason")
        return reason if isinstance(reason, str) and reason.strip() else f"{label} denied the tool call"
    return f"{label} returned unsupported action: {action!r}"


async def run_pre_tool_use_hooks(
    hooks: list[HookConfig],
    tool_name: str,
    tool_input: dict,
) -> str | None:
    """顺序执行匹配 Hook；返回拒绝原因，全部放行时返回 None。"""
    cwd = Path.cwd()
    payload = json.dumps(
        {
            "event": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cwd": str(cwd),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    for hook in hooks:
        if not fnmatch.fnmatchcase(tool_name, hook["matcher"]):
            continue
        denial = await _run_command_hook(hook, payload, cwd)
        if denial is not None:
            return denial
    return None
