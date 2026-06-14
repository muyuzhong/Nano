# Runtime Kernel — Structured `RunResult` Termination (ADR-015)

- Date: 2026-06-15
- Status: design approved, ready for implementation plan
- Scope: **Runtime Kernel layer only** (per project scope split; HarnessSession and above are another owner's layer)
- Authoritative architecture: `docs/2026-06-14-initial-architecture-decisions.md` §14.7 / ADR-015 (and ADR-008's `RunResult` sketch). This spec is the *implementation-level* plan for the kernel-side "structured termination" item (gap **C** in the 2026-06-15 kernel gap review).

## 0. Scope decision

The 2026-06-15 kernel gap review (against §14 ADR-011..015 + §14.10 invariants) found three not-yet-done kernel items:

| Gap | ADR | Cross-layer? | Disposition here |
|---|---|---|---|
| A history ownership / kernel persists | ADR-012 | **yes** — §14.8 step 2 needs harness as consumer | deferred |
| B no `TurnSnapshot`, run-grained loop | ADR-011 | partial — `run_one_turn` is kernel-only; snapshot creation is harness | deferred |
| **C bare-string termination → structured `RunResult`** | **ADR-015** | **no** — harness is only a future consumer, not a prerequisite | **this spec** |

C is chosen first precisely because it is self-contained inside the kernel, has zero cross-layer dependency, and gives A/B a stable termination contract to build on later. Today `engine.run` ends with `yield ev.AgentEnded(reason)` where `reason` is a bare `str` set at ~9 scattered points (`engine.py:70,80-81,88,108-122,124,151,157`); upper layers can only string-match it. §14.7 / ADR-015 require the kernel to end with a structured result and converge the reasons into an enum, while keeping the §14.10 invariant "异常绝不逃逸内核生成器" (already satisfied by the `engine.py:155` catch-all).

**Approved decisions (from brainstorming):**
1. `StopReason(str, Enum)` whose member *values* equal the current strings, and `AgentEnded` evolves to carry `RunResult` with a back-compat `.reason` property. Because a `str`-based enum compares equal to its string value, all 9 existing `event.reason == "..."` assertions stay green with **zero edits**.
2. `RunResult` carries **4 fields** — `reason / final_message_id / error / detail` — separating real errors (`error`) from normal policy-termination specifics (`detail`), so the supervisor's dynamic reason is preserved rather than lost.
3. Types live in a dedicated `runtime/result.py` (a focused, independently testable unit); `final_message_id` = id of the last assistant message produced *in this run* (else `None`); `detail` is used only by the supervisor for now (YAGNI).

## 1. New types — `runtime/result.py`

A new module (kept separate from `events.py` so the termination contract is its own bounded unit; `events.py` imports from it, no cycle):

```python
"""内核终止契约：结构化运行结果与终止原因枚举（ADR-015）。

内核每次运行以一个 RunResult 收尾，让上层无需解析裸字符串即可区分
「正常未完成 / 预算耗尽 / Provider 失败 / 监督策略终止 / 意外异常」。
StopReason 继承 str，使 == 旧字符串仍成立，保护既有断言与 CLI 展示。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class StopReason(str, Enum):
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    MAX_TOKENS = "max_tokens"
    USER_ABORT = "user_abort"
    TOKEN_BUDGET = "token_budget"
    CONTEXT_OVERFLOW = "context_overflow"
    PROVIDER_ERROR = "provider_error"
    INCOMPLETE_STREAM = "incomplete_stream"
    SUPERVISOR_TERMINATE = "supervisor_terminate"
    FATAL = "fatal"


@dataclass(frozen=True)
class RunResult:
    reason: StopReason
    final_message_id: Optional[str] = None
    error: Optional[str] = None    # 仅真正错误：provider_error / fatal / incomplete_stream / context_overflow
    detail: Optional[str] = None   # 非错误终止的具体说明：supervisor 的 constraint:max_tool_calls(52) 等
```

The member set is exactly the reasons currently produced by `engine.py`. `StopReason(str, Enum)` means `StopReason.COMPLETED == "completed"` is `True`, so any existing string comparison keeps working.

## 2. `AgentEnded` evolution + back-compat — `runtime/events.py`

```python
from runtime.result import RunResult, StopReason

@dataclass
class AgentEnded:
    """以结构化 RunResult 收尾；.reason 保留给既有消费者。"""
    result: RunResult

    @property
    def reason(self) -> StopReason:
        return self.result.reason
```

Existing consumers (`tests/integration/*` 9 assertions, `cli/repl.py:85` printing `event.reason`) continue to work unchanged: `.reason` now returns a `StopReason`, which `==` the old string and prints identically (`StopReason.COMPLETED` str value is `"completed"`).

## 3. Engine threading — `runtime/engine.py`

Keep the **single terminal exit** (readability, dev-guide §4.4). Inside `run`, replace the scattered `reason = "<string>"` assignments with three locals carried to the end, plus tracking the last assistant message id:

- `reason: StopReason` (init `StopReason.MAX_TURNS`, matching today's default `reason = "max_turns"` at `engine.py:70`).
- `error: str | None = None`, `detail: str | None = None`.
- `last_assistant_id: str | None = None`, set to `message.message_id` immediately after the assistant message is appended (`engine.py:118`).

Per-termination assignments (replacing the bare strings):

- abort path → `reason = StopReason.USER_ABORT`
- budget → `reason = StopReason.TOKEN_BUDGET`
- `ContextOverflowError` → `reason = StopReason.CONTEXT_OVERFLOW; error = str(error)` (mirrors the `ErrorEvent` already yielded just before at `engine.py:86`)
- provider fatal → `reason = StopReason.PROVIDER_ERROR; error = str(fatal)`
- incomplete stream → `reason = StopReason.INCOMPLETE_STREAM; error = "Provider 流未正常结束"`
- `stop_reason == "max_tokens"` → `reason = StopReason.MAX_TOKENS`
- no tool calls → `reason = StopReason.COMPLETED`
- supervisor terminate → `reason = StopReason.SUPERVISOR_TERMINATE; detail = verdict.reason` (the specific `constraint:...` string)
- catch-all `except Exception` → `reason = StopReason.FATAL; error = str(error)`

Single exit at the end:

```python
yield ev.AgentEnded(RunResult(reason, last_assistant_id, error, detail))
```

The `ErrorEvent`s already emitted before the error terminations are **left as-is**: the streaming view keeps its incremental error, and `RunResult.error` makes the terminal result self-contained (intentional, minor duplication).

## 4. Termination → field mapping (the precise contract)

| Trigger | `reason` | `error` | `detail` | `final_message_id` |
|---|---|---|---|---|
| model finished, no tool calls | `COMPLETED` | – | – | last assistant id this run |
| `max_turns` exhausted | `MAX_TURNS` | – | – | last assistant id this run |
| provider `stop_reason == max_tokens` | `MAX_TOKENS` | – | – | last assistant id this run |
| user abort (bypass flag) | `USER_ABORT` | – | – | last assistant id this run, else `None` |
| token budget exhausted | `TOKEN_BUDGET` | – | – | last assistant id this run, else `None` |
| context overflow | `CONTEXT_OVERFLOW` | overflow message | – | last assistant id this run, else `None` |
| provider error after retries | `PROVIDER_ERROR` | provider error str | – | last assistant id this run, else `None` |
| incomplete provider stream | `INCOMPLETE_STREAM` | `"Provider 流未正常结束"` | – | last assistant id this run, else `None` |
| supervisor terminate | `SUPERVISOR_TERMINATE` | – | `verdict.reason` | last assistant id this run |
| unexpected exception | `FATAL` | `str(exc)` | – | last assistant id this run, else `None` |

`final_message_id` semantics: the `message_id` of the most recent **assistant** message appended during *this* `run` call; `None` if the run terminated before producing any assistant message (e.g. abort/budget on the first safe point). `Message.message_id` is auto-generated (`blocks.py:119`).

## 5. Tests — `tests/integration/test_engine.py`, `tests/integration/test_control.py`, `tests/unit/` (new `test_result.py`)

Test **observable behavior**, not internals (dev-guide §7 / §4.3).

**Back-compat (no change, proves zero churn):** the 9 existing `event.reason == "..."` assertions stay and stay green.

**New (structured result):**
- For each terminal path already covered by an integration test, additionally assert `event.result.reason is StopReason.X` and the field mapping:
  - `COMPLETED` / `MAX_TURNS` / `MAX_TOKENS`: `error is None and detail is None`; `final_message_id` is the produced assistant message's id.
  - `PROVIDER_ERROR`: `error` is non-empty; `result.reason is StopReason.PROVIDER_ERROR`.
  - `CONTEXT_OVERFLOW`: `error` is non-empty.
  - `INCOMPLETE_STREAM`: `error == "Provider 流未正常结束"`.
  - `USER_ABORT` (control test): `reason is StopReason.USER_ABORT`; `final_message_id is None` when aborted before any assistant message.
- **Supervisor terminate:** drive `ConstraintValidator(max_total_tool_calls=...)` to fire, assert `reason is StopReason.SUPERVISOR_TERMINATE` and `detail.startswith("constraint:")`.
- **Fatal does not escape:** inject a provider that raises a non-`ProviderError` exception mid-loop; assert the generator completes normally (no raise), the last event is `AgentEnded`, `reason is StopReason.FATAL`, and `error` is non-empty. This locks the §14.10 "异常绝不逃逸" invariant.
- **Unit `test_result.py`:** `StopReason.COMPLETED == "completed"` (the back-compat property); `RunResult` is frozen and defaults `error`/`detail`/`final_message_id` to `None`.

## 6. Verification gate (dev-guide §7)

```
python -m pytest -q                 # ≥80 passed (9 unchanged + new), 0 failed
python -m compileall -q core runtime providers tools cli tests
git diff --check
```

A change is "done" only when all three pass and the report states what changed, what was deliberately not changed (A/B and the harness layer), and how each was verified.

## 7. Non-goals (explicit)

- No `run_one_turn` extraction (gap B / §14.8 step 1) — separate kernel slice.
- No `RuntimePolicy` consolidation (gap D) — separate kernel slice.
- No move of history/budget ownership out of the kernel (gaps A/E / §14.8 step 2) — needs the harness as consumer.
- No new `ErrorEvent` semantics; the streaming error events are unchanged.
- No HarnessSession; no CLI assembly refactor. `cli/repl.py` is touched **only** if a line needs it — and it does not, since `.reason` is preserved.

## 8. Self-review

- **Placeholder scan:** none — every type and engine change is shown concretely; the mapping table is exhaustive over the reasons `engine.py` produces today.
- **Internal consistency:** the `StopReason` member set equals the strings enumerated in §3; the mapping table (§4) covers every member; back-compat claim (§0.1, §2, §5) rests on `str`-enum equality and is itself tested.
- **Scope check:** single focused change (one new module + two edited files + tests), well inside one implementation plan; A/B/D/E explicitly deferred.
- **Ambiguity check:** `final_message_id` "this run, else None" is made explicit; `error` vs `detail` population is pinned per-row in §4; `error` intentionally duplicating the prior `ErrorEvent` message is called out.
