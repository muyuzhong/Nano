# Mono Runtime 引擎实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Mono 实现完整的事件驱动运行时引擎：统一块消息模型、双 Provider 流式抽象、并发工具执行、Token 预算与压缩、漂移监督、实时控制平面与终端 REPL。

**Architecture:** AgentLoop 异步生成器产出统一事件流；控制指令经 ControlPlane 收件箱在安全点反向注入；Provider 差异由适配器翻译为标准化 StreamEvent；会话状态 append-only 落盘 JSONL。

**Tech Stack:** Python 3.10+，httpx（异步 HTTP/SSE），rich（终端渲染），pytest + pytest-asyncio（asyncio_mode=auto）。核心模块零第三方依赖。

**工作目录：所有命令都在 `D:\harness agent\Mono` 下执行。**

**设计文档：** `docs/2026-06-10-runtime-design.md`（已评审通过）

---

## 文件结构总览

| 文件 | 职责 | 任务 |
|------|------|------|
| `pyproject.toml` | 项目配置与依赖 | 0 |
| `runtime/blocks.py` | 统一会话模型：Message + 内容块 + token 估算 | 1 |
| `providers/base.py` | ModelProvider 接口 + StreamEvent + 归一化异常 | 2 |
| `providers/mock.py` | 剧本驱动 MockProvider | 3 |
| `runtime/state.py` | SessionState + JSONL 转录 | 4 |
| `runtime/events.py` | 运行时事件 dataclasses | 5 |
| `runtime/control.py` | 控制指令 + ControlPlane | 5 |
| `runtime/executor.py` | ToolRegistry + ToolExecutor | 6 |
| `runtime/context.py` | TokenLedger + RetryPolicy + ContextAssembler | 7 |
| `runtime/engine.py` | StreamAccumulator + AgentLoop | 8 |
| `providers/anthropic.py` | Anthropic SSE 适配器 | 9 |
| `providers/openai_compat.py` | OpenAI 兼容适配器 | 10 |
| `runtime/supervisor.py` | 漂移检测与约束 | 11 |
| `tests/integration/test_control.py` | 控制平面×引擎集成测试 | 12 |
| `tools/builtin.py` | read_file / run_command 演示工具 | 13 |
| `cli/repl.py` | rich 终端 REPL | 14 |

注：设计文档把 control 模块排在第 9 位实现，本计划把**指令数据类与 ControlPlane 提前到 Task 5**——因为引擎（Task 8）需要 import 指令类型；控制平面的引擎集成验证仍保留在 Task 12，顺序逻辑不变。

---

### Task 0: 项目配置与目录骨架

**Files:**
- Create: `pyproject.toml`
- Create: `runtime/__init__.py`、`providers/__init__.py`、`tools/__init__.py`、`cli/__init__.py`、`tests/__init__.py`、`tests/unit/__init__.py`、`tests/integration/__init__.py`（全部为空文件）

- [ ] **Step 1: 写 pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "mono"
version = "0.1.0"
description = "Mono: a minimal but complete agent harness runtime"
requires-python = ">=3.10"
dependencies = ["httpx>=0.27", "rich>=13.0"]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]

