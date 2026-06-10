# Mono Runtime 引擎设计文档

- 日期：2026-06-10
- 状态：已评审通过，待实现
- 参考：《智能体 Harness 工程指南》第 4 章（运行时引擎），`lab/mini_harness` 参考实现

## 1. 背景与目标

Mono 是跟随《智能体 Harness 工程指南》构建的 Agent Harness 项目。第一步已完成 `core/` 脚手架
（Agent 定义、通用事件信封、通用消息信封、Tool 抽象基类）。本设计是第二步：运行时引擎（runtime）。

### 1.1 目标定位

完整对齐指南第 4 章全部七个专题，并在教程参考实现 mini_harness 之上显著增强：

| 维度 | mini_harness | Mono runtime（本设计） |
|------|-------------|----------------------|
| 推理 | 关键词模拟 LLM | 真实双 Provider + 流式 SSE |
| 工具执行 | 串行 | 并发（Semaphore 限流）+ 重试 + 参数校验 |
| 事件 | 8 种粗粒度事件 | 细粒度事件含 text_delta，按 turn/item 分组 |
| 实时控制 | 无 | 中断 / 转向 / 审批 / 暂停恢复 |
| Token 预算 | 写死 4000 且未使用 | 真实 usage 记账 + 自动压缩 |
| 会话恢复 | 无 | JSONL 转录 + 会话恢复 |

### 1.2 已确认的设计约束

1. **架构**：事件驱动分层 runtime（方案 B），借鉴 Codex 的 Turn/Item 概念命名与"审批即双向请求"模式，
   不实现完整 JSON-RPC 协议层。
2. **模型接入**：runtime 内部统一消息模型 + `ModelProvider` 接口；Anthropic 风格 API 与
   OpenAI 兼容端点（DeepSeek/Kimi/智谱等）各一个适配器。
3. **运行形态**：纯 async 库 + rich 终端 REPL。
4. **依赖策略**：核心 runtime 只用标准库 dataclass；外围使用 httpx（异步 HTTP + SSE）与
   rich（终端渲染）。不引入 pydantic / 官方 SDK / langchain。

## 2. 总体架构

### 2.1 目录结构

```
Mono/
├── core/                  # 第一步成果，保留不动
│   ├── agent.py           #   Agent 定义（runtime 复用）
│   ├── event.py           #   通用事件信封（后续编排章节使用）
│   ├── message.py         #   通用消息信封（后续记忆/编排使用）
│   └── tool.py            #   Tool ABC（runtime 复用）
├── runtime/
│   ├── blocks.py          #   统一会话模型：Message + 内容块
│   ├── events.py          #   细粒度运行时事件（含流式 delta）
│   ├── state.py           #   SessionState：append-only 历史 + JSONL 转录
│   ├── engine.py          #   AgentLoop 异步生成器（核心循环）
│   ├── executor.py        #   ToolExecutor：并发 + 重试 + 参数校验
│   ├── context.py         #   ContextAssembler + TokenLedger + 自动压缩 + RetryPolicy
│   ├── supervisor.py      #   漂移检测、约束验证、反思注入
│   └── control.py         #   控制平面：Inbox、控制指令、安全点、审批
├── providers/
│   ├── base.py            #   ModelProvider 接口 + 标准化 StreamEvent + 归一化异常
│   ├── anthropic.py       #   Anthropic Messages API（httpx 手写 SSE 解析）
│   ├── openai_compat.py   #   OpenAI 兼容端点适配器
│   └── mock.py            #   剧本驱动 MockProvider（测试核心）
├── tools/
│   └── builtin.py         #   2-3 个最小演示工具（完整工具层属第 5 章）
├── cli/
│   └── repl.py            #   rich 终端 REPL
├── docs/                  #   设计文档（本文件）
└── tests/
    ├── unit/
    └── integration/
```

**关键决策——core 与 runtime 的消息模型分工**：`core/message.py`（字符串 content）保留为跨子系统
通用信封；runtime 内部使用 `runtime/blocks.py` 的块模型（一条 assistant 消息 = 文本块 + 多个工具
调用块），与真实 LLM API 的形状对齐。两者职责不同，不是重复。

### 2.2 核心数据流

```
用户输入 ──→ REPL ──→ AgentLoop.run() 异步生成器
                          │
        ┌─────────────────┴──────────────────┐
        │  每轮 Turn：                        │
        │  ① 安全点：消费 Inbox 控制指令       │ ←── 中断/转向/审批 由 REPL 注入
        │  ② ContextAssembler 组装 + 预算检查  │
        │  ③ provider.stream() 流式推理       │ ──→ yield TextDelta（实时渲染）
        │  ④ StreamAccumulator 累积完整消息    │
        │  ⑤ 工具调用 → 审批门 → 并发执行       │ ──→ yield ApprovalRequested / ToolResult
        │  ⑥ Supervisor 体检（漂移/约束）      │
        │  ⑦ 终止条件检查 → 继续 or 结束        │
        └─────────────────┬──────────────────┘
                          ▼
              事件流回 REPL 实时渲染
              消息 append 到 sessions/<id>.jsonl（可恢复）
```

