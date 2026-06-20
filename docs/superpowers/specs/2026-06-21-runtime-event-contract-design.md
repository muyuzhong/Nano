# Runtime 事件契约稳定化（项目① · spec 1）设计

- **日期**：2026-06-21
- **状态**：设计自批准（用户在 `/loop` 中授权「所有决策由我选更推荐的一种，无需询问」）。
- **参考架构**：`D:\Pi\pi\packages\agent\src`（`agent-loop.ts`、`types.ts`）。
- **本 spec 范围**：runtime **事件流契约** + **事件可推导状态** 的稳定化与文档化。**不**改工具执行机制、不加续跑队列、不动 provider。
- **roadmap 定位**：这是 runtime 巩固四步（②事件契约 → ③工具生命周期 → ④续跑/队列 → ⑤provider 成熟度）的第一步。P1 abort re-check 已在 `37aea1e` 落地。

---

## 1. 目标与非目标

**目标**
- 把 `agent_loop` / `Agent` 发出的事件**顺序**定为**书面契约**，UI/harness 可依赖而不被偶发实现行为绑架。
- 让运行状态**可完全由事件推导**：补齐 `streaming_message` 与 `error_message` 的可见性缺口。
- 用回归测试**锁定**契约不变量，且这些不变量在 spec ③（并行工具、完成序 end、partial update）落地后**仍成立**（即「契约定义最终顺序」）。

**非目标（YAGNI）**
- 不实现 `after_tool_call`、`tool_execution_update` 的产出、`terminate`、并行/顺序执行重排 → spec ③。
- 不实现 follow-up/continuation/queue → spec ④。
- 不动 provider → spec ⑤。
- 不把工具执行从「批量」改成「逐工具相邻」机制——本 spec 只**书面**规定最终顺序，机制保持现状。

---

## 2. 背景：现状 vs Pi 的差距（已核实）

| 维度 | Pi（参考） | NanoAgent 现状 | 本 spec 处理 |
|---|---|---|---|
| 顺序保证文档 | 写在类型注释上（`types.ts:36-44,246-254,403-418`） | 无书面契约 | **补**：`events.py` 模块级契约文档 |
| `message_update` 范围 | 仅 assistant 流式 | 同（`loop.py`） | 文档化为 G3 |
| 工具事件顺序 | 逐工具相邻(顺序)/完成序 end(并行)，结果消息按源序（`agent-loop.ts:434-510`） | 批量：全 start→全 end→全结果消息 | 文档化最终顺序；机制保持批量（spec ③ 补齐） |
| `streaming_message` | 流式期间可见（`types.ts:337`） | 仅 `message_update` 时置位，`message_start` 时为 None（`agent.py:96`） | **改**：assistant `message_start` 即置位 |
| `error_message` | `AgentState` 暴露（`types.ts:341`） | **缺字段** | **加**：`AgentState.error_message` |
| `pending_tool_calls` | `ReadonlySet<id>`（`types.ts:339`） | `dict[id,PendingToolCall]`（更丰富，OK） | 文档化为 G5 状态窗口 |

---

## 3. 事件契约（规范顺序）

一次 **run** = `agent_loop(...)` 单次调用发出的事件序列。开头恰好一个 `agent_start`，结尾恰好一个 `agent_end`。

```
agent_start
  # 每个 prompt 消息 m：
  message_start(m) ; message_end(m)
  # 每个 turn 重复：
  turn_start
    # 每个注入消息 s（steering；将来含 follow-up）：
    message_start(s) ; message_end(s)
    # assistant 流式：
    message_start(assistant)
    message_update(assistant, ev)*        # 仅 assistant
    message_end(assistant)
    # 若终止（abort/error/complete/max_turns）：turn_end(assistant, []) ; agent_end ; 结束
    # 否则若有 tool calls：进入「工具执行段」
    turn_end(assistant, tool_results)     # tool_results 按源序
agent_end(messages, result)               # 恰好一次，终止
```

### 3.1 工具执行段顺序（契约）
对 assistant 内的工具调用集合，按**源序** s1..sn：
- 每个调用发 `tool_execution_start`，**start 按源序**出现。
- 执行期间该调用**可**发 `tool_execution_update`*（spec ③ 才真正产出）；某调用的 update 落在它的 start 之后、end 之前，靠 `tool_call_id` 关联。
- 每个调用发 `tool_execution_end`（带 `result`+`is_error`）：
  - 顺序执行：end 按源序。
  - 并行执行（spec ③）：end 按**完成序**。**消费者必须靠 `tool_call_id` 关联，不得靠位置。**