[tool.setuptools]
packages = ["core", "runtime", "providers", "tools", "cli"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["live: 需要真实 API key 的冒烟测试"]
```

- [ ] **Step 2: 创建空的 `__init__.py` 文件**

PowerShell:

```powershell
New-Item -ItemType Directory -Force runtime, providers, tools, cli, tests\unit, tests\integration | Out-Null
"runtime","providers","tools","cli","tests","tests\unit","tests\integration" | ForEach-Object { New-Item -ItemType File -Force "$_\__init__.py" | Out-Null }
```

- [ ] **Step 3: 安装依赖并验证**

Run: `python --version`（期望 3.10+）
Run: `pip install -e ".[dev]"`
Run: `python -m pytest`
Expected: `no tests ran`（收集 0 个测试，无报错）

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml runtime providers tools cli tests
git commit -m "chore: project scaffolding for runtime (pyproject + packages)"
```

---

### Task 1: 统一消息模型 `runtime/blocks.py`

**Files:**
- Create: `runtime/blocks.py`
- Test: `tests/unit/test_blocks.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/unit/test_blocks.py"""
from runtime.blocks import (Message, TextBlock, ThinkingBlock, ToolResultBlock,
                            ToolUseBlock, Usage, estimate_tokens)


def test_user_factory():
    m = Message.user("你好")
    assert m.role == "user"
    assert m.get_text() == "你好"
    assert not m.has_tool_calls()
    assert m.message_id.startswith("msg_")


def test_assistant_with_tool_calls():
    blocks = [TextBlock(text="我来执行"), ToolUseBlock(name="bash", input={"cmd": "ls"})]
    m = Message.assistant(blocks, usage=Usage(input_tokens=10, output_tokens=5))
    assert m.has_tool_calls()
    calls = m.get_tool_calls()
    assert calls[0].name == "bash"
    assert calls[0].id.startswith("tooluse_")
    assert m.usage.input_tokens == 10


def test_tool_results_factory_is_user_role():
    r = ToolResultBlock(tool_use_id="t1", content="ok")
    m = Message.tool_results([r])
    assert m.role == "user"
    assert isinstance(m.content[0], ToolResultBlock)


def test_serialization_roundtrip():
    original = Message.assistant(
        [ThinkingBlock(thinking="想一想"),
         TextBlock(text="执行中"),
         ToolUseBlock(name="bash", input={"cmd": "ls"}, id="tooluse_abc")],
        usage=Usage(input_tokens=3, output_tokens=4))
    restored = Message.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()
    assert restored.get_tool_calls()[0].input == {"cmd": "ls"}
    assert restored.usage.output_tokens == 4


def test_estimate_tokens_positive():
    assert estimate_tokens(Message.user("hello world")) >= 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_blocks.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'runtime.blocks'`）

- [ ] **Step 3: 实现**

```python
"""runtime/blocks.py — Mono 统一会话模型：runtime 内部唯一的消息语言"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class TextBlock:
    text: str
    type: str = "text"

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass
class ThinkingBlock:
    thinking: str
    type: str = "thinking"

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "thinking", "thinking": self.thinking}


@dataclass
class ToolUseBlock:
    name: str
    input: Dict[str, Any]
    id: str = field(default_factory=lambda: _new_id("tooluse"))
    type: str = "tool_use"

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    error_type: Optional[str] = None
    type: str = "tool_result"

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "tool_result", "tool_use_id": self.tool_use_id,
                "content": self.content, "is_error": self.is_error,
                "error_type": self.error_type}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


def block_from_dict(d: Dict[str, Any]):
    t = d.get("type")
    if t == "text":
        return TextBlock(text=d["text"])
    if t == "thinking":
        return ThinkingBlock(thinking=d["thinking"])
    if t == "tool_use":
        return ToolUseBlock(name=d["name"], input=d["input"], id=d["id"])
    if t == "tool_result":
        return ToolResultBlock(tool_use_id=d["tool_use_id"], content=d["content"],
                               is_error=d.get("is_error", False),
                               error_type=d.get("error_type"))
    raise ValueError(f"未知块类型: {t}")


@dataclass
class Message:
    role: str                      # "user" | "assistant"
    content: List[Any]             # 混合块列表
    message_id: str = field(default_factory=lambda: _new_id("msg"))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    usage: Optional[Usage] = None

    @classmethod
    def user(cls, text: str) -> "Message":
        return cls(role="user", content=[TextBlock(text=text)])

    @classmethod
    def assistant(cls, blocks: List[Any], usage: Optional[Usage] = None) -> "Message":
        return cls(role="assistant", content=list(blocks), usage=usage)

    @classmethod
    def tool_results(cls, results: List[ToolResultBlock]) -> "Message":
        # 按 LLM API 约定，tool_result 属于 user 角色消息
        return cls(role="user", content=list(results))

    def get_text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    def get_tool_calls(self) -> List[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    def has_tool_calls(self) -> bool:
        return any(isinstance(b, ToolUseBlock) for b in self.content)

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role,
                "content": [b.to_dict() for b in self.content],
                "message_id": self.message_id,
                "timestamp": self.timestamp.isoformat(),
                "usage": self.usage.to_dict() if self.usage else None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        usage = Usage(**data["usage"]) if data.get("usage") else None
        return cls(role=data["role"],
                   content=[block_from_dict(b) for b in data["content"]],
                   message_id=data["message_id"],
                   timestamp=datetime.fromisoformat(data["timestamp"]),
                   usage=usage)


def estimate_tokens(message: Message) -> int:
    """粗估：序列化 JSON 长度 // 4。中文会低估，仅用于预算门限，真实记账用 Usage。"""
    return max(1, len(json.dumps(message.to_dict(), ensure_ascii=False)) // 4)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_blocks.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add runtime/blocks.py tests/unit/test_blocks.py
git commit -m "feat(runtime): unified block-based message model"
```

---

### Task 2: Provider 接口 `providers/base.py`

**Files:**
- Create: `providers/base.py`
- Test: `tests/unit/test_provider_base.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/unit/test_provider_base.py"""
import pytest

from providers.base import (ModelProvider, ModelRequest, ProviderAuthError,
                            ProviderError, ProviderServerError,
                            ProviderTimeoutError, RateLimitError, TextDelta)
from runtime.blocks import Message


def test_cannot_instantiate_abstract_provider():
    with pytest.raises(TypeError):
        ModelProvider()


def test_error_hierarchy():
    assert issubclass(RateLimitError, ProviderError)
    assert issubclass(ProviderTimeoutError, ProviderError)
    assert issubclass(ProviderServerError, ProviderError)
    assert issubclass(ProviderAuthError, ProviderError)
    assert RateLimitError(retry_after=7.5).retry_after == 7.5


def test_default_token_estimate():
    class Dummy(ModelProvider):
        async def stream(self, request):
            yield TextDelta(text="x")

    est = Dummy().count_tokens_estimate([Message.user("hello"), Message.user("world")])
    assert est >= 2


def test_model_request_defaults():
    req = ModelRequest(system="s", messages=[], tools=[], model="m")
    assert req.max_tokens == 4096
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_provider_base.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'providers.base'`）

- [ ] **Step 3: 实现**

```python
"""providers/base.py — ModelProvider 接口、标准化流事件与归一化异常

引擎只消费本模块定义的 StreamEvent；各 Provider 的协议差异都死在适配器里。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Union

from runtime.blocks import Message, Usage, estimate_tokens


# ── 标准化流事件 ─────────────────────────────────────────────

@dataclass
class MessageStart:
    model: str


@dataclass
class TextDelta:
    text: str


@dataclass
class ThinkingDelta:
    thinking: str


@dataclass
class ToolUseStart:
    id: str
    name: str


@dataclass
class ToolInputDelta:
    id: str               # 所属 tool_use 的 id（多工具交错时必须）
    partial_json: str


@dataclass
class ToolUseEnd:
    id: str


@dataclass
class MessageEnd:
    stop_reason: str      # 规范值: end_turn | tool_use | max_tokens
    usage: Usage


StreamEvent = Union[MessageStart, TextDelta, ThinkingDelta,
                    ToolUseStart, ToolInputDelta, ToolUseEnd, MessageEnd]


# ── 归一化异常 ───────────────────────────────────────────────

class ProviderError(Exception):
    """所有 Provider 异常的基类"""


class RateLimitError(ProviderError):
    def __init__(self, retry_after: float = 30.0, message: str = "rate limited"):
        self.retry_after = retry_after
        super().__init__(message)


class ProviderTimeoutError(ProviderError):
    pass


class ProviderServerError(ProviderError):
    pass


class ProviderAuthError(ProviderError):
    """401/403，不可重试"""


class ProviderBadRequestError(ProviderError):
    """400，不可重试"""


RETRYABLE_ERRORS = (RateLimitError, ProviderTimeoutError, ProviderServerError)


# ── 请求与接口 ───────────────────────────────────────────────

@dataclass
class ModelRequest:
    system: str
    messages: List[Message]
    tools: List[Dict]          # [{"name", "description", "input_schema"}, ...]
    model: str
    max_tokens: int = 4096


class ModelProvider(ABC):
    @abstractmethod
    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """流式推理，逐个产出标准化 StreamEvent（异步生成器）"""

    def count_tokens_estimate(self, messages: List[Message]) -> int:
        return sum(estimate_tokens(m) for m in messages)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_provider_base.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add providers/base.py tests/unit/test_provider_base.py
git commit -m "feat(providers): ModelProvider interface, stream events, normalized errors"
```

---

### Task 3: 剧本驱动 `providers/mock.py`

**Files:**
- Create: `providers/mock.py`
- Test: `tests/unit/test_mock_provider.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/unit/test_mock_provider.py"""
import pytest

from providers.base import (MessageEnd, MessageStart, ModelRequest, RateLimitError,
                            TextDelta, ToolInputDelta, ToolUseEnd, ToolUseStart)
from providers.mock import MockProvider


def make_request():
    return ModelRequest(system="s", messages=[], tools=[], model="mock-model")


async def collect(provider):
    return [e async for e in provider.stream(make_request())]


async def test_text_turn_replay():
    p = MockProvider([MockProvider.text_turn("你好")])
    events = await collect(p)
    assert isinstance(events[0], MessageStart)
    assert isinstance(events[1], TextDelta) and events[1].text == "你好"
    assert isinstance(events[-1], MessageEnd)
    assert events[-1].stop_reason == "end_turn"


async def test_tool_turn_replay():
    p = MockProvider([MockProvider.tool_turn("bash", {"cmd": "ls"}, tool_id="t1")])
    events = await collect(p)
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["MessageStart", "ToolUseStart", "ToolInputDelta", "ToolUseEnd", "MessageEnd"]
    assert events[1].id == "t1" and events[1].name == "bash"
    assert events[2].id == "t1"
    assert events[-1].stop_reason == "tool_use"


async def test_exception_entry_raises():
    p = MockProvider([RateLimitError(retry_after=1.0)])
    with pytest.raises(RateLimitError):
        await collect(p)


async def test_records_requests_and_exhausts():
    p = MockProvider([MockProvider.text_turn("a")])
    await collect(p)
    assert len(p.requests) == 1
    with pytest.raises(AssertionError):
        await collect(p)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_mock_provider.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'providers.mock'`）

- [ ] **Step 3: 实现**

```python
"""providers/mock.py — 剧本驱动的 MockProvider：集成测试与离线演示的核心道具"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import AsyncIterator, List, Union

from providers.base import (MessageEnd, MessageStart, ModelProvider, ModelRequest,
                            StreamEvent, TextDelta, ToolInputDelta, ToolUseEnd,
                            ToolUseStart)
from runtime.blocks import Usage

ScriptEntry = Union[List[StreamEvent], Exception]


class MockProvider(ModelProvider):
    """按剧本逐轮回放流事件；剧本条目也可以是异常（该轮调用直接抛出）。"""

    def __init__(self, script: List[ScriptEntry]):
        self.script = list(script)
        self.requests: List[ModelRequest] = []
        self._i = 0

    @staticmethod
    def text_turn(text: str, usage: Usage = None) -> List[StreamEvent]:
        return [MessageStart(model="mock-model"),
                TextDelta(text=text),
                MessageEnd(stop_reason="end_turn", usage=usage or Usage(10, 5))]

    @staticmethod
    def tool_turn(name: str, tool_input: dict, tool_id: str = None,
                  usage: Usage = None) -> List[StreamEvent]:
        tid = tool_id or f"tooluse_{uuid.uuid4().hex[:8]}"
        return [MessageStart(model="mock-model"),
                ToolUseStart(id=tid, name=name),
                ToolInputDelta(id=tid, partial_json=json.dumps(tool_input, ensure_ascii=False)),
                ToolUseEnd(id=tid),
                MessageEnd(stop_reason="tool_use", usage=usage or Usage(10, 5))]

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        if self._i >= len(self.script):
            raise AssertionError("MockProvider 剧本已耗尽")
        entry = self.script[self._i]
        self._i += 1
        if isinstance(entry, Exception):
            raise entry
        for event in entry:
            await asyncio.sleep(0)   # 让出事件循环，模拟真实流式节奏
            yield event
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_mock_provider.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add providers/mock.py tests/unit/test_mock_provider.py
git commit -m "feat(providers): script-driven MockProvider"
```

---

### Task 4: 会话状态 `runtime/state.py`

**Files:**
- Create: `runtime/state.py`
- Test: `tests/unit/test_state.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/unit/test_state.py"""
from runtime.blocks import Message, TextBlock, ToolUseBlock, Usage
from runtime.state import SessionState


def test_append_writes_jsonl(tmp_path):
    state = SessionState(transcript_dir=str(tmp_path))
    state.append(Message.user("第一条"))
    state.append(Message.assistant([TextBlock(text="回复")], usage=Usage(3, 4)))
    lines = state.transcript_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_resume_roundtrip(tmp_path):
    state = SessionState(session_id="sess_test01", transcript_dir=str(tmp_path))
    state.append(Message.user("问题"))
    state.append(Message.assistant(
        [ToolUseBlock(name="bash", input={"cmd": "ls"}, id="t1")], usage=Usage(7, 8)))

    restored = SessionState.resume("sess_test01", transcript_dir=str(tmp_path))
    assert len(restored.messages) == 2
    assert restored.messages[1].get_tool_calls()[0].input == {"cmd": "ls"}
    assert restored.messages[1].usage.output_tokens == 8
    assert [m.to_dict() for m in restored.messages] == [m.to_dict() for m in state.messages]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_state.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现**

```python
"""runtime/state.py — SessionState：append-only 消息历史 + JSONL 转录

转录文件即检查点：每条消息落一行 JSON，进程级恢复 = 重读转录。
注意：上下文压缩只修改内存中的工作集（state.messages），转录始终保留完整历史。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import List, Optional

from runtime.blocks import Message


class SessionState:
    def __init__(self, session_id: Optional[str] = None, transcript_dir: str = "sessions"):
        self.session_id = session_id or f"sess_{uuid.uuid4().hex[:8]}"
        self.transcript_dir = Path(transcript_dir)
        self.messages: List[Message] = []

    @property
    def transcript_path(self) -> Path:
        return self.transcript_dir / f"{self.session_id}.jsonl"

    def append(self, message: Message) -> None:
        self.messages.append(message)
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        with open(self.transcript_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")

    @classmethod
    def resume(cls, session_id: str, transcript_dir: str = "sessions") -> "SessionState":
        state = cls(session_id=session_id, transcript_dir=transcript_dir)
        with open(state.transcript_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    state.messages.append(Message.from_dict(json.loads(line)))
        return state
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_state.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add runtime/state.py tests/unit/test_state.py
git commit -m "feat(runtime): SessionState with append-only JSONL transcript"
```

---

### Task 5: 运行时事件与控制指令 `runtime/events.py` + `runtime/control.py`

**Files:**
- Create: `runtime/events.py`
- Create: `runtime/control.py`
- Test: `tests/unit/test_control.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/unit/test_control.py"""
import asyncio

from runtime.control import Abort, Approve, ControlPlane, Deny, Pause, Resume, Steer


def test_submit_abort_sets_flag():
    cp = ControlPlane()
    assert cp.abort_requested is False
    cp.submit(Abort())
    assert cp.abort_requested is True


def test_drain_returns_in_order_and_empties():
    cp = ControlPlane()
    cp.submit(Steer(text="a"))
    cp.submit(Pause())
    drained = cp.drain_nowait()
    assert isinstance(drained[0], Steer) and drained[0].text == "a"
    assert isinstance(drained[1], Pause)
    assert cp.drain_nowait() == []


async def test_wait_decision_preserves_non_decisions():
    cp = ControlPlane()
    cp.submit(Steer(text="先转向"))
    cp.submit(Approve(ids=["t1"]))
    decision = await cp.wait_decision()
    assert isinstance(decision, Approve) and decision.ids == ["t1"]
    # Steer 不能被审批等待吞掉，应该还在队列里
    remaining = cp.drain_nowait()
    assert len(remaining) == 1 and isinstance(remaining[0], Steer)


async def test_wait_resume():
    cp = ControlPlane()
    cp.submit(Resume())
    cmd = await cp.wait_resume()
    assert isinstance(cmd, Resume)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_control.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 `runtime/control.py`**

```python
"""runtime/control.py — 实时控制平面：指令词汇表 + 收件箱

REPL（或任何外部系统）通过 submit() 注入指令；
引擎在安全点调用 drain_nowait()，在审批/暂停时调用 wait_decision()/wait_resume()。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Abort:
    """立即中止本次运行"""


@dataclass
class Steer:
    """在下一个安全点注入一条用户消息（只追加不改历史，保持前缀一致性）"""
    text: str


@dataclass
class Approve:
    """批准工具调用；ids=None 表示批准全部待审批项"""
    ids: Optional[List[str]] = None


@dataclass
class Deny:
    """拒绝工具调用；ids=None 表示拒绝全部待审批项"""
    ids: Optional[List[str]] = None


@dataclass
class Pause:
    """在下一个安全点暂停，等待 Resume"""


@dataclass
class Resume:
    """从 Pause 中恢复"""


class ControlPlane:
    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self.abort_requested = False

    def submit(self, cmd) -> None:
        if isinstance(cmd, Abort):
            self.abort_requested = True   # 旁路标志：流式循环中无需排队即可感知
        self._queue.put_nowait(cmd)

    def drain_nowait(self) -> list:
        out = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return out

    async def _wait_for(self, kinds: tuple):
        stash = []
        try:
            while True:
                cmd = await self._queue.get()
                if isinstance(cmd, kinds):
                    return cmd
                stash.append(cmd)        # 其他指令不丢弃，等待结束后放回
        finally:
            for c in stash:
                self._queue.put_nowait(c)

    async def wait_decision(self):
        return await self._wait_for((Approve, Deny, Abort))

    async def wait_resume(self):
        return await self._wait_for((Resume, Abort))
```

- [ ] **Step 4: 实现 `runtime/events.py`**

```python
"""runtime/events.py — 运行时事件：AgentLoop 向消费者（REPL/测试）发布的一切"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class AgentStarted:
    session_id: str


@dataclass
class TurnStarted:
    turn: int


@dataclass
class TextDeltaEvent:
    text: str


@dataclass
class ThinkingDeltaEvent:
    thinking: str


@dataclass
class ToolCallStarted:
    """模型在流中开始生成一个工具调用"""
    tool_use_id: str
    name: str


@dataclass
class AssistantMessageEnd:
    stop_reason: str


@dataclass
class ApprovalRequested:
    """双向模式：引擎暂停，等待控制平面回传 Approve/Deny"""
    calls: List      # List[ToolUseBlock]


@dataclass
class ToolExecutionStarted:
    tool_use_id: str
    name: str


@dataclass
class ToolResultReceived:
    tool_use_id: str
    name: str
    is_error: bool
    content_preview: str


@dataclass
class ContextCompacted:
    before_tokens: int
    after_tokens: int


@dataclass
class InferenceRetrying:
    attempt: int
    delay: float


@dataclass
class SupervisorInjected:
    reason: str


@dataclass
class Steered:
    text: str


@dataclass
class Paused:
    pass


@dataclass
class Resumed:
    pass


@dataclass
class TurnEnded:
    turn: int


@dataclass
class ErrorEvent:
    error_type: str
    message: str


@dataclass
class AgentEnded:
    reason: str   # completed | max_turns | user_abort | token_budget | provider_error | fatal | supervisor 前缀
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_control.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add runtime/events.py runtime/control.py tests/unit/test_control.py
git commit -m "feat(runtime): runtime events vocabulary and control plane inbox"
```

---

### Task 6: 工具执行 `runtime/executor.py`

**Files:**
- Create: `runtime/executor.py`
- Create: `tests/helpers.py`（共享测试工具）
- Test: `tests/unit/test_executor.py`

- [ ] **Step 1: 写共享测试辅助 `tests/helpers.py`**

```python
"""tests/helpers.py — 跨测试文件共享的工具与夹具"""
from core.tool import Tool, ToolResult


class EchoTool(Tool):
    timeout_seconds = 5

    def name(self):
        return "echo"

    def description(self):
        return "回显输入文本"

    def input_schema(self):
        return {"type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"]}

    async def call(self, params):
        return ToolResult(success=True, content=f"echo:{params['text']}", execution_time=0.0)


class DangerTool(EchoTool):
    """需要审批的工具（控制平面测试用）"""
    requires_approval = True

    def name(self):
        return "danger"


class FakeSleep:
    """可注入的假睡眠：记录调用而不真等"""
    def __init__(self):
        self.calls = []

    async def __call__(self, delay):
        self.calls.append(delay)


async def collect(aiter):
    return [e async for e in aiter]
```

- [ ] **Step 2: 写失败测试**

```python
"""tests/unit/test_executor.py"""
import asyncio

from core.tool import Tool, ToolResult
from runtime.blocks import ToolUseBlock
from runtime.executor import ToolExecutor, ToolRegistry, validate_params
from tests.helpers import EchoTool, FakeSleep


def make_executor(*tools, **kwargs):
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    return ToolExecutor(registry, **kwargs)


def test_validate_params():
    schema = {"type": "object",
              "properties": {"text": {"type": "string"}, "n": {"type": "integer"}},
              "required": ["text"]}
    assert validate_params(schema, {"text": "hi", "n": 3}) == []
    assert any("缺少必填参数" in e for e in validate_params(schema, {}))
    assert any("类型应为" in e for e in validate_params(schema, {"text": 42}))
    assert any("未知参数" in e for e in validate_params(schema, {"text": "a", "bogus": 1}))
    assert any("JSON 解析失败" in e for e in validate_params(schema, {"__parse_error__": "{bad"}))


async def test_tool_not_found():
    ex = make_executor()
    r = await ex.execute_one(ToolUseBlock(name="nope", input={}, id="t1"))
    assert r.is_error and r.error_type == "ToolNotFound" and r.tool_use_id == "t1"


async def test_success_maps_tool_result():
    ex = make_executor(EchoTool())
    r = await ex.execute_one(ToolUseBlock(name="echo", input={"text": "hi"}, id="t2"))
    assert not r.is_error and r.content == "echo:hi"


async def test_invalid_params_rejected_before_execution():
    ex = make_executor(EchoTool())
    r = await ex.execute_one(ToolUseBlock(name="echo", input={}, id="t3"))
    assert r.is_error and r.error_type == "ParameterValidation"


async def test_exception_becomes_observation():
    class BoomTool(EchoTool):
        def name(self):
            return "boom"

        async def call(self, params):
            raise RuntimeError("炸了")

    ex = make_executor(BoomTool())
    r = await ex.execute_one(ToolUseBlock(name="boom", input={"text": "x"}, id="t4"))
    assert r.is_error and r.error_type == "RuntimeError" and "炸了" in r.content


async def test_timeout_retries_once_then_errors():
    class SlowTool(EchoTool):
        timeout_seconds = 0.01
        calls = 0

        def name(self):
            return "slow"

        async def call(self, params):
            type(self).calls += 1
            await asyncio.sleep(1)

    sleeper = FakeSleep()
    ex = make_executor(SlowTool(), sleep=sleeper)
    r = await ex.execute_one(ToolUseBlock(name="slow", input={"text": "x"}, id="t5"))
    assert r.is_error and r.error_type == "ToolTimeout"
    assert SlowTool.calls == 2          # 原始 1 次 + 重试 1 次
    assert sleeper.calls == [0.5]


async def test_execute_all_concurrent_and_ordered():
    class GateTool(EchoTool):
        active = 0
        max_active = 0

        def name(self):
            return "gate"

        async def call(self, params):
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            await asyncio.sleep(0.02)
            type(self).active -= 1
            return ToolResult(success=True, content=params["text"], execution_time=0.0)

    ex = make_executor(GateTool(), max_concurrent=2)
    calls = [ToolUseBlock(name="gate", input={"text": str(i)}, id=f"g{i}") for i in range(3)]
    results = await ex.execute_all(calls)
    assert [r.tool_use_id for r in results] == ["g0", "g1", "g2"]   # 顺序与请求一致
    assert GateTool.max_active == 2                                  # 并发但受信号量限制
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_executor.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'runtime.executor'`）

- [ ] **Step 4: 实现**

```python
"""runtime/executor.py — ToolRegistry + ToolExecutor

执行管道四步：查找 → 参数校验 → 并发执行（限流+超时）→ 错误即观察。
任何失败都变成 is_error=True 的 ToolResultBlock 反馈给模型，永不打断循环。
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from core.tool import Tool, ToolResult
from runtime.blocks import ToolResultBlock, ToolUseBlock

TYPE_MAP = {"string": str, "integer": int, "number": (int, float),
            "boolean": bool, "array": list, "object": dict}


def validate_params(schema: dict, params: dict) -> List[str]:
    if "__parse_error__" in params:
        return [f"工具参数 JSON 解析失败：{str(params['__parse_error__'])[:200]}"]
    errors = []
    for req in schema.get("required", []):
        if req not in params:
            errors.append(f"缺少必填参数：{req}")
    props = schema.get("properties", {})
    for key, value in params.items():
        if key not in props:
            errors.append(f"未知参数：{key}")
            continue
        expected = props[key].get("type")
        py_type = TYPE_MAP.get(expected)
        if py_type is not None and not isinstance(value, py_type):
            errors.append(f"参数 {key} 类型应为 {expected}，实际为 {type(value).__name__}")
    return errors


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name()] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def schemas(self) -> List[dict]:
        return [t.get_definition_dict() for t in self._tools.values()]


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, max_concurrent: int = 5, sleep=None):
        self.registry = registry
        self.max_concurrent = max_concurrent
        self.sleep = sleep or asyncio.sleep
        self._sem: Optional[asyncio.Semaphore] = None

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_concurrent)
        return self._sem

    async def execute_all(self, calls: List[ToolUseBlock]) -> List[ToolResultBlock]:
        if not calls:
            return []
        return list(await asyncio.gather(*(self.execute_one(c) for c in calls)))

    async def execute_one(self, call: ToolUseBlock) -> ToolResultBlock:
        tool = self.registry.get(call.name)
        if tool is None:
            return ToolResultBlock(tool_use_id=call.id,
                                   content=f"工具 '{call.name}' 不存在",
                                   is_error=True, error_type="ToolNotFound")
        errors = validate_params(tool.input_schema(), call.input)
        if errors:
            return ToolResultBlock(tool_use_id=call.id,
                                   content="参数校验失败：\n" + "\n".join(errors),
                                   is_error=True, error_type="ParameterValidation")
        timeout = getattr(tool, "timeout_seconds", 30)
        async with self._semaphore():
            result = None
            for attempt in (1, 2):                       # 超时重试一次
                try:
                    result = await asyncio.wait_for(tool.call(call.input), timeout=timeout)
                    break
                except asyncio.TimeoutError:
                    if attempt == 2:
                        return ToolResultBlock(tool_use_id=call.id,
                                               content=f"工具执行超时（>{timeout}s，已重试 1 次）",
                                               is_error=True, error_type="ToolTimeout")
                    await self.sleep(0.5)
                except Exception as e:                   # 错误即观察
                    return ToolResultBlock(tool_use_id=call.id,
                                           content=f"{type(e).__name__}: {e}",
                                           is_error=True, error_type=type(e).__name__)
        if isinstance(result, ToolResult):
            return ToolResultBlock(tool_use_id=call.id, content=str(result.content),
                                   is_error=not result.success, error_type=result.error_type)
        return ToolResultBlock(tool_use_id=call.id, content=str(result), is_error=False)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_executor.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add runtime/executor.py tests/unit/test_executor.py tests/helpers.py
git commit -m "feat(runtime): concurrent tool executor with validation and timeout retry"
```

---

### Task 7: 上下文与预算 `runtime/context.py`

**Files:**
- Create: `runtime/context.py`
- Test: `tests/unit/test_context.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/unit/test_context.py"""
from providers.base import (ProviderAuthError, ProviderTimeoutError, RateLimitError)
from runtime.blocks import Message, TextBlock, ToolResultBlock, ToolUseBlock, Usage
from runtime.context import ContextAssembler, RetryPolicy, TokenLedger
from runtime.executor import ToolRegistry
from runtime.state import SessionState
from tests.helpers import FakeSleep


def test_ledger_records_and_checks_budget():
    ledger = TokenLedger(max_total_tokens=100, max_api_calls=2)
    assert ledger.budget_ok()
    ledger.record(Usage(input_tokens=40, output_tokens=20))
    assert ledger.total_tokens == 60 and ledger.api_calls == 1
    assert ledger.budget_ok()
    ledger.record(Usage(input_tokens=30, output_tokens=20))
    assert not ledger.budget_ok()       # api_calls 达到上限


def test_retry_policy_decisions():
    p = RetryPolicy(max_retries=3, initial_backoff=1.0, max_backoff=8.0)
    assert p.should_retry(RateLimitError(retry_after=5.0), attempt=1)
    assert p.should_retry(ProviderTimeoutError("t"), attempt=3)
    assert not p.should_retry(ProviderTimeoutError("t"), attempt=4)   # 超过 max_retries
    assert not p.should_retry(ProviderAuthError("bad key"), attempt=1)  # 不可重试
    assert p.backoff_for(RateLimitError(retry_after=5.0), attempt=1) == 5.0  # Retry-After 优先
    assert p.backoff_for(ProviderTimeoutError("t"), attempt=1) == 1.0
    assert p.backoff_for(ProviderTimeoutError("t"), attempt=2) == 2.0
    assert p.backoff_for(ProviderTimeoutError("t"), attempt=5) == 8.0  # 封顶


def _build_state(tmp_path, messages):
    state = SessionState(transcript_dir=str(tmp_path))
    for m in messages:
        state.append(m)
    return state


def test_no_compaction_under_threshold(tmp_path):
    state = _build_state(tmp_path, [Message.user("短问题")])
    asm = ContextAssembler(system_prompt="s", registry=None, model="m",
                           context_window=200_000)
    request, info = asm.build(state)
    assert info is None
    assert request.messages[0].get_text() == "短问题"


def test_compaction_truncates_old_tool_results(tmp_path):
    big = "x" * 5000
    state = _build_state(tmp_path, [
        Message.user("查一下"),
        Message.assistant([ToolUseBlock(name="bash", input={"cmd": "cat"}, id="t1")]),
        Message.tool_results([ToolResultBlock(tool_use_id="t1", content=big)]),
        Message.assistant([TextBlock(text="结果很长")]),
        Message.user("继续"),
    ])
    asm = ContextAssembler(system_prompt="s", registry=None, model="m",
                           context_window=1000, compact_threshold=0.8, keep_recent=2)
    request, info = asm.build(state)
    assert info is not None and info[0] > info[1]          # (before, after)
    truncated = state.messages[2].content[0].content
    assert "[结果已截断" in truncated and "5000" in truncated


def test_snip_keeps_first_and_recent_with_summary(tmp_path):
    msgs = []
    for i in range(12):
        msgs.append(Message.user(f"问题{i}" + "啰" * 100))
        msgs.append(Message.assistant([TextBlock(text=f"答案{i}" + "嗦" * 100)]))
    state = _build_state(tmp_path, msgs)
    asm = ContextAssembler(system_prompt="s", registry=None, model="m",
                           context_window=500, compact_threshold=0.8, keep_recent=4)
    request, info = asm.build(state)
    assert info is not None
    assert state.messages[0].get_text().startswith("问题0")     # 首条保留
    assert "[历史已压缩]" in state.messages[1].get_text()        # 摘要占位
    assert state.messages[2].role == "user"                     # 切口落在普通用户消息
    assert all(not isinstance(b, ToolResultBlock)
               for m in state.messages for b in m.content)       # 无孤儿工具结果
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_context.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现**

```python
"""runtime/context.py — TokenLedger（真实记账）+ RetryPolicy + ContextAssembler（组装与压缩）"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional, Tuple

from providers.base import (ModelRequest, RETRYABLE_ERRORS)
from providers.base import RateLimitError
from runtime.blocks import (Message, TextBlock, ToolResultBlock, Usage,
                            estimate_tokens)

TRUNCATE_LIMIT = 200   # 超过该长度的旧工具结果会被截断


class TokenLedger:
    """任务级预算：真实 usage 记账（三级预算体系的 Per-Task 层）"""

    def __init__(self, max_total_tokens: int = 1_000_000, max_api_calls: int = 100):
        self.max_total_tokens = max_total_tokens
        self.max_api_calls = max_api_calls
        self.input_tokens = 0
        self.output_tokens = 0
        self.api_calls = 0

    def record(self, usage: Usage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.api_calls += 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def budget_ok(self) -> bool:
        return (self.total_tokens < self.max_total_tokens
                and self.api_calls < self.max_api_calls)


class RetryPolicy:
    """指数退避 + Retry-After 感知；时钟可注入，测试不用真睡"""

    def __init__(self, max_retries: int = 3, initial_backoff: float = 1.0,
                 max_backoff: float = 30.0, base: float = 2.0, sleep=None):
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self.base = base
        self.sleep = sleep or asyncio.sleep

    def should_retry(self, error: Exception, attempt: int) -> bool:
        if attempt > self.max_retries:
            return False
        return isinstance(error, RETRYABLE_ERRORS)

    def backoff_for(self, error: Exception, attempt: int) -> float:
        if isinstance(error, RateLimitError):
            return float(error.retry_after)
        return min(self.initial_backoff * (self.base ** (attempt - 1)), self.max_backoff)


def _truncate_old_tool_results(messages: List[Message], keep_recent: int) -> List[Message]:
    """策略 1：把最近 keep_recent 条之外的大块工具结果截断（工具结果最肥、最先失去价值）"""
    cutoff = max(0, len(messages) - keep_recent)
    out = []
    for i, m in enumerate(messages):
        needs = (i < cutoff and m.role == "user"
                 and any(isinstance(b, ToolResultBlock) and len(b.content) > TRUNCATE_LIMIT
                         for b in m.content))
        if needs:
            blocks = []
            for b in m.content:
                if isinstance(b, ToolResultBlock) and len(b.content) > TRUNCATE_LIMIT:
                    blocks.append(ToolResultBlock(
                        tool_use_id=b.tool_use_id,
                        content=f"[结果已截断，原长 {len(b.content)} 字符]",
                        is_error=b.is_error, error_type=b.error_type))
                else:
                    blocks.append(b)
            m = Message(role="user", content=blocks,
                        message_id=m.message_id, timestamp=m.timestamp)
        out.append(m)
    return out


def _is_plain_user(message: Message) -> bool:
    return message.role == "user" and all(isinstance(b, TextBlock) for b in message.content)


def _snip_history(messages: List[Message], keep_recent: int) -> List[Message]:
    """策略 2：保留首条 + 摘要占位 + 近期消息。切口只落在普通用户消息上，
    保证 assistant 的 tool_use 与其 tool_result 永远成对保留或成对丢弃。"""
    if len(messages) <= keep_recent + 2:
        return messages
    start = len(messages) - keep_recent
    while start < len(messages) and not _is_plain_user(messages[start]):
        start += 1
    if start >= len(messages):                 # 找不到安全边界，退回硬切
        start = len(messages) - keep_recent
        while start < len(messages) and any(
                isinstance(b, ToolResultBlock) for b in messages[start].content):
            start += 1                         # 丢弃孤儿工具结果，保持配对完整
    omitted = start - 1
    summary = Message.user(f"[历史已压缩] 此处省略了 {omitted} 条早期消息，"
                           f"关键上下文见首条消息与下方近期对话。")
    return [messages[0], summary] + messages[start:]


class ContextAssembler:
    """组装 system + 历史 + 工具 schema；预估超过阈值时先截断工具结果、再裁剪历史"""

    def __init__(self, system_prompt: str, registry=None, model: str = "mock-model",
                 max_tokens: int = 4096, context_window: int = 200_000,
                 compact_threshold: float = 0.8, keep_recent: int = 8):
        self.system_prompt = system_prompt
        self.registry = registry
        self.model = model
        self.max_tokens = max_tokens
        self.context_window = context_window
        self.compact_threshold = compact_threshold
        self.keep_recent = keep_recent

    def _estimate(self, messages: List[Message], tools: List[dict]) -> int:
        total = sum(estimate_tokens(m) for m in messages)
        if tools:
            total += len(json.dumps(tools, ensure_ascii=False)) // 4
        total += len(self.system_prompt) // 4
        return total

    def build(self, state) -> Tuple[ModelRequest, Optional[Tuple[int, int]]]:
        tools = self.registry.schemas() if self.registry else []
        msgs = state.messages
        limit = self.context_window * self.compact_threshold
        info = None
        est = self._estimate(msgs, tools)
        if est > limit:
            before = est
            msgs = _truncate_old_tool_results(msgs, self.keep_recent)
            if self._estimate(msgs, tools) > limit:
                msgs = _snip_history(msgs, self.keep_recent)
            state.messages = msgs            # 压缩只改工作集；JSONL 转录保留全量历史
            info = (before, self._estimate(msgs, tools))
        request = ModelRequest(system=self.system_prompt, messages=list(msgs),
                               tools=tools, model=self.model, max_tokens=self.max_tokens)
        return request, info
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_context.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add runtime/context.py tests/unit/test_context.py
git commit -m "feat(runtime): token ledger, retry policy, context assembler with compaction"
```

---

### Task 8: 核心循环 `runtime/engine.py` + Mock 端到端集成测试

**Files:**
- Create: `runtime/engine.py`
- Test: `tests/unit/test_accumulator.py`
- Test: `tests/integration/test_engine.py`

- [ ] **Step 1: 写 StreamAccumulator 失败测试**

```python
"""tests/unit/test_accumulator.py"""
from providers.base import (MessageEnd, TextDelta, ThinkingDelta, ToolInputDelta,
                            ToolUseEnd, ToolUseStart)
from runtime.blocks import TextBlock, ThinkingBlock, ToolUseBlock, Usage
from runtime.engine import StreamAccumulator


def test_text_only():
    acc = StreamAccumulator()
    for e in [TextDelta(text="你"), TextDelta(text="好"),
              MessageEnd(stop_reason="end_turn", usage=Usage(3, 4))]:
        acc.feed(e)
    m = acc.result()
    assert m.get_text() == "你好" and m.usage.output_tokens == 4
    assert acc.stop_reason == "end_turn"


def test_text_then_tool_with_split_json():
    acc = StreamAccumulator()
    for e in [TextDelta(text="我来执行"),
              ToolUseStart(id="t1", name="bash"),
              ToolInputDelta(id="t1", partial_json='{"cmd"'),
              ToolInputDelta(id="t1", partial_json=': "ls"}'),
              ToolUseEnd(id="t1"),
              MessageEnd(stop_reason="tool_use", usage=Usage(1, 2))]:
        acc.feed(e)
    m = acc.result()
    assert isinstance(m.content[0], TextBlock)
    call = m.get_tool_calls()[0]
    assert call.id == "t1" and call.input == {"cmd": "ls"}


def test_broken_json_marked_as_parse_error():
    acc = StreamAccumulator()
    for e in [ToolUseStart(id="t1", name="bash"),
              ToolInputDelta(id="t1", partial_json='{"cmd": '),
              ToolUseEnd(id="t1"),
              MessageEnd(stop_reason="tool_use", usage=Usage(1, 1))]:
        acc.feed(e)
    call = acc.result().get_tool_calls()[0]
    assert "__parse_error__" in call.input


def test_interleaved_tools_by_id():
    acc = StreamAccumulator()
    for e in [ToolUseStart(id="a", name="t_a"),
              ToolUseStart(id="b", name="t_b"),
              ToolInputDelta(id="a", partial_json='{"x": 1}'),
              ToolInputDelta(id="b", partial_json='{"y": 2}'),
              ToolUseEnd(id="a"), ToolUseEnd(id="b"),
              MessageEnd(stop_reason="tool_use", usage=Usage(1, 1))]:
        acc.feed(e)
    calls = {c.id: c.input for c in acc.result().get_tool_calls()}
    assert calls == {"a": {"x": 1}, "b": {"y": 2}}


def test_thinking_block():
    acc = StreamAccumulator()
    for e in [ThinkingDelta(thinking="想想"), TextDelta(text="好的"),
              MessageEnd(stop_reason="end_turn", usage=Usage(1, 1))]:
        acc.feed(e)
    m = acc.result()
    assert isinstance(m.content[0], ThinkingBlock) and m.content[0].thinking == "想想"
```

- [ ] **Step 2: 写引擎端到端失败测试**

```python
"""tests/integration/test_engine.py"""
import pytest

from providers.base import ProviderAuthError, RateLimitError
from providers.mock import MockProvider
from runtime import events as ev
from runtime.context import RetryPolicy, TokenLedger
from runtime.engine import AgentLoop
from runtime.executor import ToolRegistry
from runtime.state import SessionState
from tests.helpers import EchoTool, FakeSleep, collect


def make_loop(tmp_path, script, tools=(), **kwargs):
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    sleeper = FakeSleep()
    loop = AgentLoop(MockProvider(script), registry,
                     state=SessionState(transcript_dir=str(tmp_path)),
                     retry_policy=RetryPolicy(sleep=sleeper), **kwargs)
    return loop, sleeper


def kinds(events):
    return [type(e).__name__ for e in events]


async def test_text_only_completes(tmp_path):
    loop, _ = make_loop(tmp_path, [MockProvider.text_turn("你好！")])
    events = await collect(loop.run("hi"))
    assert kinds(events)[0] == "AgentStarted"
    assert any(isinstance(e, ev.TextDeltaEvent) and e.text == "你好！" for e in events)
    assert events[-1].reason == "completed"
    assert len(loop.state.messages) == 2          # user + assistant


async def test_tool_roundtrip(tmp_path):
    script = [MockProvider.tool_turn("echo", {"text": "hi"}, tool_id="t1"),
              MockProvider.text_turn("完成")]
    loop, _ = make_loop(tmp_path, script, tools=[EchoTool()])
    events = await collect(loop.run("回显 hi"))
    assert events[-1].reason == "completed"
    results = [e for e in events if isinstance(e, ev.ToolResultReceived)]
    assert results[0].content_preview == "echo:hi" and not results[0].is_error
    assert len(loop.state.messages) == 4          # user/assistant/tool_results/assistant
    # 第二次推理请求必须携带工具结果（前缀一致性）
    second = loop.provider.requests[1]
    assert any("echo:hi" in b.content for m in second.messages
               for b in m.content if hasattr(b, "tool_use_id"))


async def test_retryable_error_then_success(tmp_path):
    script = [RateLimitError(retry_after=0.0), MockProvider.text_turn("恢复了")]
    loop, sleeper = make_loop(tmp_path, script)
    events = await collect(loop.run("hi"))
    assert any(isinstance(e, ev.InferenceRetrying) for e in events)
    assert events[-1].reason == "completed"
    assert sleeper.calls == [0.0]


async def test_non_retryable_error_ends_run(tmp_path):
    loop, _ = make_loop(tmp_path, [ProviderAuthError("bad key")])
    events = await collect(loop.run("hi"))
    assert any(isinstance(e, ev.ErrorEvent) and e.error_type == "ProviderAuthError"
               for e in events)
    assert events[-1].reason == "provider_error"


async def test_broken_tool_json_fed_back_as_error(tmp_path):
    from providers.base import (MessageEnd, MessageStart, ToolInputDelta,
                                ToolUseEnd, ToolUseStart)
    from runtime.blocks import Usage
    broken = [MessageStart(model="mock-model"),
              ToolUseStart(id="t1", name="echo"),
              ToolInputDelta(id="t1", partial_json='{"text": '),
              ToolUseEnd(id="t1"),
              MessageEnd(stop_reason="tool_use", usage=Usage(1, 1))]
    loop, _ = make_loop(tmp_path, [broken, MockProvider.text_turn("知道了")],
                        tools=[EchoTool()])
    events = await collect(loop.run("hi"))
    results = [e for e in events if isinstance(e, ev.ToolResultReceived)]
    assert results[0].is_error                     # 解析失败→校验拒绝→错误即观察
    assert events[-1].reason == "completed"        # 循环继续，模型有机会修正


async def test_token_budget_stops_loop(tmp_path):
    script = [MockProvider.tool_turn("echo", {"text": "a"}),
              MockProvider.text_turn("不会到这里")]
    loop, _ = make_loop(tmp_path, script, tools=[EchoTool()],
                        ledger=TokenLedger(max_api_calls=1))
    events = await collect(loop.run("hi"))
    assert events[-1].reason == "token_budget"


async def test_max_turns(tmp_path):
    script = [MockProvider.tool_turn("echo", {"text": str(i)}) for i in range(3)]
    loop, _ = make_loop(tmp_path, script, tools=[EchoTool()], max_turns=3)
    events = await collect(loop.run("hi"))
    assert events[-1].reason == "max_turns"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_accumulator.py tests/integration/test_engine.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'runtime.engine'`）

- [ ] **Step 4: 实现 `runtime/engine.py`**

```python
"""runtime/engine.py — StreamAccumulator + AgentLoop：Mono 运行时的心脏

AgentLoop 是异步生成器：一切（流式文本、工具进度、预算警告、错误）都以事件 yield 给消费者；
控制指令通过 ControlPlane 收件箱在安全点反向注入。supervisor/control 均为可选依赖（None 即关闭）。
"""
from __future__ import annotations

import json
from typing import Optional

from providers.base import (MessageEnd, ProviderError, TextDelta, ThinkingDelta,
                            ToolInputDelta, ToolUseEnd, ToolUseStart)
from runtime.blocks import (Message, TextBlock, ThinkingBlock, ToolResultBlock,
                            ToolUseBlock)
from runtime.context import ContextAssembler, RetryPolicy, TokenLedger
from runtime.control import Abort, Approve, Pause, Steer
from runtime.events import (AgentEnded, AgentStarted, ApprovalRequested,
                            AssistantMessageEnd, ContextCompacted, ErrorEvent,
                            InferenceRetrying, Paused, Resumed, Steered,
                            SupervisorInjected, TextDeltaEvent, ThinkingDeltaEvent,
                            ToolCallStarted, ToolExecutionStarted,
                            ToolResultReceived, TurnEnded, TurnStarted)
from runtime.executor import ToolExecutor, ToolRegistry
from runtime.state import SessionState


class StreamAccumulator:
    """把 Provider 的标准化流事件累积成一条完整的 assistant Message。
    按 id 跟踪打开的工具块，支持多工具交错（OpenAI 风格）。"""

    def __init__(self):
        self.blocks = []
        self._text = ""
        self._thinking = ""
        self._open_tools = {}      # id → {"name", "buf"}
        self.usage = None
        self.stop_reason = None

    def _flush_text(self):
        if self._thinking:
            self.blocks.append(ThinkingBlock(thinking=self._thinking))
            self._thinking = ""
        if self._text:
            self.blocks.append(TextBlock(text=self._text))
            self._text = ""

    def feed(self, event):
        if isinstance(event, TextDelta):
            self._text += event.text
        elif isinstance(event, ThinkingDelta):
            self._thinking += event.thinking
        elif isinstance(event, ToolUseStart):
            self._flush_text()
            self._open_tools[event.id] = {"name": event.name, "buf": ""}
        elif isinstance(event, ToolInputDelta):
            if event.id in self._open_tools:
                self._open_tools[event.id]["buf"] += event.partial_json
        elif isinstance(event, ToolUseEnd):
            entry = self._open_tools.pop(event.id, None)
            if entry is not None:
                raw = entry["buf"].strip()
                try:
                    parsed = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    parsed = {"__parse_error__": raw}   # 交给参数校验拒绝，错误即观察
                self.blocks.append(ToolUseBlock(name=entry["name"], input=parsed, id=event.id))
        elif isinstance(event, MessageEnd):
            self.usage = event.usage
            self.stop_reason = event.stop_reason

    def result(self) -> Message:
        self._flush_text()
        return Message.assistant(self.blocks, usage=self.usage)


class AgentLoop:
    def __init__(self, provider, registry=None, *,
                 system_prompt: str = "You are Mono, a helpful agent.",
                 model: str = "mock-model", max_tokens: int = 4096, max_turns: int = 10,
                 state: Optional[SessionState] = None,
                 ledger: Optional[TokenLedger] = None,
                 executor: Optional[ToolExecutor] = None,
                 assembler: Optional[ContextAssembler] = None,
                 supervisor=None, control=None,
                 retry_policy: Optional[RetryPolicy] = None):
        self.provider = provider
        self.registry = registry or ToolRegistry()
        self.state = state or SessionState()
        self.ledger = ledger or TokenLedger()
        self.executor = executor or ToolExecutor(self.registry)
        self.assembler = assembler or ContextAssembler(
            system_prompt=system_prompt, registry=self.registry,
            model=model, max_tokens=max_tokens)
        self.supervisor = supervisor
        self.control = control
        self.retry = retry_policy or RetryPolicy()
        self.max_turns = max_turns

    def _needs_approval(self, call: ToolUseBlock) -> bool:
        return bool(getattr(self.registry.get(call.name), "requires_approval", False))

    async def run(self, user_input: str):
        self.state.append(Message.user(user_input))
        yield AgentStarted(session_id=self.state.session_id)
        reason = "max_turns"
        try:
            for turn in range(self.max_turns):
                # ── 安全点 1：控制指令（转向 / 暂停 / 中断）──
                if self.control is not None:
                    for cmd in self.control.drain_nowait():
                        if isinstance(cmd, Steer):
                            self.state.append(Message.user(cmd.text))
                            yield Steered(text=cmd.text)
                        elif isinstance(cmd, Pause):
                            yield Paused()
                            resumed = await self.control.wait_resume()
                            if not isinstance(resumed, Abort):
                                yield Resumed()
                    if self.control.abort_requested:
                        reason = "user_abort"
                        break

                # ── 任务级预算检查 ──
                if not self.ledger.budget_ok():
                    reason = "token_budget"
                    break

                yield TurnStarted(turn=turn)

                # ── 上下文组装（可能触发压缩）──
                request, compacted = self.assembler.build(self.state)
                if compacted is not None:
                    yield ContextCompacted(before_tokens=compacted[0],
                                           after_tokens=compacted[1])

                # ── 流式推理（带重试；中断旁路检查）──
                acc = StreamAccumulator()
                attempt = 0
                fatal: Optional[ProviderError] = None
                aborted = False
                while True:
                    acc = StreamAccumulator()
                    try:
                        async for sev in self.provider.stream(request):
                            if self.control is not None and self.control.abort_requested:
                                aborted = True
                                break
                            acc.feed(sev)
                            if isinstance(sev, TextDelta):
                                yield TextDeltaEvent(text=sev.text)
                            elif isinstance(sev, ThinkingDelta):
                                yield ThinkingDeltaEvent(thinking=sev.thinking)
                            elif isinstance(sev, ToolUseStart):
                                yield ToolCallStarted(tool_use_id=sev.id, name=sev.name)
                        break
                    except ProviderError as e:
                        attempt += 1
                        if not self.retry.should_retry(e, attempt):
                            fatal = e
                            break
                        delay = self.retry.backoff_for(e, attempt)
                        yield InferenceRetrying(attempt=attempt, delay=delay)
                        await self.retry.sleep(delay)
                if aborted:
                    reason = "user_abort"   # 半成品 assistant 消息直接丢弃
                    break
                if fatal is not None:
                    yield ErrorEvent(error_type=type(fatal).__name__, message=str(fatal))
                    reason = "provider_error"
                    break

                message = acc.result()
                if acc.usage is not None:
                    self.ledger.record(acc.usage)
                self.state.append(message)
                yield AssistantMessageEnd(stop_reason=acc.stop_reason or "end_turn")

                calls = message.get_tool_calls()
                if not calls:
                    reason = "completed"
                    break

                # ── 审批门（双向：发出请求事件，await 控制平面回传决定）──
                needs = [c for c in calls if self._needs_approval(c)]
                approved = [c for c in calls if c not in needs]
                denied = []
                if needs:
                    if self.control is None:
                        approved += needs
                    else:
                        yield ApprovalRequested(calls=needs)
                        pending = {c.id for c in needs}
                        approved_ids = set()
                        while pending:
                            decision = await self.control.wait_decision()
                            if isinstance(decision, Abort):
                                aborted = True
                                break
                            ids = set(decision.ids) if decision.ids is not None else set(pending)
                            if isinstance(decision, Approve):
                                approved_ids |= ids & pending
                            pending -= ids
                        if aborted:
                            reason = "user_abort"
                            break
                        approved += [c for c in needs if c.id in approved_ids]
                        denied = [c for c in needs if c.id not in approved_ids]

                # ── 并发执行，错误即观察；结果顺序与请求一致 ──
                for c in approved:
                    yield ToolExecutionStarted(tool_use_id=c.id, name=c.name)
                results = await self.executor.execute_all(approved)
                results += [ToolResultBlock(tool_use_id=c.id, content="用户拒绝执行该工具。",
                                            is_error=True, error_type="PermissionDenied")
                            for c in denied]
                by_id = {r.tool_use_id: r for r in results}
                ordered = [by_id[c.id] for c in calls]
                names = {c.id: c.name for c in calls}
                for r in ordered:
                    yield ToolResultReceived(tool_use_id=r.tool_use_id,
                                             name=names[r.tool_use_id],
                                             is_error=r.is_error,
                                             content_preview=r.content[:120])
                self.state.append(Message.tool_results(ordered))

                # ── Supervisor 体检 ──
                if self.supervisor is not None:
                    verdict = self.supervisor.review(self.state)
                    if verdict.action == "terminate":
                        reason = verdict.reason or "supervisor_terminate"
                        break
                    if verdict.action == "inject":
                        self.state.append(Message.user(verdict.message))
                        yield SupervisorInjected(reason=verdict.reason or "supervisor")

                yield TurnEnded(turn=turn)
        except Exception as e:          # 兜底：未捕获异常不会让事件流静默消失
            yield ErrorEvent(error_type=type(e).__name__, message=str(e))
            reason = "fatal"
        yield AgentEnded(reason=reason)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_accumulator.py tests/integration/test_engine.py -v`
Expected: 12 passed

- [ ] **Step 6: 全量回归**

Run: `python -m pytest`
Expected: 全部通过

- [ ] **Step 7: Commit**

```bash
git add runtime/engine.py tests/unit/test_accumulator.py tests/integration/test_engine.py
git commit -m "feat(runtime): AgentLoop async-generator core with streaming, retry, approval gate"
```

---

### Task 9: Anthropic 适配器 `providers/anthropic.py`

**Files:**
- Create: `providers/anthropic.py`
- Test: `tests/unit/test_anthropic_provider.py`

- [ ] **Step 1: 写失败测试（httpx.MockTransport 回放 SSE fixture）**

```python
"""tests/unit/test_anthropic_provider.py"""
import json
import os

import httpx
import pytest

from providers.anthropic import AnthropicProvider
from providers.base import (MessageEnd, MessageStart, ModelRequest, RateLimitError,
                            TextDelta, ToolInputDelta, ToolUseEnd, ToolUseStart)
from runtime.blocks import Message, TextBlock, ToolResultBlock, ToolUseBlock

TEXT_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":12,"output_tokens":1}}}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}\n\n'
    'data: {"type":"content_block_stop","index":0}\n\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}\n\n'
    'data: {"type":"message_stop"}\n\n'
)

TOOL_SSE = (
    'data: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":20,"output_tokens":1}}}\n\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_01","name":"run_command"}}\n\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"command\\""}}\n\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":": \\"dir\\"}"}}\n\n'
    'data: {"type":"content_block_stop","index":0}\n\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":15}}\n\n'
    'data: {"type":"message_stop"}\n\n'
)


def make_provider(body: str, status: int = 200, headers: dict = None):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(status, content=body.encode("utf-8"),
                              headers=headers or {"content-type": "text/event-stream"})

    provider = AnthropicProvider(api_key="test-key",
                                 transport=httpx.MockTransport(handler))
    return provider, captured


def make_request(messages=None):
    return ModelRequest(system="你是助手", messages=messages or [Message.user("hi")],
                        tools=[], model="claude-sonnet-4-6", max_tokens=64)


async def test_text_stream():
    provider, captured = make_provider(TEXT_SSE)
    events = [e async for e in provider.stream(make_request())]
    assert isinstance(events[0], MessageStart)
    assert isinstance(events[1], TextDelta) and events[1].text == "你好"
    end = events[-1]
    assert isinstance(end, MessageEnd) and end.stop_reason == "end_turn"
    assert end.usage.input_tokens == 12 and end.usage.output_tokens == 7
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["system"] == "你是助手"


async def test_tool_stream():
    provider, _ = make_provider(TOOL_SSE)
    events = [e async for e in provider.stream(make_request())]
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["MessageStart", "ToolUseStart", "ToolInputDelta",
                     "ToolInputDelta", "ToolUseEnd", "MessageEnd"]
    assert events[1].id == "toolu_01" and events[1].name == "run_command"
    assert events[2].id == "toolu_01"
    assert events[-1].stop_reason == "tool_use"


async def test_429_maps_to_rate_limit():
    provider, _ = make_provider('{"error": "rate"}', status=429,
                                headers={"retry-after": "7"})
    with pytest.raises(RateLimitError) as ei:
        async for _ in provider.stream(make_request()):
            pass
    assert ei.value.retry_after == 7.0


async def test_history_serialization_includes_tool_blocks():
    provider, captured = make_provider(TEXT_SSE)
    history = [
        Message.user("查目录"),
        Message.assistant([TextBlock(text="我来"),
                           ToolUseBlock(name="run_command", input={"command": "dir"}, id="t1")]),
        Message.tool_results([ToolResultBlock(tool_use_id="t1", content="file.txt")]),
    ]
    async for _ in provider.stream(make_request(messages=history)):
        pass
    sent = captured["payload"]["messages"]
    assert sent[1]["content"][1] == {"type": "tool_use", "id": "t1",
                                     "name": "run_command", "input": {"command": "dir"}}
    assert sent[2]["content"][0]["type"] == "tool_result"
    assert sent[2]["content"][0]["tool_use_id"] == "t1"


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="需要真实 API key")
async def test_live_smoke():
    provider = AnthropicProvider()
    events = [e async for e in provider.stream(make_request())]
    assert isinstance(events[-1], MessageEnd)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_anthropic_provider.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现**

```python
"""providers/anthropic.py — Anthropic Messages API 适配器（httpx 手写 SSE 解析）"""
from __future__ import annotations

import json
import os
from typing import AsyncIterator, List, Optional

import httpx

from providers.base import (MessageEnd, MessageStart, ModelProvider, ModelRequest,
                            ProviderAuthError, ProviderBadRequestError,
                            ProviderServerError, ProviderTimeoutError,
                            RateLimitError, StreamEvent, TextDelta, ThinkingDelta,
                            ToolInputDelta, ToolUseEnd, ToolUseStart)
from runtime.blocks import (Message, TextBlock, ToolResultBlock, ToolUseBlock,
                            Usage)

API_VERSION = "2023-06-01"


def _map_status(status: int, headers, body: str) -> Exception:
    if status == 429:
        return RateLimitError(retry_after=float(headers.get("retry-after", 30)),
                              message=body[:200])
    if status in (401, 403):
        return ProviderAuthError(body[:200])
    if status == 400:
        return ProviderBadRequestError(body[:200])
    return ProviderServerError(f"HTTP {status}: {body[:200]}")


class AnthropicProvider(ModelProvider):
    def __init__(self, api_key: Optional[str] = None,
                 base_url: str = "https://api.anthropic.com",
                 timeout: float = 60.0, transport=None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    def _serialize_messages(self, messages: List[Message]) -> List[dict]:
        out = []
        for m in messages:
            blocks = []
            for b in m.content:
                if isinstance(b, TextBlock):
                    blocks.append({"type": "text", "text": b.text})
                elif isinstance(b, ToolUseBlock):
                    blocks.append({"type": "tool_use", "id": b.id,
                                   "name": b.name, "input": b.input})
                elif isinstance(b, ToolResultBlock):
                    blocks.append({"type": "tool_result", "tool_use_id": b.tool_use_id,
                                   "content": b.content, "is_error": b.is_error})
                # ThinkingBlock 不回传
            out.append({"role": m.role, "content": blocks})
        return out

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        payload = {"model": request.model,
                   "system": request.system,
                   "messages": self._serialize_messages(request.messages),
                   "max_tokens": request.max_tokens,
                   "stream": True}
        if request.tools:
            payload["tools"] = request.tools          # 本就是 Anthropic 工具格式
        headers = {"x-api-key": self.api_key,
                   "anthropic-version": API_VERSION,
                   "content-type": "application/json"}
        input_tokens = 0
        output_tokens = 0
        stop_reason: Optional[str] = None
        tool_by_index: dict = {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout,
                                         transport=self.transport) as client:
                async with client.stream("POST", f"{self.base_url}/v1/messages",
                                         json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        raise _map_status(resp.status_code, resp.headers, body)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = json.loads(line[5:].strip())
                        t = data.get("type")
                        if t == "message_start":
                            usage = data["message"].get("usage", {})
                            input_tokens = usage.get("input_tokens", 0)
                            yield MessageStart(model=data["message"].get("model", request.model))
                        elif t == "content_block_start":
                            cb = data["content_block"]
                            if cb["type"] == "tool_use":
                                tool_by_index[data["index"]] = cb["id"]
                                yield ToolUseStart(id=cb["id"], name=cb["name"])
                        elif t == "content_block_delta":
                            d = data["delta"]
                            if d["type"] == "text_delta":
                                yield TextDelta(text=d["text"])
                            elif d["type"] == "thinking_delta":
                                yield ThinkingDelta(thinking=d["thinking"])
                            elif d["type"] == "input_json_delta":
                                tid = tool_by_index.get(data["index"])
                                if tid:
                                    yield ToolInputDelta(id=tid, partial_json=d["partial_json"])
                        elif t == "content_block_stop":
                            tid = tool_by_index.pop(data["index"], None)
                            if tid:
                                yield ToolUseEnd(id=tid)
                        elif t == "message_delta":
                            stop_reason = data["delta"].get("stop_reason") or stop_reason
                            output_tokens = data.get("usage", {}).get("output_tokens", output_tokens)
                        elif t == "message_stop":
                            yield MessageEnd(stop_reason=stop_reason or "end_turn",
                                             usage=Usage(input_tokens, output_tokens))
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(str(e)) from e
        except httpx.HTTPError as e:
            raise ProviderServerError(str(e)) from e
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_anthropic_provider.py -v`
Expected: 4 passed, 1 skipped（live 测试无 key 跳过）

- [ ] **Step 5: Commit**

```bash
git add providers/anthropic.py tests/unit/test_anthropic_provider.py
git commit -m "feat(providers): Anthropic SSE streaming adapter"
```

---

### Task 10: OpenAI 兼容适配器 `providers/openai_compat.py`

**Files:**
- Create: `providers/openai_compat.py`
- Test: `tests/unit/test_openai_provider.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/unit/test_openai_provider.py"""
import json

import httpx
import pytest

from providers.base import (MessageEnd, MessageStart, ModelRequest, RateLimitError,
                            TextDelta, ToolInputDelta, ToolUseEnd, ToolUseStart)
from providers.openai_compat import OpenAICompatProvider
from runtime.blocks import Message, TextBlock, ToolResultBlock, ToolUseBlock

TEXT_CHUNKS = "\n\n".join([
    'data: {"id":"1","model":"deepseek-chat","choices":[{"index":0,"delta":{"role":"assistant","content":"你"}}]}',
    'data: {"choices":[{"index":0,"delta":{"content":"好"}}]}',
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
    'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":3}}',
    'data: [DONE]',
]) + "\n\n"

TOOL_CHUNKS = "\n\n".join([
    'data: {"id":"1","model":"deepseek-chat","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"run_command","arguments":""}}]}}]}',
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"command\\":"}}]}}]}',
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"dir\\"}"}}]}}]}',
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
    'data: {"choices":[],"usage":{"prompt_tokens":15,"completion_tokens":8}}',
    'data: [DONE]',
]) + "\n\n"