## 3. 统一消息模型（`runtime/blocks.py`）

runtime 内部唯一的会话语言，所有 Provider 差异挡在适配器层之外。

```python
@dataclass
class TextBlock:        # 文本
    text: str

@dataclass
class ThinkingBlock:    # 推理过程（extended thinking / reasoning 模型）
    thinking: str

@dataclass
class ToolUseBlock:     # 模型发起的工具调用
    id: str             # 与结果配对
    name: str
    input: dict

@dataclass
class ToolResultBlock:  # 工具执行结果（按 API 约定属于 user 角色消息）
    tool_use_id: str
    content: str
    is_error: bool = False
    error_type: str | None = None

@dataclass
class Usage:
    input_tokens: int
    output_tokens: int

@dataclass
class Message:
    role: str                        # "user" | "assistant"
    content: list                    # 混合块列表
    message_id: str
    timestamp: datetime
    usage: Usage | None = None       # assistant 消息携带真实 token 消耗
```

- 工厂方法：`Message.user(text)` / `Message.assistant(blocks)` / `Message.tool_results(results)`
- 工具方法：`get_text()` / `get_tool_calls()` / `has_tool_calls()` / `to_dict()` / `from_dict()`
- `to_dict/from_dict` 必须序列化往返无损（JSONL 转录与会话恢复依赖此性质）
- 相比指南 4.2 的版本，新增 `usage` 字段，是 Token 预算"真记账"的基础

## 4. Provider 抽象层（`providers/`）

### 4.1 接口与标准化流事件（`base.py`）

```python
@dataclass
class ModelRequest:
    system: str
    messages: list[Message]          # 统一块模型
    tools: list[dict]                # JSON Schema 工具定义
    max_tokens: int
    model: str

# 标准化流事件（Provider 无关）：
#   MessageStart(model)
#   TextDelta(text)                  ← 立即转发 UI
#   ThinkingDelta(text)
#   ToolUseStart(id, name)
#   ToolInputDelta(partial_json)     ← 只累积，不执行
#   ToolUseEnd(id)                   ← 参数完整，可调度
#   MessageEnd(stop_reason, usage)   ← 携带真实 token 用量
#   StreamError(error)

class ModelProvider(ABC):
    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]: ...
    def count_tokens_estimate(self, messages: list[Message]) -> int: ...
```

引擎只消费 `StreamEvent`，对 Provider 协议细节零感知。

### 4.2 三个适配器

| 适配器 | 翻译职责 |
|--------|---------|
| `AnthropicProvider` | httpx 手写 SSE：`content_block_start/delta/stop` → StreamEvent；tool_use 块天然对齐 |
| `OpenAICompatProvider` | `delta.tool_calls[].function.arguments` 增量 → ToolInputDelta；`finish_reason` → stop_reason；块模型 ↔ `tool_calls`/`tool` 角色消息双向转换 |
| `MockProvider` | 接受剧本 `list[list[StreamEvent]]` 按轮回放，可注入延迟与错误；集成测试核心道具 |

### 4.3 异常归一化

适配器内将各家错误统一映射为：

- `RateLimitError(retry_after)` —— 429
- `ProviderTimeoutError` —— 超时
- `ProviderServerError` —— 5xx
- `ProviderAuthError` / `ProviderBadRequestError` —— 401/400（不可重试）

重试策略只认识归一化异常，不关心来源。

## 5. 执行层

### 5.1 AgentLoop（`runtime/engine.py`）

异步生成器，每轮 Turn 七步（见 2.2 数据流）。要点：

- **StreamAccumulator** 独立小类：吃 `StreamEvent` 序列，吐完整 `Message`（含解析好的 tool_use
  块与 usage）。独立成类保证可单测。
- **终止条件**（对齐 4.1.3）：无工具调用（completed）/ max_turns / Token 预算耗尽 / 用户 abort /
  Supervisor 判死。`AgentEnded` 事件必须携带 `termination_reason`。
- 推理调用由 `asyncio.Task` 包裹，支持取消（Abort）。
- state 的每次 append 自动落盘 JSONL。

### 5.2 ToolExecutor（`runtime/executor.py`）

执行管道四步（对齐 4.3 + 4.4）：

1. **查找**：registry 无此工具 → 错误结果（不抛异常）
2. **参数校验**：按 `ToolDefinition.input_schema` 校验必填/类型，不合法直接返回错误结果
3. **并发执行**：`asyncio.gather` + `Semaphore(max_concurrent=5)` + 每工具
   `asyncio.timeout(tool.timeout_seconds)`（复用 `core/tool.py` 已有字段）
4. **错误即观察**：任何异常 → `ToolResultBlock(is_error=True, error_type=...)`，永不打断循环

可重试的工具错误（超时类）内部指数退避重试 ≤2 次；权限/参数错误不重试。

### 5.3 上下文与预算（`runtime/context.py`）

- **TokenLedger**：累积 assistant 消息真实 `usage` 记账；未发送内容用 `len(text)//4` 启发式预估，
  发送后用真实值校准。任务级预算（max_api_calls / max_total_tokens）在此检查——实现三级预算体系
  （4.6.5）的 Per-Request 与 Per-Task 两层。
