"""Lion Code 上下文管理最小正式评测。

默认只执行离线验收；传入 ``--online`` 才会调用 OpenAI 兼容 API。密钥只从
环境变量读取，结果、报告和日志均不保存密钥。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import patch

from openai import OpenAI


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark import (  # noqa: E402
    Agent,
    Budget,
    EventCounts,
    MICROCOMPACT_IDLE_S,
    Price,
    api_call,
    apply_pipeline,
    compact_with_metrics,
    expand_to_bytes,
    ratio,
    source_snapshot,
    sum_usage,
    tool_content_stats,
    usage_cost,
)
from formal_tasks import (  # noqa: E402
    FIXTURES,
    evaluate_files,
    render_files,
    validate_fixture_catalog,
)
from lion_code.agent import _get_context_window  # noqa: E402


DATASET_PATH = HERE / "formal_dataset.json"
DEFAULT_RESULT_PATH = HERE / "results" / "formal-latest.json"
DEFAULT_REPORT_PATH = HERE / "REPORT_CN.md"
POLICIES = ("summary_only", "managed_eager", "managed")
READ_FILE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取评测工作区文件。",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    }
]


@dataclass
class FormalResult:
    task_id: str
    task_name: str
    category: str
    load_band: str
    policy: str
    repeat: int
    session_id: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    events: EventCounts = field(default_factory=EventCounts)
    quality: dict[str, Any] = field(default_factory=dict)

    def totals(self) -> dict[str, Any]:
        usage = sum_usage(call["usage"] for call in self.calls)
        normal_calls = [call for call in self.calls if call["kind"] != "summary"]
        session_cost = sum(float(call["cost_cny"]) for call in self.calls)
        return {
            "call_count": len(self.calls),
            "summary_call_count": sum(call["kind"] == "summary" for call in self.calls),
            "all_usage": usage,
            "normal_usage": sum_usage(call["usage"] for call in normal_calls),
            "total_cost_cny": session_cost,
            "cache_hit_rate": ratio(usage["cache_hit_tokens"], usage["prompt_tokens"]),
            "peak_prompt_tokens": max(
                (call["usage"]["prompt_tokens"] for call in normal_calls), default=0
            ),
            "peak_utilization": max((call["utilization"] for call in normal_calls), default=0.0),
            "latency_seconds": sum(float(call["latency_seconds"]) for call in self.calls),
            "task_passed": bool(self.quality.get("passed")),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "category": self.category,
            "load_band": self.load_band,
            "policy": self.policy,
            "repeat": self.repeat,
            "session_id": self.session_id,
            "calls": self.calls,
            "events": self.events.as_dict(),
            "quality": self.quality,
            "totals": self.totals(),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lion Code 上下文管理最小正式评测")
    parser.add_argument("--online", action="store_true", help="运行真实 API 评测")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--budget-cny", type=float, default=15.0)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument(
        "--cache-settle-seconds",
        type=float,
        default=2.0,
        help="相邻请求间等待缓存前缀落盘的秒数",
    )
    parser.add_argument("--repeat-count", type=int)
    parser.add_argument("--only-task", action="append", default=[])
    parser.add_argument("--only-policy", action="append", default=[])
    parser.add_argument("--seed", type=int, default=20260723)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--unattributed-spend-cny",
        type=float,
        default=0.0,
        help="续跑前补记上次中断但尚未落入会话结果的实际费用",
    )
    return parser.parse_args()


def read_dataset() -> dict[str, Any]:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    tasks = dataset["tasks"]
    ids = [task["id"] for task in tasks]
    errors: list[str] = []
    if len(tasks) != 9 or len(set(ids)) != 9:
        errors.append("正式评测必须包含 9 个唯一任务")
    if tuple(dataset["policies"]) != POLICIES:
        errors.append("策略必须依次为 summary_only、managed_eager、managed")
    matrix = {(task["category"], task["load_band"]) for task in tasks}
    if len(matrix) != 9:
        errors.append("任务必须完整覆盖 3 类任务 × 3 个负载档")
    for task in tasks:
        if task["fixture_id"] not in FIXTURES:
            errors.append(f"{task['id']} 引用了未知 fixture")
        for rel in task["source_files"]:
            if not (ROOT / rel).is_file():
                errors.append(f"{task['id']} 的源码快照不存在：{rel}")
    fixture_checks = validate_fixture_catalog()
    for check in fixture_checks:
        if not check["initial_fails"]:
            errors.append(f"{check['fixture_id']} 的初始缺陷没有被验收脚本捕获")
        if not check["reference_passes"]:
            errors.append(f"{check['fixture_id']} 的参考修复未通过验收")
    return {
        "passed": not errors,
        "errors": errors,
        "task_count": len(tasks),
        "matrix_cells": len(matrix),
        "fixture_checks": fixture_checks,
    }


def select_tasks(dataset: dict[str, Any], requested: list[str]) -> list[dict[str, Any]]:
    tasks = list(dataset["tasks"])
    if not requested:
        return tasks
    allowed = set(requested)
    selected = [task for task in tasks if task["id"] in allowed]
    missing = allowed - {task["id"] for task in selected}
    if missing:
        raise SystemExit(f"未知任务：{', '.join(sorted(missing))}")
    return selected


def select_policies(requested: list[str]) -> list[str]:
    if not requested:
        return list(POLICIES)
    unknown = set(requested) - set(POLICIES)
    if unknown:
        raise SystemExit(f"未知策略：{', '.join(sorted(unknown))}")
    return [policy for policy in POLICIES if policy in requested]


def build_run_order(
    tasks: list[dict[str, Any]], policies: list[str], repeats: int, seed: int
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    order: list[dict[str, Any]] = []
    for repeat in range(1, repeats + 1):
        task_order = list(tasks)
        rng.shuffle(task_order)
        for task in task_order:
            policy_order = list(policies)
            rng.shuffle(policy_order)
            order.extend(
                {"task_id": task["id"], "policy": policy, "repeat": repeat}
                for policy in policy_order
            )
    return order


def make_tool_messages(task_id: str, turn: int | str, payload: str) -> list[dict[str, Any]]:
    call_id = f"call_{task_id}_{turn}"
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps(
                            {"file_path": f"fixture://{task_id}", "turn": turn},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": payload},
    ]


def fill_payload(prefix: str, seed: str, target_bytes: int, marker: str) -> str:
    prefix_bytes = len(prefix.encode("utf-8"))
    if prefix_bytes >= target_bytes:
        return prefix
    filler = expand_to_bytes(seed, target_bytes - prefix_bytes, marker)
    return prefix + filler


def record_api_call(
    client: OpenAI,
    result: FormalResult,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    budget: Budget,
    kind: str,
    turn: int,
    effective_window: int,
    response_format: dict[str, str] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
) -> Any:
    started = time.perf_counter()
    response, usage, cost = api_call(
        client,
        model=model,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        budget=budget,
        response_format=response_format,
        tool_choice=tool_choice,
    )
    result.calls.append(
        {
            "kind": kind,
            "turn": turn,
            "usage": usage,
            "cost_cny": cost,
            "effective_window_tokens": effective_window,
            "utilization": ratio(usage["prompt_tokens"], effective_window),
            "latency_seconds": time.perf_counter() - started,
        }
    )
    return response


def maybe_compact(
    client: OpenAI,
    agent: Agent,
    result: FormalResult,
    *,
    model: str,
    budget: Budget,
    turn: int,
) -> None:
    if agent.last_input_token_count <= agent.effective_window * 0.85:
        return
    started = time.perf_counter()
    before_count = len(result.calls)
    compact_with_metrics(client, agent, result, model=model, budget=budget, turn=turn)
    if len(result.calls) > before_count:
        result.calls[-1]["latency_seconds"] = time.perf_counter() - started


def apply_policy(agent: Agent, policy: str, result: FormalResult) -> None:
    if policy == "summary_only":
        return
    mapped = "eager_ablation" if policy == "managed_eager" else "managed"
    apply_pipeline(agent, mapped, result.events)


def persist_if_managed(agent: Agent, policy: str, payload: str, result: FormalResult) -> str:
    if policy == "summary_only":
        return payload
    persisted = agent._persist_large_result("read_file", payload)
    if persisted != payload:
        result.events.persisted_results += 1
    return persisted


def contract_for_turn(task: dict[str, Any], turn: int) -> str:
    value = task["contract_turns"].get(str(turn))
    if not value:
        return ""
    return f"\n必须精确保留并在最终修复中落实：{value}"


def run_session(
    client: OpenAI,
    task: dict[str, Any],
    *,
    policy: str,
    repeat: int,
    model: str,
    base_url: str,
    api_key: str,
    budget: Budget,
    effective_window: int,
    run_id: str,
    cache_settle_seconds: float,
) -> tuple[FormalResult, list[dict[str, Any]]]:
    fixture = FIXTURES[task["fixture_id"]]
    source_text, source_meta = source_snapshot(task["source_files"])
    fixture_text = render_files(fixture.initial_files)
    session_id = f"{run_id}-{task['id']}-r{repeat}-{policy}"
    result = FormalResult(
        task_id=task["id"],
        task_name=task["name"],
        category=task["category"],
        load_band=task["load_band"],
        policy=policy,
        repeat=repeat,
        session_id=session_id,
    )
    system_prompt = (
        f"[formal-session={session_id}]\n"
        "你正在参加 Lion Code 上下文管理的受控编码评测。中间计量点只返回 JSON："
        "{\"status\":\"继续\"}。"
        "最终验收时必须返回 JSON 对象，不得调用工具或使用 Markdown 代码块。"
    )
    agent = Agent(
        model=model,
        api_base=base_url,
        api_key=api_key,
        custom_system_prompt=system_prompt,
    )
    agent._openai_messages = [{"role": "system", "content": system_prompt}]
    agent.effective_window = effective_window

    with tempfile.TemporaryDirectory(prefix="lion-formal-home-") as temp_home:
        with patch("lion_code.agent.Path.home", return_value=Path(temp_home)):
            for turn in range(1, int(task["turns"]) + 1):
                user_seed = source_text + "\n" + fixture.failure_log
                user_payload = expand_to_bytes(
                    user_seed,
                    int(task["user_payload_bytes"]),
                    f"{task['id']}-user-r{repeat}-t{turn}",
                )
                user_text = (
                    f"第 {turn} 轮：继续分析当前项目中的上下文管理缺陷。"
                    f"{contract_for_turn(task, turn)}\n以下是本轮设计与排障记录：\n{user_payload}"
                )
                agent._openai_messages.append({"role": "user", "content": user_text})
                maybe_compact(
                    client,
                    agent,
                    result,
                    model=model,
                    budget=budget,
                    turn=turn,
                )

                target_bytes = int(task["tool_payload_bytes"])
                if turn in task.get("large_result_turns", []):
                    target_bytes = int(task["large_tool_payload_bytes"])
                prefix = (
                    f"验收目标：{task['acceptance']}\n"
                    f"失败日志：{fixture.failure_log}\n\n{fixture_text}\n\n"
                )
                tool_payload = fill_payload(
                    prefix,
                    source_text,
                    target_bytes,
                    f"{task['id']}-tool-r{repeat}-t{turn}",
                )
                tool_payload = persist_if_managed(agent, policy, tool_payload, result)
                agent._openai_messages.extend(make_tool_messages(task["id"], turn, tool_payload))
                agent._openai_messages.append(
                    {
                        "role": "user",
                        "content": "计量点：只返回 {\"status\":\"继续\"}，不要给出修复。",
                    }
                )
                apply_policy(agent, policy, result)

                checkpoint_response = record_api_call(
                    client,
                    result,
                    model=model,
                    messages=agent._openai_messages,
                    tools=READ_FILE_TOOL,
                    max_tokens=32,
                    budget=budget,
                    kind="turn",
                    turn=turn,
                    effective_window=effective_window,
                    response_format={"type": "json_object"},
                    tool_choice="none",
                )
                usage = result.calls[-1]["usage"]
                agent.last_input_token_count = usage["prompt_tokens"] + usage["completion_tokens"]
                agent.last_api_call_time = time.time()
                checkpoint_text = checkpoint_response.choices[0].message.content
                agent._openai_messages.append(
                    {
                        "role": "assistant",
                        "content": checkpoint_text or '{"status":"继续"}',
                    }
                )
                if cache_settle_seconds > 0:
                    time.sleep(cache_settle_seconds)

            agent._openai_messages.append(
                {"role": "user", "content": "准备最终验收，请先压缩必要上下文，再重新读取待修复文件。"}
            )
            maybe_compact(
                client,
                agent,
                result,
                model=model,
                budget=budget,
                turn=int(task["turns"]) + 1,
            )
            agent._openai_messages.extend(
                make_tool_messages(task["id"], "final", fixture_text)
            )
            allowed = sorted(fixture.allowed_files)
            agent._openai_messages.append(
                {
                    "role": "user",
                    "content": (
                        "现在完成修复。必须遵守此前会话中的全部契约。只返回一个 JSON 对象，"
                        "格式为 {\"contract_check\": [\"已落实的早期契约\"], "
                        "\"files\": {\"文件名\": \"完整文件内容\"}, \"note\": \"一句话说明\"}。"
                        "先在 contract_check 中逐条核对早期契约，再给出文件内容。"
                        f"只能修改这些文件：{', '.join(allowed)}。不要返回 Markdown。"
                    ),
                }
            )
            apply_policy(agent, policy, result)
            response = record_api_call(
                client,
                result,
                model=model,
                messages=agent._openai_messages,
                tools=READ_FILE_TOOL,
                max_tokens=4096,
                budget=budget,
                kind="solution",
                turn=int(task["turns"]) + 1,
                effective_window=effective_window,
                response_format={"type": "json_object"},
                tool_choice="none",
            )

    content = response.choices[0].message.content or ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        result.quality = {
            "passed": False,
            "stage": "response_json",
            "detail": str(exc),
            "response_excerpt": content[:4000],
        }
        return result, source_meta

    candidate_files = parsed.get("files") if isinstance(parsed, dict) else None
    if not isinstance(candidate_files, dict):
        result.quality = {
            "passed": False,
            "stage": "response_schema",
            "detail": "响应缺少 files 对象",
            "response": parsed,
        }
        return result, source_meta
    evaluation = evaluate_files(task["fixture_id"], candidate_files)
    result.quality = {
        **evaluation,
        "candidate_files": candidate_files,
        "contract_check": parsed.get("contract_check", []),
        "note": parsed.get("note", ""),
    }
    return result, source_meta


def _quartiles(values: list[float]) -> tuple[float, float]:
    if len(values) < 2:
        value = values[0] if values else 0.0
        return value, value
    qs = statistics.quantiles(values, n=4, method="inclusive")
    return float(qs[0]), float(qs[2])


def aggregate_items(items: list[dict[str, Any]], price: Price) -> dict[str, Any]:
    calls = [call for item in items for call in item["calls"]]
    usage = sum_usage(call["usage"] for call in calls)
    costs = [float(item["totals"]["total_cost_cny"]) for item in items]
    prompts = [float(item["totals"]["all_usage"]["prompt_tokens"]) for item in items]
    hit_rates = [float(item["totals"]["cache_hit_rate"]) for item in items]
    q1_cost, q3_cost = _quartiles(costs)
    q1_prompt, q3_prompt = _quartiles(prompts)
    input_usage = {**usage, "completion_tokens": 0}
    successes = sum(bool(item["quality"].get("passed")) for item in items)
    return {
        "session_count": len(items),
        "success_count": successes,
        "success_rate": ratio(successes, len(items)),
        "all_usage": usage,
        "input_cost_cny": usage_cost(input_usage, price),
        "total_cost_cny": sum(costs),
        "cost_per_success_cny": ratio(sum(costs), successes),
        "cache_hit_rate": ratio(usage["cache_hit_tokens"], usage["prompt_tokens"]),
        "median_session_cache_hit_rate": statistics.median(hit_rates) if hit_rates else 0.0,
        "median_session_cost_cny": statistics.median(costs) if costs else 0.0,
        "session_cost_iqr_cny": [q1_cost, q3_cost],
        "median_session_prompt_tokens": statistics.median(prompts) if prompts else 0.0,
        "session_prompt_iqr_tokens": [q1_prompt, q3_prompt],
        "peak_prompt_tokens": max(
            (int(item["totals"]["peak_prompt_tokens"]) for item in items), default=0
        ),
        "over_effective_window_calls": sum(
            call["usage"]["prompt_tokens"] > call["effective_window_tokens"] for call in calls
        ),
        "summary_call_count": sum(call["kind"] == "summary" for call in calls),
        "latency_seconds": sum(float(call["latency_seconds"]) for call in calls),
        "events": {
            key: sum(int(item["events"].get(key, 0)) for item in items)
            for key in EventCounts().as_dict()
        },
    }


def aggregate_results(results: list[dict[str, Any]], price: Price) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for policy in POLICIES:
        selected = [item for item in results if item["policy"] == policy]
        if not selected:
            continue
        item = aggregate_items(selected, price)
        item["by_load_band"] = {
            band: aggregate_items(
                [result for result in selected if result["load_band"] == band], price
            )
            for band in sorted({result["load_band"] for result in selected})
        }
        output[policy] = item
    return output


def _bootstrap_interval(values: list[float]) -> list[float]:
    if not values:
        return [0.0, 0.0]
    ordered = sorted(values)
    low = ordered[int((len(ordered) - 1) * 0.025)]
    high = ordered[int((len(ordered) - 1) * 0.975)]
    return [low, high]


def paired_bootstrap(
    results: list[dict[str, Any]],
    *,
    baseline: str,
    candidate: str,
    price: Price,
    seed: int,
    samples: int = 5000,
) -> dict[str, list[float]]:
    task_ids = sorted({item["task_id"] for item in results})
    grouped = {
        policy: {
            task_id: [
                item
                for item in results
                if item["policy"] == policy and item["task_id"] == task_id
            ]
            for task_id in task_ids
        }
        for policy in (baseline, candidate)
    }
    task_ids = [
        task_id
        for task_id in task_ids
        if grouped[baseline][task_id] and grouped[candidate][task_id]
    ]
    if not task_ids:
        return {}
    rng = random.Random(seed)
    prompt_reductions: list[float] = []
    cost_reductions: list[float] = []
    success_differences: list[float] = []
    cache_gains: list[float] = []
    for _ in range(samples):
        sampled = [rng.choice(task_ids) for _ in task_ids]
        base_items = [item for task_id in sampled for item in grouped[baseline][task_id]]
        candidate_items = [item for task_id in sampled for item in grouped[candidate][task_id]]
        base = aggregate_items(base_items, price)
        current = aggregate_items(candidate_items, price)
        prompt_reductions.append(
            1 - ratio(current["all_usage"]["prompt_tokens"], base["all_usage"]["prompt_tokens"])
        )
        cost_reductions.append(1 - ratio(current["total_cost_cny"], base["total_cost_cny"]))
        success_differences.append(current["success_rate"] - base["success_rate"])
        cache_gains.append(current["cache_hit_rate"] - base["cache_hit_rate"])
    return {
        "prompt_token_reduction_95ci": _bootstrap_interval(prompt_reductions),
        "total_cost_reduction_95ci": _bootstrap_interval(cost_reductions),
        "success_rate_difference_95ci": _bootstrap_interval(success_differences),
        "cache_hit_rate_gain_95ci": _bootstrap_interval(cache_gains),
    }


def comparison(
    aggregate: dict[str, Any], results: list[dict[str, Any]], price: Price, seed: int
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    if "summary_only" in aggregate and "managed" in aggregate:
        baseline = aggregate["summary_only"]
        managed = aggregate["managed"]
        output["managed_vs_summary_only"] = {
            "all_input_token_reduction": 1
            - ratio(managed["all_usage"]["prompt_tokens"], baseline["all_usage"]["prompt_tokens"]),
            "input_cost_reduction": 1
            - ratio(managed["input_cost_cny"], baseline["input_cost_cny"]),
            "total_cost_reduction": 1
            - ratio(managed["total_cost_cny"], baseline["total_cost_cny"]),
            "success_counts": {
                "summary_only": baseline["success_count"],
                "managed": managed["success_count"],
                "denominator_each": managed["session_count"],
            },
            "bootstrap": paired_bootstrap(
                results,
                baseline="summary_only",
                candidate="managed",
                price=price,
                seed=seed,
            ),
        }
    if "managed_eager" in aggregate and "managed" in aggregate:
        eager = aggregate["managed_eager"]
        managed = aggregate["managed"]
        output["managed_vs_eager"] = {
            "eager_cache_hit_rate": eager["cache_hit_rate"],
            "managed_cache_hit_rate": managed["cache_hit_rate"],
            "cache_hit_rate_gain_percentage_points": (
                managed["cache_hit_rate"] - eager["cache_hit_rate"]
            )
            * 100,
            "total_cost_reduction": 1
            - ratio(managed["total_cost_cny"], eager["total_cost_cny"]),
            "bootstrap": paired_bootstrap(
                results,
                baseline="managed_eager",
                candidate="managed",
                price=price,
                seed=seed + 1,
            ),
        }
    return output


def quality_guard(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_policy = {
        policy: [item for item in results if item["policy"] == policy] for policy in POLICIES
    }
    summary_success = sum(item["quality"].get("passed", False) for item in by_policy["summary_only"])
    managed_success = sum(item["quality"].get("passed", False) for item in by_policy["managed"])
    catastrophic_tasks: list[str] = []
    for task_id in sorted({item["task_id"] for item in results}):
        baseline = [
            item for item in by_policy["summary_only"] if item["task_id"] == task_id
        ]
        managed = [item for item in by_policy["managed"] if item["task_id"] == task_id]
        if baseline and managed and all(item["quality"].get("passed") for item in baseline) and not any(
            item["quality"].get("passed") for item in managed
        ):
            catastrophic_tasks.append(task_id)
    complete = all(by_policy[policy] for policy in POLICIES) and len(
        {len(by_policy[policy]) for policy in POLICIES}
    ) == 1
    passed = complete and managed_success >= summary_success and not catastrophic_tasks
    return {
        "passed": passed,
        "complete_balanced_run": complete,
        "managed_success_not_lower": managed_success >= summary_success,
        "catastrophic_task_regressions": catastrophic_tasks,
        "success_counts": {
            "summary_only": summary_success,
            "managed_eager": sum(
                item["quality"].get("passed", False) for item in by_policy["managed_eager"]
            ),
            "managed": managed_success,
        },
        "claim_rule": "仅当本护栏通过时，报告 managed 相对 summary_only 的成本降幅。",
    }


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_report(payload: dict[str, Any]) -> str:
    meta = payload["metadata"]
    lines = [
        "# Lion Code 上下文管理最小正式评测报告",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 模型：`{meta['model']}`，非思考模式",
        f"- API：`{meta['base_url']}`（OpenAI 兼容格式）",
        f"- 任务：{meta['selected_task_count']} 项；计划/完成会话：{meta['planned_session_count']} / {meta['completed_session_count']}",
        f"- 每项任务重复：{meta['repeat_count']} 次；有效窗口：{meta['effective_window_tokens']:,} token",
        f"- 实际计量费用：{meta['benchmark_spend_cny']:.4f} 元；硬上限：{meta['budget_limit_cny']:.2f} 元",
        "- API Key：只从进程环境读取，未写入任何评测文件。",
        "",
        "## 策略",
        "",
        "- `summary_only`：85% 水位触发一次全量摘要，不运行渐进式压缩。",
        "- `managed_eager`：运行四级管线，但在缓存仍热时提前裁剪旧前缀。",
        "- `managed`：运行生产四级管线，60%～75% 热缓存区间延迟改写前缀。",
    ]
    aggregate = payload.get("aggregate", {})
    if not aggregate:
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            "",
            "## 总体结果",
            "",
            "| 策略 | 成功任务 | 输入 token | 命中率 | 摘要调用 | 峰值输入 | API 费用 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for policy in POLICIES:
        if policy not in aggregate:
            continue
        item = aggregate[policy]
        lines.append(
            f"| `{policy}` | {item['success_count']}/{item['session_count']} | "
            f"{item['all_usage']['prompt_tokens']:,} | {pct(item['cache_hit_rate'])} | "
            f"{item['summary_call_count']} | {item['peak_prompt_tokens']:,} | "
            f"{item['total_cost_cny']:.4f} 元 |"
        )
    comp = payload.get("comparison", {})
    main = comp.get("managed_vs_summary_only")
    if main:
        counts = main["success_counts"]
        lines.extend(
            [
                "",
                "## 主对照：完整管线 vs 单阶段摘要",
                "",
                f"- 含摘要累计输入 token：减少 **{pct(main['all_input_token_reduction'])}**。",
                f"- 输入费用：减少 **{pct(main['input_cost_reduction'])}**。",
                f"- API 总费用：减少 **{pct(main['total_cost_reduction'])}**。",
                f"- 任务成功数：`managed` {counts['managed']}/{counts['denominator_each']}，"
                f"`summary_only` {counts['summary_only']}/{counts['denominator_each']}。",
            ]
        )
    cache = comp.get("managed_vs_eager")
    if cache:
        lines.extend(
            [
                "",
                "## 热缓存保护消融",
                "",
                f"- 提前改写前缀：{pct(cache['eager_cache_hit_rate'])}。",
                f"- 缓存热度感知：{pct(cache['managed_cache_hit_rate'])}。",
                f"- 命中率变化：{cache['cache_hit_rate_gain_percentage_points']:+.1f} 个百分点。",
                f"- API 费用变化：完整策略相对提前改写减少 {pct(cache['total_cost_reduction'])}。",
            ]
        )
    guard = payload.get("quality_guard", {})
    lines.extend(
        [
            "",
            "## 质量护栏",
            "",
            f"- 结果：{'通过' if guard.get('passed') else '未通过'}。",
            f"- 成功数：{json.dumps(guard.get('success_counts', {}), ensure_ascii=False)}。",
            f"- 基线两次成功、完整策略两次均失败的任务：{guard.get('catastrophic_task_regressions', [])}。",
            "- 18 次/策略时单次结果对应 5.6 个百分点；报告原始计数，不声称统计学非劣效。",
            "",
            "## 测量边界",
            "",
            "- 这是 9 项由当前仓库源码构造的受控编码任务，每项有独立可执行验收，不代表生产流量。",
            "- DeepSeek OpenAI 接口使用自动前缀缓存。本结果可以验证缓存热度感知，不能验证 Anthropic 显式双 cache_control 断点的收益。",
            "- 供应商缓存为尽力而为，报告同时保留逐会话原始 usage、负载档和配对 bootstrap 区间。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_payload(path: Path, report_path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(payload), encoding="utf-8")


def result_key(item: dict[str, Any]) -> tuple[str, str, int]:
    return item["task_id"], item["policy"], int(item["repeat"])


def initialize_payload(
    args: argparse.Namespace,
    dataset: dict[str, Any],
    validation: dict[str, Any],
    tasks: list[dict[str, Any]],
    policies: list[str],
    repeat_count: int,
    order: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "online": bool(args.online),
            "model": args.model,
            "base_url": args.base_url,
            "mode": "OpenAI-compatible, non-thinking, paired formal benchmark",
            "dataset_version": dataset["version"],
            "dataset_sha256": sha256_file(DATASET_PATH),
            "code_model_context_tokens": _get_context_window(args.model),
            "effective_window_tokens": int(dataset["effective_window_tokens"]),
            "provider_advertised_window_tokens": int(
                dataset["provider_advertised_window_tokens"]
            ),
            "selected_task_count": len(tasks),
            "repeat_count": repeat_count,
            "planned_session_count": len(order),
            "completed_session_count": 0,
            "budget_limit_cny": args.budget_cny,
            "benchmark_spend_cny": 0.0,
            "run_id": uuid.uuid4().hex[:12],
            "random_seed": args.seed,
            "cache_settle_seconds": args.cache_settle_seconds,
            "credential_persisted": False,
        },
        "pricing_cny_per_million_tokens": dataset[
            "pricing_cny_per_million_tokens"
        ],
        "pricing_source": dataset["pricing_source"],
        "load_band_basis": dataset["load_band_basis"],
        "validation": validation,
        "run_order": order,
        "source_snapshots": {},
        "session_results": [],
    }


def finalize_payload(payload: dict[str, Any], price: Price, seed: int) -> None:
    results = payload["session_results"]
    payload["metadata"]["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload["metadata"]["completed_session_count"] = len(results)
    aggregate = aggregate_results(results, price)
    payload["aggregate"] = aggregate
    payload["comparison"] = comparison(aggregate, results, price, seed)
    payload["quality_guard"] = quality_guard(results)


def main() -> int:
    args = parse_args()
    dataset = read_dataset()
    validation = validate_dataset(dataset)
    if not validation["passed"]:
        for error in validation["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(
        f"离线校验通过：{validation['task_count']} 项任务，"
        f"{validation['matrix_cells']} 个类型/负载组合，初始缺陷均失败且参考修复均通过。"
    )
    if not args.online:
        return 0

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f"缺少环境变量 {args.api_key_env}；密钥不会从文件或参数读取。")
    tasks = select_tasks(dataset, args.only_task)
    policies = select_policies(args.only_policy)
    repeat_count = args.repeat_count or int(dataset["repeat_count"])
    if repeat_count < 1:
        raise SystemExit("repeat-count 必须大于 0")
    order = build_run_order(tasks, policies, repeat_count, args.seed)
    price = Price(**dataset["pricing_cny_per_million_tokens"])

    if args.resume:
        if not args.output.is_file():
            raise SystemExit(f"续跑结果不存在：{args.output}")
        payload = json.loads(args.output.read_text(encoding="utf-8"))
        if payload["metadata"]["dataset_sha256"] != sha256_file(DATASET_PATH):
            raise SystemExit("数据集已变化，不能续跑旧结果")
        if payload["run_order"] != order:
            raise SystemExit("任务顺序或筛选条件已变化，不能续跑旧结果")
        if args.unattributed_spend_cny < 0:
            raise SystemExit("unattributed-spend-cny 不能为负数")
        if args.unattributed_spend_cny:
            payload["metadata"]["unattributed_partial_spend_cny"] = (
                float(payload["metadata"].get("unattributed_partial_spend_cny", 0.0))
                + args.unattributed_spend_cny
            )
            payload["metadata"]["benchmark_spend_cny"] = (
                float(payload["metadata"].get("benchmark_spend_cny", 0.0))
                + args.unattributed_spend_cny
            )
        payload["metadata"]["budget_limit_cny"] = args.budget_cny
    else:
        payload = initialize_payload(
            args, dataset, validation, tasks, policies, repeat_count, order
        )

    budget = Budget(args.budget_cny, price)
    budget.spent_cny = float(payload["metadata"].get("benchmark_spend_cny", 0.0))
    client = OpenAI(
        api_key=api_key,
        base_url=args.base_url,
        timeout=args.timeout_seconds,
        max_retries=2,
    )
    tasks_by_id = {task["id"]: task for task in tasks}
    completed = {result_key(item) for item in payload["session_results"]}

    for index, entry in enumerate(order, start=1):
        key = entry["task_id"], entry["policy"], int(entry["repeat"])
        if key in completed:
            continue
        task = tasks_by_id[entry["task_id"]]
        print(
            f"[{index}/{len(order)}] {task['name']} / {entry['policy']} / "
            f"第 {entry['repeat']} 次（已计费 {budget.spent_cny:.4f} 元）",
            flush=True,
        )
        try:
            result, source_meta = run_session(
                client,
                task,
                policy=entry["policy"],
                repeat=int(entry["repeat"]),
                model=args.model,
                base_url=args.base_url,
                api_key=api_key,
                budget=budget,
                effective_window=int(dataset["effective_window_tokens"]),
                run_id=payload["metadata"]["run_id"],
                cache_settle_seconds=args.cache_settle_seconds,
            )
        except Exception as exc:
            payload["metadata"]["benchmark_spend_cny"] = budget.spent_cny
            payload.setdefault("aborted_attempts", []).append(
                {
                    "task_id": entry["task_id"],
                    "policy": entry["policy"],
                    "repeat": int(entry["repeat"]),
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "spend_cny_after_attempt": budget.spent_cny,
                    "error": str(exc),
                }
            )
            finalize_payload(payload, price, args.seed)
            write_payload(args.output, args.report, payload)
            raise
        payload["session_results"].append(result.as_dict())
        payload["source_snapshots"][task["id"]] = source_meta
        payload["metadata"]["benchmark_spend_cny"] = budget.spent_cny
        payload["metadata"]["completed_session_count"] = len(
            payload["session_results"]
        )
        finalize_payload(payload, price, args.seed)
        write_payload(args.output, args.report, payload)
        print(
            f"    结果：{'通过' if result.quality.get('passed') else '失败'}；"
            f"本会话 {result.totals()['total_cost_cny']:.4f} 元；"
            f"累计 {budget.spent_cny:.4f} 元",
            flush=True,
        )

    finalize_payload(payload, price, args.seed)
    write_payload(args.output, args.report, payload)
    print(f"正式结果：{args.output}")
    print(f"中文报告：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