def make_provider(body: str, status: int = 200, headers: dict = None):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(status, content=body.encode("utf-8"),
                              headers=headers or {"content-type": "text/event-stream"})

    provider = OpenAICompatProvider(api_key="test-key", base_url="https://api.example.com",
                                    transport=httpx.MockTransport(handler))
    return provider, captured


def make_request(messages=None, tools=None):
    return ModelRequest(system="你是助手", messages=messages or [Message.user("hi")],
                        tools=tools or [], model="deepseek-chat", max_tokens=64)


async def test_text_stream():
    provider, captured = make_provider(TEXT_CHUNKS)
    events = [e async for e in provider.stream(make_request())]
    assert isinstance(events[0], MessageStart)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["你", "好"]
    end = events[-1]
    assert isinstance(end, MessageEnd) and end.stop_reason == "end_turn"
    assert end.usage.input_tokens == 9 and end.usage.output_tokens == 3
    assert captured["payload"]["messages"][0] == {"role": "system", "content": "你是助手"}


async def test_tool_stream():
    provider, _ = make_provider(TOOL_CHUNKS)
    events = [e async for e in provider.stream(make_request())]
    starts = [e for e in events if isinstance(e, ToolUseStart)]
    assert starts[0].id == "call_1" and starts[0].name == "run_command"
    deltas = [e for e in events if isinstance(e, ToolInputDelta)]
    assert "".join(d.partial_json for d in deltas) == '{"command":"dir"}'
    assert any(isinstance(e, ToolUseEnd) and e.id == "call_1" for e in events)
    assert events[-1].stop_reason == "tool_use"      # finish_reason 映射