- **ContextAssembler**：组装 `system + 历史窗口 + 工具 schema`；预估超 80% 阈值触发自动压缩：
  - 策略 1（优先）：旧的大块工具结果截断为 `[结果已截断，原长 N 字符]`
  - 策略 2：保留首条用户消息 + 最近 N 条，中间折叠为规则拼接的摘要占位
    （LLM 摘要升级留给第 6 章记忆系统）
  - 压缩发生时 yield `ContextCompacted` 事件
- **RetryPolicy**（providers 共用）：指数退避 + `Retry-After` 感知；429/5xx/超时重试 ≤3 次，
  401/400 立即失败；时钟可注入以便测试。

## 6. 治理与控制

### 6.1 Supervisor（`runtime/supervisor.py`，对齐 4.5）

每轮 turn 末尾体检，返回裁决：`CONTINUE | INJECT(message) | TERMINATE(reason)`。
检查器为可插拔列表，按序执行，第一个非 CONTINUE 的生效：

- **RepetitionDetector**：最近 5 轮内相同 `(tool_name, input_hash)` 出现 ≥3 次 → INJECT 纠正消息
- **ConstraintValidator**：工具调用总数、墙钟时间硬上限 → 超限 TERMINATE
- **ReflectionStep**（默认关闭）：每 N 轮 INJECT 反思提示

明确不做：基于 embedding 的语义漂移检测（额外依赖，启发式已覆盖主要场景，YAGNI）。

### 6.2 控制平面（`runtime/control.py`，对齐 4.8）

```python
# 控制指令：Abort | Steer(text) | Approve(ids) | Deny(ids) | Pause | Resume

class ControlPlane:
    inbox: asyncio.Queue            # REPL → 引擎
    def submit(cmd): ...            # 任意任务可调用
    async def drain(safe_point): ...# 引擎在安全点调用
```

| 能力 | 机制 |
|------|------|
| 中断 Abort | 取消推理 Task；半成品 assistant 消息丢弃；`AgentEnded(reason="user_abort")`；工具执行中则取消 gather |
| 转向 Steer | 文本在下一安全点作为 user 消息**追加**（只追加不改历史，保持前缀一致性，缓存友好） |
| 审批 | 工具带 `permissions_required` 或在危险清单 → yield `ApprovalRequested` 后 await Approve/Deny（双向模式）；Deny 返回 `is_error=True` 结果让模型知晓 |
| 暂停/恢复 | Pause 后引擎在安全点阻塞等 Resume；进程级恢复 = 读 JSONL 转录重建 state（检查点即转录，append-only 事件溯源） |

### 6.3 REPL（`cli/repl.py`）

- 主循环：读输入 → 创建任务消费 `loop.run()` 事件流并渲染
- `TextDelta` 实时打印；工具调用渲染为面板（名称+参数+耗时+结果摘要）；压缩/预算警告显示状态条
- 运行中按 Esc → Abort；运行中输入文字回车 → Steer；`ApprovalRequested` → rich 确认框 y/n
- 命令：`/resume <session>`（JSONL 恢复会话）、`/status`（token 用量）、`/quit`

## 7. 错误处理汇总（对齐 4.4）

| 层 | 错误 | 策略 |
|----|------|------|
| Provider | 429/5xx/超时 | RetryPolicy 指数退避 ≤3 次；仍失败 → `AgentEnded(reason="provider_error")` |
| Provider | 流中途断开 | 丢弃半成品，整轮重试一次 |
| Executor | 工具异常/超时/参数非法 | 错误即观察，`is_error=True` 反馈给模型 |
| 引擎 | 未捕获异常 | 兜底 yield `ErrorEvent` + `AgentEnded(reason="fatal")`；state 已落盘可恢复 |

## 8. 测试策略

- **单元测试**（不碰网络）：blocks 序列化往返、StreamAccumulator、终止检查器、
  RetryPolicy（注入假时钟）、executor 并发/超时/校验、压缩器、supervisor、
  两个 Provider 适配器用录制的 SSE 文本 fixture 回放解析
- **集成测试**：MockProvider 剧本驱动端到端——多轮工具循环、并发工具、审批批/拒、
  abort、steer、压缩触发、JSONL 恢复；全部可在 CI 运行
- **真实 API 冒烟**：`@pytest.mark.live` 标记，本地有 key 才运行

## 9. 实现顺序（依赖关系决定）

1. `runtime/blocks.py` —— 一切的地基
2. `providers/base.py` + `providers/mock.py` —— 接口与测试道具
3. `runtime/state.py` —— SessionState + JSONL 转录
4. `runtime/executor.py` —— 工具执行管道
5. `runtime/context.py` —— 组装、预算、压缩、重试策略
6. `runtime/engine.py` —— AgentLoop 核心循环（此时端到端可用 Mock 跑通）
7. `providers/anthropic.py` + `providers/openai_compat.py` —— 真模型接入
8. `runtime/supervisor.py` —— 漂移与约束
9. `runtime/control.py` —— 控制平面
10. `cli/repl.py` —— 终端体验

每步配套对应测试，前一步通过再进下一步。