- 全部 finalize 后，工具结果消息以 `message_start`/`message_end` 成对发出，**按源序**（保证 transcript 顺序与完成序无关）。

### 3.2 保证的不变量（稳定；测试锁定）
- **G1**：恰好一个 `agent_start`（首），恰好一个 `agent_end`（尾）。
- **G2**：每个 `message_start(m)` 最终对应恰好一个 `message_end(m)`；成对平衡。
- **G3**：`message_update` 只为当前 turn 的 assistant 消息发出，严格落在其 `message_start` 与 `message_end` 之间。
- **G4**：每 turn `turn_start` 先于 `turn_end`；turn 不嵌套。
- **G5**：对每个 `tool_call_id`，`start` 先于任意 `update` 先于 `end`；被执行的调用恰好一个 start、一个 end（被拒/未知工具也照发 start+end）。
- **G6**：工具结果 transcript 消息（ToolResultMessage 的 `message_start/end`）按源序，且在本批所有 `tool_execution_end` 之后。
- **G7**：`agent_end.result.final_message_id` 指向最后一条 produced 消息 id（或 None）。

> 注：G5 只约束「每 id 内 start<update<end」，**不**约束 end 的全局顺序——故 spec ③ 把 end 改成完成序时契约不破。测试**不得**断言 end 的全局源序。

---

## 4. 状态可见性契约（事件可推导）

在每个事件的订阅者运行时（`Agent` 先 reduce 再 emit），状态满足：
- `message_start(assistant)` 起，经 `message_update`*：`streaming_message` 为进行中的 assistant 消息；`is_streaming` 为 True。
- `message_end(m)` 后：`m` 已入 `state.messages`；`streaming_message` 为 None。
- `tool_execution_start(id)` 与 `tool_execution_end(id)` 之间：`id` 在 `state.pending_tool_calls`。
- run 起始 `error_message` 置 None；`agent_end` 后 `error_message` = `result.error`（ERROR run 为错误文本；其它为 None）。
- `is_streaming`：`prompt()` 开始到 `agent_end` 监听器结算之间为 True（由 `prompt()` 管理，非事件推导）。

---

## 5. 改动点（最小）

1. **`src/nanoagent/agent/events.py`**：加模块级 docstring，写入 §3 规范顺序 + G1–G7 + §4 状态窗口。仅文档，无类型改动（`ToolExecutionUpdate` 已存在）。
2. **`src/nanoagent/agent/agent.py`**：
   - `AgentState` 加 `error_message: str | None = None`。
   - `_reduce`：`MessageStart` 且 `role=="assistant"` → 置 `streaming_message`；新增 `AgentEnd` 分支 → `error_message = event.result.error`。（`MessageEnd`/`MessageUpdate`/`ToolExecution*` 不变。）
   - `prompt()` 起始：`self.state.error_message = None`。
3. **测试**：
   - `tests/agent/test_event_contract.py`：多工具 run 断言 G1–G7（按 id 关联，不断言 end 全局序）。
   - `tests/agent/test_agent.py` 增：assistant `message_start` 时 `streaming_message` 已置位；ERROR run 后 `error_message` 有值且下一次成功 run 前被重置。

---

## 6. 测试与验收

- `pytest -q` 全绿；`pytest tests/test_import_contract.py -q` 绿；`lint-imports` KEPT。
- TDD：先写失败测试（streaming_message@start、error_message），再改 `_reduce`。契约测试在现状（批量）即应通过。

---

## 7. 自主决策记录（用户已授权）

- **D1 工具执行机制保持批量**：本 spec 不改机制，只文档化最终顺序。理由：机制重排与 spec ③ 的并行/完成序强耦合，提前改会做两遍。
- **D2 `error_message` 取自 `AgentEnd.result.error`**（而非逐 turn）：NanoAgent run 在首个 error 即终止，run 级与 turn 级等价；更少代码。abort 无错误文本时保持 None。
- **D3 `streaming_message` 在 assistant `message_start` 即置位**：靠 `role=="assistant"` 区分（prompt/tool-result/steering 的 start 不置位）。
- **D4 契约测试只断言「每 id start<end」**：不锁 end 全局序，保证 spec ③ 改完成序时不破契约。

---

## 8. 与后续 spec 的衔接

- **spec ③（工具生命周期）**：在 §3.1 已预留的 update/end 槽位上填机制——产出 `tool_execution_update`、加 `after_tool_call`、`terminate`、并行(完成序 end)。契约顺序不变，只是「填空」。
- **spec ④（续跑/队列）**：在 §3 的「注入消息」点接入 follow-up/continuation/queue drain。
- **spec ⑤（provider）**：不影响事件契约。