async def test_history_serialization_roundtrip():
    provider, captured = make_provider(TEXT_CHUNKS)
    history = [
        Message.user("查目录"),
        Message.assistant([TextBlock(text="我来"),
                           ToolUseBlock(name="run_command", input={"command": "dir"}, id="call_9")]),
        Message.tool_results([ToolResultBlock(tool_use_id="call_9", content="file.txt")]),
    ]
    async for _ in provider.stream(make_request(messages=history)):
        pass
    sent = captured["payload"]["messages"]
    assistant = sent[2]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "call_9"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"command": "dir"}
    tool_msg = sent[3]
    assert tool_msg == {"role": "tool", "tool_call_id": "call_9", "content": "file.txt"}


async def test_tools_wrapped_in_function_format():
    provider, captured = make_provider(TEXT_CHUNKS)
    tools = [{"name": "echo", "description": "回显",
              "input_schema": {"type": "object", "properties": {}, "required": []}}]
    async for _ in provider.stream(make_request(tools=tools)):
        pass
    sent_tool = captured["payload"]["tools"][0]
    assert sent_tool["type"] == "function"
    assert sent_tool["function"]["name"] == "echo"
    assert sent_tool["function"]["parameters"]["type"] == "object"


async def test_429_maps_to_rate_limit():
    provider, _ = make_provider('{"error": "rate"}', status=429,
                                headers={"retry-after": "3"})
    with pytest.raises(RateLimitError):
        async for _ in provider.stream(make_request()):
            pass
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_openai_provider.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现**

```python
"""providers/openai_compat.py — OpenAI 兼容端点适配器（DeepSeek/Kimi/智谱/通义等）

职责：块模型 ↔ tool_calls/tool 角色消息双向转换；chunk 流 → 标准化 StreamEvent。
"""
from __future__ import annotations

import json
import os
from typing import AsyncIterator, List, Optional

import httpx

from providers.base import (MessageEnd, MessageStart, ModelProvider, ModelRequest,
                            ProviderAuthError, ProviderBadRequestError,
                            ProviderServerError, ProviderTimeoutError,
                            RateLimitError, StreamEvent, TextDelta, ThinkingDelta,
                            ToolInputDelta, ToolUseEnd, ToolUseStart)
from runtime.blocks import Message, ToolResultBlock, Usage

STOP_MAP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


def _map_status(status: int, headers, body: str) -> Exception:
    if status == 429:
        return RateLimitError(retry_after=float(headers.get("retry-after", 30)),
                              message=body[:200])
    if status in (401, 403):
        return ProviderAuthError(body[:200])
    if status == 400:
        return ProviderBadRequestError(body[:200])
    return ProviderServerError(f"HTTP {status}: {body[:200]}")


class OpenAICompatProvider(ModelProvider):
    def __init__(self, api_key: Optional[str] = None,
                 base_url: str = "https://api.deepseek.com",
                 timeout: float = 60.0, transport=None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    def _serialize(self, system: str, messages: List[Message]) -> List[dict]:
        out: List[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "user":
                results = [b for b in m.content if isinstance(b, ToolResultBlock)]
                for r in results:                    # tool_result → tool 角色消息
                    out.append({"role": "tool", "tool_call_id": r.tool_use_id,
                                "content": r.content})
                text = m.get_text()
                if text:
                    out.append({"role": "user", "content": text})
            else:
                entry: dict = {"role": "assistant", "content": m.get_text() or None}
                calls = m.get_tool_calls()
                if calls:
                    entry["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name,
                                      "arguments": json.dumps(c.input, ensure_ascii=False)}}
                        for c in calls]
                out.append(entry)
        return out

    @staticmethod
    def _serialize_tools(tools: List[dict]) -> List[dict]:
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in tools]

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        payload = {"model": request.model,
                   "messages": self._serialize(request.system, request.messages),
                   "max_tokens": request.max_tokens,
                   "stream": True,
                   "stream_options": {"include_usage": True}}
        if request.tools:
            payload["tools"] = self._serialize_tools(request.tools)
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        open_tools: dict = {}       # index → tool_call id
        emit_order: list = []
        finish: Optional[str] = None
        usage = Usage(0, 0)
        started = False
        try:
            async with httpx.AsyncClient(timeout=self.timeout,
                                         transport=self.transport) as client:
                async with client.stream("POST", f"{self.base_url}/chat/completions",
                                         json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        raise _map_status(resp.status_code, resp.headers, body)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        if chunk.get("usage"):
                            u = chunk["usage"]
                            usage = Usage(u.get("prompt_tokens", 0),
                                          u.get("completion_tokens", 0))
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        if not started:
                            yield MessageStart(model=chunk.get("model", request.model))
                            started = True
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        if delta.get("reasoning_content"):       # DeepSeek 推理模型
                            yield ThinkingDelta(thinking=delta["reasoning_content"])
                        if delta.get("content"):
                            yield TextDelta(text=delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            if idx not in open_tools:
                                tid = tc.get("id") or f"call_{idx}"
                                open_tools[idx] = tid
                                emit_order.append(tid)
                                yield ToolUseStart(
                                    id=tid,
                                    name=(tc.get("function") or {}).get("name", ""))
                            args = (tc.get("function") or {}).get("arguments")
                            if args:
                                yield ToolInputDelta(id=open_tools[idx], partial_json=args)
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(str(e)) from e
        except httpx.HTTPError as e:
            raise ProviderServerError(str(e)) from e
        for tid in emit_order:                       # 流结束才能确定工具参数完整
            yield ToolUseEnd(id=tid)
        yield MessageEnd(stop_reason=STOP_MAP.get(finish, "end_turn"), usage=usage)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_openai_provider.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add providers/openai_compat.py tests/unit/test_openai_provider.py
git commit -m "feat(providers): OpenAI-compatible streaming adapter with tool_calls translation"
```

---

### Task 11: 监督者 `runtime/supervisor.py`

**Files:**
- Create: `runtime/supervisor.py`
- Test: `tests/unit/test_supervisor.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/unit/test_supervisor.py"""
from providers.mock import MockProvider
from runtime.blocks import Message, TextBlock, ToolUseBlock
from runtime.context import RetryPolicy
from runtime.engine import AgentLoop
from runtime import events as ev
from runtime.executor import ToolRegistry
from runtime.state import SessionState
from runtime.supervisor import (ConstraintValidator, ReflectionStep,
                                RepetitionDetector, Supervisor, Verdict)
from tests.helpers import EchoTool, FakeSleep, collect


def _state_with_repeated_calls(tmp_path, n):
    state = SessionState(transcript_dir=str(tmp_path))
    state.append(Message.user("开始"))
    for _ in range(n):
        state.append(Message.assistant(
            [ToolUseBlock(name="bash", input={"cmd": "ls"})]))
        state.append(Message.tool_results([]))
    return state


def test_repetition_detector_injects(tmp_path):
    detector = RepetitionDetector(window=5, threshold=3)
    v = detector.check(_state_with_repeated_calls(tmp_path, 3))
    assert v.action == "inject" and "bash" in v.message
    v2 = detector.check(_state_with_repeated_calls(tmp_path, 2))
    assert v2.action == "continue"


def test_constraint_validator_terminates(tmp_path):
    validator = ConstraintValidator(max_total_tool_calls=2)
    v = validator.check(_state_with_repeated_calls(tmp_path, 3))
    assert v.action == "terminate" and "max_tool_calls" in v.reason


def test_wall_time_constraint(tmp_path):
    fake_now = [0.0]
    validator = ConstraintValidator(max_wall_seconds=10.0, clock=lambda: fake_now[0])
    state = _state_with_repeated_calls(tmp_path, 1)
    assert validator.check(state).action == "continue"
    fake_now[0] = 11.0
    assert validator.check(state).action == "terminate"


def test_reflection_step_every_n(tmp_path):
    step = ReflectionStep(every_n_turns=2)
    state = _state_with_repeated_calls(tmp_path, 1)
    assert step.check(state).action == "continue"   # 第 1 次
    v = step.check(state)                            # 第 2 次
    assert v.action == "inject" and "反思" in v.message


def test_supervisor_first_non_continue_wins(tmp_path):
    sup = Supervisor(checkers=[RepetitionDetector(threshold=3),
                               ConstraintValidator(max_total_tool_calls=1)])
    v = sup.review(_state_with_repeated_calls(tmp_path, 3))
    assert v.action == "inject"        # 重复检测排在前面，先生效


async def test_engine_integration_injects_correction(tmp_path):
    script = ([MockProvider.tool_turn("echo", {"text": "same"}) for _ in range(3)]
              + [MockProvider.text_turn("我换个方法")])
    registry = ToolRegistry()
    registry.register(EchoTool())
    loop = AgentLoop(MockProvider(script), registry,
                     state=SessionState(transcript_dir=str(tmp_path)),
                     retry_policy=RetryPolicy(sleep=FakeSleep()),
                     supervisor=Supervisor(checkers=[RepetitionDetector(threshold=3)]))
    events = await collect(loop.run("反复回显"))
    assert any(isinstance(e, ev.SupervisorInjected) for e in events)
    injected = [m for m in loop.state.messages
                if m.role == "user" and "重复" in m.get_text()
                or (m.role == "user" and "相同参数" in m.get_text())]
    assert injected
    assert events[-1].reason == "completed"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_supervisor.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现**

```python
"""runtime/supervisor.py — 漂移检测与约束：每轮 turn 末尾给状态做体检

裁决语义：continue（无事）| inject（注入纠正消息）| terminate（终止运行）。
检查器可插拔，按序执行，第一个非 continue 的裁决生效。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class Verdict:
    action: str = "continue"          # continue | inject | terminate
    message: Optional[str] = None     # inject 时注入的用户消息
    reason: Optional[str] = None


class RepetitionDetector:
    """最常见的漂移形态：原地打转。窗口内相同 (工具, 参数) 重复 ≥ 阈值 → 注入纠正。"""

    def __init__(self, window: int = 5, threshold: int = 3):
        self.window = window
        self.threshold = threshold

    def check(self, state) -> Verdict:
        recent = [m for m in state.messages[-self.window * 2:] if m.role == "assistant"]
        counts: dict = {}
        for m in recent:
            for c in m.get_tool_calls():
                key = (c.name, json.dumps(c.input, sort_keys=True, ensure_ascii=False))
                counts[key] = counts.get(key, 0) + 1
        for (name, _args), n in counts.items():
            if n >= self.threshold:
                return Verdict(
                    action="inject", reason=f"repetition:{name}",
                    message=(f"你已经 {n} 次以相同参数调用工具 {name}，重复相同操作通常说明"
                             f"当前方法无效。请停下来分析失败原因，换一种不同的方法，"
                             f"或者明确说明你遇到的障碍。"))
        return Verdict()


class ConstraintValidator:
    """硬约束：工具调用总数、墙钟时间。超限直接终止。"""

    def __init__(self, max_total_tool_calls: int = 50,
                 max_wall_seconds: float = 3600.0,
                 clock: Callable[[], float] = time.monotonic):
        self.max_total_tool_calls = max_total_tool_calls
        self.max_wall_seconds = max_wall_seconds
        self._clock = clock
        self._started = clock()

    def check(self, state) -> Verdict:
        total = sum(len(m.get_tool_calls())
                    for m in state.messages if m.role == "assistant")
        if total > self.max_total_tool_calls:
            return Verdict(action="terminate",
                           reason=f"constraint:max_tool_calls({total})")
        if self._clock() - self._started > self.max_wall_seconds:
            return Verdict(action="terminate", reason="constraint:max_wall_time")
        return Verdict()


class ReflectionStep:
    """可选：每 N 轮注入反思提示，要求模型对照原始目标自评。默认不启用。"""

    def __init__(self, every_n_turns: int = 5):
        self.every = every_n_turns
        self._reviews = 0

    def check(self, state) -> Verdict:
        self._reviews += 1
        if self._reviews % self.every != 0:
            return Verdict()
        goal = next((m.get_text() for m in state.messages if m.role == "user"), "")
        return Verdict(action="inject", reason="reflection",
                       message=(f"### 反思检查点\n原始目标：{goal}\n"
                                f"请简要回答：1) 是否仍在朝目标推进？"
                                f"2) 是否需要调整方法？回答后继续执行。"))


class Supervisor:
    def __init__(self, checkers: Optional[List] = None):
        self.checkers = (list(checkers) if checkers is not None
                         else [RepetitionDetector(), ConstraintValidator()])

    def review(self, state) -> Verdict:
        for checker in self.checkers:
            verdict = checker.check(state)
            if verdict.action != "continue":
                return verdict
        return Verdict()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_supervisor.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add runtime/supervisor.py tests/unit/test_supervisor.py
git commit -m "feat(runtime): supervisor with repetition/constraint/reflection checkers"
```

---

### Task 12: 控制平面 × 引擎集成测试

**Files:**
- Test: `tests/integration/test_control.py`（纯测试任务，验证 Task 5+8 的协作）

- [ ] **Step 1: 写集成测试**

```python
"""tests/integration/test_control.py — 中断/转向/审批/暂停 的端到端验证"""
from providers.mock import MockProvider
from runtime import events as ev
from runtime.context import RetryPolicy
from runtime.control import Abort, Approve, ControlPlane, Deny, Pause, Resume, Steer
from runtime.engine import AgentLoop
from runtime.executor import ToolRegistry
from runtime.state import SessionState
from tests.helpers import DangerTool, EchoTool, FakeSleep


def make_loop(tmp_path, script, tools=(), **kwargs):
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    control = ControlPlane()
    loop = AgentLoop(MockProvider(script), registry, control=control,
                     state=SessionState(transcript_dir=str(tmp_path)),
                     retry_policy=RetryPolicy(sleep=FakeSleep()), **kwargs)
    return loop, control


async def test_abort_mid_stream_discards_partial(tmp_path):
    loop, control = make_loop(tmp_path, [MockProvider.text_turn("很长的回复")])
    events = []
    async for e in loop.run("hi"):
        events.append(e)
        if isinstance(e, ev.TextDeltaEvent):
            control.submit(Abort())          # 第一个文本增量后立即中断
    assert events[-1].reason == "user_abort"
    assert len(loop.state.messages) == 1     # 半成品 assistant 消息被丢弃


async def test_steer_appends_user_message(tmp_path):
    loop, control = make_loop(tmp_path, [MockProvider.text_turn("收到")])
    control.submit(Steer(text="注意：优先处理报错"))
    events = [e async for e in loop.run("帮我修代码")]
    assert any(isinstance(e, ev.Steered) for e in events)
    first_request = loop.provider.requests[0]
    user_texts = [m.get_text() for m in first_request.messages if m.role == "user"]
    assert user_texts == ["帮我修代码", "注意：优先处理报错"]   # 只追加，不改历史


async def test_approval_approve_executes_tool(tmp_path):
    script = [MockProvider.tool_turn("danger", {"text": "rm"}, tool_id="d1"),
              MockProvider.text_turn("执行完毕")]
    loop, control = make_loop(tmp_path, script, tools=[DangerTool()])
    events = []
    async for e in loop.run("执行危险操作"):
        events.append(e)
        if isinstance(e, ev.ApprovalRequested):
            assert e.calls[0].name == "danger"
            control.submit(Approve())        # 批准全部
    results = [e for e in events if isinstance(e, ev.ToolResultReceived)]
    assert results[0].content_preview == "echo:rm" and not results[0].is_error
    assert events[-1].reason == "completed"


async def test_approval_deny_feeds_error_to_model(tmp_path):
    script = [MockProvider.tool_turn("danger", {"text": "rm"}, tool_id="d1"),
              MockProvider.text_turn("好的，不执行了")]
    loop, control = make_loop(tmp_path, script, tools=[DangerTool()])
    events = []
    async for e in loop.run("执行危险操作"):
        events.append(e)
        if isinstance(e, ev.ApprovalRequested):
            control.submit(Deny())           # 拒绝全部
    results = [e for e in events if isinstance(e, ev.ToolResultReceived)]
    assert results[0].is_error
    # 模型在下一轮能看到拒绝结果（错误即观察）
    second = loop.provider.requests[1]
    assert any(getattr(b, "error_type", None) == "PermissionDenied"
               for m in second.messages for b in m.content)
    assert events[-1].reason == "completed"


async def test_pause_then_resume(tmp_path):
    loop, control = make_loop(tmp_path, [MockProvider.text_turn("继续干活")])
    control.submit(Pause())
    control.submit(Resume())                 # 预先排队：暂停后立即恢复
    events = [e async for e in loop.run("hi")]
    names = [type(e).__name__ for e in events]
    assert "Paused" in names and "Resumed" in names
    assert events[-1].reason == "completed"


async def test_unattended_tools_do_not_need_approval(tmp_path):
    script = [MockProvider.tool_turn("echo", {"text": "safe"}),
              MockProvider.text_turn("done")]
    loop, control = make_loop(tmp_path, script, tools=[EchoTool()])
    events = [e async for e in loop.run("hi")]
    assert not any(isinstance(e, ev.ApprovalRequested) for e in events)
    assert events[-1].reason == "completed"
```

- [ ] **Step 2: 跑测试确认通过（实现已在 Task 5/8 完成，此处应直接通过）**

Run: `python -m pytest tests/integration/test_control.py -v`
Expected: 6 passed。若有失败，说明 Task 5/8 的实现有缺陷——修复实现而不是改测试。

- [ ] **Step 3: 全量回归**

Run: `python -m pytest`
Expected: 全部通过（1 个 live 测试 skipped）

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_control.py
git commit -m "test(runtime): end-to-end control plane scenarios (abort/steer/approval/pause)"
```

---

### Task 13: 演示工具 `tools/builtin.py`

**Files:**
- Create: `tools/builtin.py`
- Test: `tests/unit/test_builtin_tools.py`

- [ ] **Step 1: 写失败测试**

```python
"""tests/unit/test_builtin_tools.py"""
from tools.builtin import ReadFileTool, RunCommandTool


async def test_read_file(tmp_path):
    f = tmp_path / "demo.txt"
    f.write_text("内容123", encoding="utf-8")
    result = await ReadFileTool().call({"path": str(f)})
    assert result.success and "内容123" in result.content


async def test_read_file_missing_is_error():
    result = await ReadFileTool().call({"path": "Z:/不存在/no.txt"})
    assert not result.success and result.error_type is not None


async def test_read_file_truncates(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("x" * 30000, encoding="utf-8")
    result = await ReadFileTool().call({"path": str(f)})
    assert result.success and "[已截断]" in result.content
    assert len(result.content) < 21000


async def test_run_command_echo():
    result = await RunCommandTool().call({"command": "echo mono-test"})
    assert result.success and "mono-test" in result.content


def test_run_command_requires_approval():
    assert RunCommandTool.requires_approval is True
    assert getattr(ReadFileTool, "requires_approval", False) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_builtin_tools.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现**

```python
"""tools/builtin.py — 最小演示工具集（完整工具层属第 5 章，这里只为 runtime 验收服务）"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from core.tool import Tool, ToolResult

MAX_FILE_CHARS = 20_000


class ReadFileTool(Tool):
    timeout_seconds = 10

    def name(self):
        return "read_file"

    def description(self):
        return "读取文本文件内容（UTF-8），超长会截断到 20000 字符"

    def input_schema(self):
        return {"type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"]}

    async def call(self, params):
        start = time.perf_counter()
        try:
            text = Path(params["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ToolResult(success=False, content=str(e),
                              execution_time=time.perf_counter() - start,
                              error_type=type(e).__name__)
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + "\n[已截断]"
        return ToolResult(success=True, content=text,
                          execution_time=time.perf_counter() - start)


class RunCommandTool(Tool):
    timeout_seconds = 30
    requires_approval = True          # 危险工具：引擎审批门依赖此标志

    def name(self):
        return "run_command"

    def description(self):
        return "在系统 shell 中执行命令，返回退出码与合并的 stdout/stderr"

    def input_schema(self):
        return {"type": "object",
                "properties": {"command": {"type": "string", "description": "要执行的 shell 命令"}},
                "required": ["command"]}

    async def call(self, params):
        start = time.perf_counter()
        proc = await asyncio.create_subprocess_shell(
            params["command"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        text = out.decode("utf-8", errors="replace")
        return ToolResult(success=proc.returncode == 0,
                          content=f"[exit {proc.returncode}]\n{text}",
                          execution_time=time.perf_counter() - start,
                          error_type=None if proc.returncode == 0 else "NonZeroExit")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_builtin_tools.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add tools/builtin.py tests/unit/test_builtin_tools.py
git commit -m "feat(tools): read_file and run_command demo tools"
```

---

### Task 14: 终端 REPL `cli/repl.py`

**Files:**
- Create: `cli/repl.py`

说明：REPL 是覆盖在已充分测试的引擎之上的薄 I/O 壳层，不写自动化测试，用下面的手动验收清单验证。按键监听用 stdlib 的 msvcrt（Windows；非 Windows 自动禁用，靠任务自然结束）。

- [ ] **Step 1: 实现**

```python
"""cli/repl.py — Mono 终端 REPL

用法：python -m cli.repl
环境变量：
  MONO_PROVIDER = mock | anthropic | openai   （默认 mock，离线可跑）
  MONO_MODEL    = 模型名（默认随 provider）
  ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENAI_BASE_URL
运行中：Esc 中断；输入文字+回车 转向。命令：/status /resume <id> /quit
"""
from __future__ import annotations

import asyncio
import json
import os

from rich.console import Console
from rich.panel import Panel

from providers.anthropic import AnthropicProvider
from providers.mock import MockProvider
from providers.openai_compat import OpenAICompatProvider
from runtime import events as ev
from runtime.context import TokenLedger
from runtime.control import Abort, Approve, ControlPlane, Deny, Steer
from runtime.engine import AgentLoop
from runtime.executor import ToolRegistry
from runtime.state import SessionState
from tools.builtin import ReadFileTool, RunCommandTool


def make_provider():
    kind = os.environ.get("MONO_PROVIDER", "mock").lower()
    if kind == "anthropic":
        return AnthropicProvider(), os.environ.get("MONO_MODEL", "claude-sonnet-4-6")
    if kind == "openai":
        return (OpenAICompatProvider(
                    api_key=os.environ.get("OPENAI_API_KEY", ""),
                    base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com")),
                os.environ.get("MONO_MODEL", "deepseek-chat"))
    # 离线演示剧本：一次需审批的命令执行 + 一段总结
    script = [MockProvider.tool_turn("run_command", {"command": "echo hello-from-mono"}),
              MockProvider.text_turn("命令执行完成，演示结束。再次输入会提示剧本耗尽，属预期。")]
    return MockProvider(script), "mock-model"


class KeyListener:
    """Windows 下基于 msvcrt 的非阻塞按键监听：Esc → Abort，整行+回车 → Steer。
    审批问答期间挂起，避免与 input() 抢按键。非 Windows 平台自动禁用。"""

    def __init__(self, control: ControlPlane, console: Console):
        self.control = control
        self.console = console
        self.suspended = False
        self._buf = ""
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        try:
            import msvcrt
        except ImportError:
            return
        while True:
            await asyncio.sleep(0.05)
            if self.suspended:
                continue
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch == "\x1b":                      # Esc
                    self.control.submit(Abort())
                    self.console.print("\n[red]⏹ 已请求中断[/red]")
                elif ch in ("\r", "\n"):
                    if self._buf.strip():
                        self.control.submit(Steer(text=self._buf.strip()))
                        self.console.print(f"\n[cyan]↪ 转向消息已注入：{self._buf.strip()}[/cyan]")
                    self._buf = ""
                else:
                    self._buf += ch


async def render_events(run, console: Console, control: ControlPlane,
                        listener: KeyListener):
    async for event in run:
        if isinstance(event, ev.TextDeltaEvent):
            console.print(event.text, end="", highlight=False)
        elif isinstance(event, ev.ThinkingDeltaEvent):
            console.print(f"[dim]{event.thinking}[/dim]", end="", highlight=False)
        elif isinstance(event, ev.ToolCallStarted):
            console.print(f"\n[yellow]🔧 工具调用：{event.name}[/yellow]")
        elif isinstance(event, ev.ApprovalRequested):
            listener.suspended = True
            approved, denied = [], []
            for call in event.calls:
                console.print(Panel(json.dumps(call.input, ensure_ascii=False, indent=2),
                                    title=f"待审批：{call.name}", border_style="red"))
                answer = await asyncio.to_thread(input, "允许执行？[y/n] ")
                (approved if answer.strip().lower() == "y" else denied).append(call.id)
            listener.suspended = False
            if approved:
                control.submit(Approve(ids=approved))
            if denied:
                control.submit(Deny(ids=denied))
        elif isinstance(event, ev.ToolResultReceived):
            style = "red" if event.is_error else "green"
            console.print(f"[{style}]   ↳ {event.name}: {event.content_preview}[/{style}]")
        elif isinstance(event, ev.ContextCompacted):
            console.print(f"\n[magenta]🗜 上下文已压缩 "
                          f"{event.before_tokens}→{event.after_tokens} tokens[/magenta]")
        elif isinstance(event, ev.InferenceRetrying):
            console.print(f"\n[yellow]⟳ 推理重试 #{event.attempt}"
                          f"（{event.delay:.1f}s 后）[/yellow]")
        elif isinstance(event, ev.SupervisorInjected):
            console.print(f"\n[magenta]🧭 监督者注入纠正：{event.reason}[/magenta]")
        elif isinstance(event, ev.Steered):
            pass                                       # 监听器已就地提示
        elif isinstance(event, ev.ErrorEvent):
            console.print(f"\n[red]✖ {event.error_type}: {event.message}[/red]")
        elif isinstance(event, ev.AgentEnded):
            console.print(f"\n[dim]—— 本轮结束（{event.reason}）——[/dim]")


async def main():
    console = Console()
    provider, model = make_provider()
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(RunCommandTool())
    state = SessionState()
    ledger = TokenLedger()
    console.print(Panel(
        f"Mono REPL · provider={type(provider).__name__} · model={model}\n"
        f"运行中：Esc 中断 / 输入文字+回车 转向\n"
        f"命令：/status  /resume <session_id>  /quit",
        title="Mono", border_style="blue"))
    while True:
        try:
            user = (await asyncio.to_thread(input, "\nyou> ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user:
            continue
        if user == "/quit":
            break
        if user == "/status":
            console.print(f"session={state.session_id}  消息数={len(state.messages)}  "
                          f"tokens={ledger.total_tokens}  api_calls={ledger.api_calls}")
            continue
        if user.startswith("/resume "):
            sid = user.split(maxsplit=1)[1]
            try:
                state = SessionState.resume(sid)
                console.print(f"[green]已恢复会话 {sid}（{len(state.messages)} 条消息）[/green]")
            except FileNotFoundError:
                console.print("[red]找不到该会话的转录文件[/red]")
            continue
        control = ControlPlane()
        agent = AgentLoop(provider, registry, state=state, control=control,
                          ledger=ledger, model=model)
        listener = KeyListener(control, console)
        listener.start()
        try:
            await render_events(agent.run(user), console, control, listener)
        finally:
            listener.stop()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 手动验收清单（mock 模式，无需 key）**

Run: `python -m cli.repl`

1. 输入任意文字 → 出现 `🔧 工具调用：run_command` 和红色审批面板
2. 输入 `n` 拒绝 → 工具结果显示红色 PermissionDenied，模型继续输出总结文字
3. `/status` → 显示消息数和 token 记账（mock usage 也会累积）
4. 重新启动后用 `/resume <上一个 session_id>` → 显示恢复的消息条数
5. `/quit` 正常退出

- [ ] **Step 3: 手动验收（真实 Provider，可选，需要 key）**

```powershell
$env:MONO_PROVIDER = "openai"; $env:OPENAI_API_KEY = "<你的key>"; python -m cli.repl
```

1. 提问观察流式逐字输出
2. 让它"执行 dir 命令并总结结果" → 审批 y → 观察工具执行与第二轮推理
3. 长回复中按 Esc → 显示 `⏹ 已请求中断`，本轮以 user_abort 结束
4. 运行中输入一行文字+回车 → 下一轮开始时注入转向消息

- [ ] **Step 4: 全量回归 + Commit**

Run: `python -m pytest`
Expected: 全部通过

```bash
git add cli/repl.py
git commit -m "feat(cli): rich terminal REPL with streaming, approval, abort and steering"
```

---

## 验收总览（对照设计文档）

| 设计章节 | 验收点 | 所在任务 |
|---------|--------|---------|
| §3 消息模型 | 序列化往返无损、块模型、usage 字段 | 1 |
| §4 Provider 抽象 | 双适配器 + Mock、异常归一化、SSE fixture 回放 | 2/3/9/10 |
| §5.1 AgentLoop | 五种终止条件、事件流、StreamAccumulator | 8 |
| §5.2 ToolExecutor | 并发限流、校验、超时重试、错误即观察 | 6 |
| §5.3 上下文与预算 | 真实记账、两级压缩、退避重试 | 7 |
| §6.1 Supervisor | 重复检测、约束、反思（默认关） | 11 |
| §6.2 控制平面 | 中断/转向/审批/暂停，前缀一致性 | 5/12 |
| §6.3 REPL | 流式渲染、Esc、审批框、/resume、/status | 14 |
| §7 错误处理 | 各层兜底矩阵 | 6/7/8/9/10 |
| §8 测试策略 | 单元 + Mock 集成 + live 标记 | 全部 |
