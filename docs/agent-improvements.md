# Agent 改进记录

本文件记录每次小而安全的改动，便于后续会话继续按模块推进。

## 2026-06-23 - agent 运行期事件闭合

- 模块：`nanoagent.agent`
- 改动：异常发生在消息、回合或工具执行中途时，`agent_loop` 会先补齐对应的 `message_end`、`turn_end` 和 `tool_execution_end`，再产出唯一的 `agent_end`。
- 改动：`MessageStart` 增加 `generated` 标记，用来区分本轮模型生成输出和注入的 assistant 历史消息，避免状态层把历史消息误认为正在流式输出。
- 测试：新增覆盖 hook/stream 异常后的事件平衡，以及 Agent 状态中 `streaming_message`、`pending_tool_calls` 的清理。
- 验证：`pytest tests/agent/test_agent.py tests/agent/test_loop_hooks.py -q`，18 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，58 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - ai 流式文本增量可见

- 模块：`nanoagent.ai`
- 改动：`StreamAccumulator` 现在会在收到 `TextDelta` 时追加到当前 `TextContent`，让中间累计消息在 `TextEnd` 前也能反映已到达文本。
- 约束：`TextEnd` 仍保留最终文本覆盖逻辑，避免改变 provider 最终消息的权威性。
- 测试：先新增 `test_stream_accumulator_exposes_text_delta_before_text_end` 并确认失败，再做最小实现。
- 验证：`pytest tests/ai/test_accumulator.py::test_stream_accumulator_exposes_text_delta_before_text_end -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/ai/test_accumulator.py -q`，3 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，59 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - ai provider 注册表只读查询

- 模块：`nanoagent.ai`
- 改动：新增 `registered_provider_apis()`，返回当前已注册 provider API 名称的稳定 tuple 快照，供 harness 或测试做机制层 introspection。
- 约束：该函数只暴露注册键，不选择默认 provider，不处理 API key，也不引入产品策略。
- 测试：先新增 `test_registered_provider_apis_returns_sorted_snapshot` 并确认因缺少导出失败，再做最小实现。
- 验证：`pytest tests/ai/test_provider.py::test_registered_provider_apis_returns_sorted_snapshot -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/ai/test_provider.py -q`，3 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，60 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - utils 根 logger 名称规范化

- 模块：`nanoagent.utils`
- 改动：`get_logger("")` 现在返回 `nanoagent` 根 logger，避免生成 `nanoagent.` 这种带尾点的名称。
- 约束：普通子 logger 仍按 `nanoagent.<name>` 命名，不添加 handler，不引入 harness 日志策略。
- 测试：先新增 `test_get_logger_returns_package_root_for_empty_name` 并确认失败，再做一行最小实现。
- 验证：`pytest tests/utils/test_logging.py -q`，2 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/utils -q`，3 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，62 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - agent context hook 输入隔离

- 模块：`nanoagent.agent`
- 改动：`assemble_context` 传给 `transform_context` 的消息列表现在是浅拷贝，hook 对列表结构的改动不会回写到调用方持有的历史列表。
- 约束：只隔离列表结构，不深拷贝消息对象；上下文压缩、裁剪等具体策略仍由 harness 或注入 hook 决定。
- 测试：先新增 `test_transform_context_receives_message_list_snapshot` 并确认原列表被清空导致失败，再做最小实现。
- 验证：`pytest tests/agent/test_context.py::test_transform_context_receives_message_list_snapshot -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/agent/test_context.py -q`，3 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，63 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - agent tool wire schema 缓存防污染

- 模块：`nanoagent.agent`
- 改动：`AgentTool.to_wire()` 仍缓存内部 wire 模板，但每次对外返回带深拷贝参数 schema 的 `Tool`，避免 provider adapter 或调用方修改返回值后污染后续上下文。
- 约束：不改变工具执行、参数校验、并发策略或 approval/hook 行为。
- 测试：先新增 `test_to_wire_schema_cache_is_not_externally_mutable` 并确认第二次读取 schema 缺失导致失败，再做最小实现。
- 验证：`pytest tests/agent/test_tools.py::test_to_wire_schema_cache_is_not_externally_mutable -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/agent/test_tools.py -q`，5 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，64 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - ai 流式 thinking 增量可见

- 模块：`nanoagent.ai`
- 改动：`StreamAccumulator` 现在会在收到 `ThinkingDelta` 时追加到当前 `ThinkingContent`，让中间累计消息在 `ThinkingEnd` 前也能反映已到达 thinking 内容。
- 约束：`ThinkingEnd` 仍保留最终内容覆盖逻辑；不改 provider 输出格式，也不处理 tool-call 增量。
- 测试：先新增 `test_stream_accumulator_exposes_thinking_delta_before_thinking_end` 并确认失败，再做最小实现。
- 验证：`pytest tests/ai/test_accumulator.py::test_stream_accumulator_exposes_thinking_delta_before_thinking_end -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/ai/test_accumulator.py -q`，4 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，65 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - agent 默认消息转换按类型过滤

- 模块：`nanoagent.agent`
- 改动：`default_convert_to_llm` 现在按 `UserMessage`、`AssistantMessage`、`ToolResultMessage` 实际类型保留 wire 消息，而不是只看 `role` 字符串。
- 约束：自定义消息仍默认丢弃；具体降级或映射策略仍由 harness 通过 `convert_to_llm` 注入。
- 测试：先新增 `test_default_convert_filters_custom_even_with_wire_role_string` 并确认误标 `role="user"` 的自定义消息被透传导致失败，再做最小实现。
- 验证：`pytest tests/agent/test_messages.py::test_default_convert_filters_custom_even_with_wire_role_string -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/agent/test_messages.py -q`，2 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，66 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - openai 工具调用参数增量事件

- 模块：`nanoagent.ai.providers.openai`
- 改动：OpenAI SSE 解析工具调用参数分片时，现在会在 `ToolCallStart` 和 `ToolCallEnd` 之间发出 `ToolCallDelta`，让调用方能观察参数流式累积过程。
- 约束：不改变请求编码、默认 provider、API key、approval 或 harness 策略；最终 `ToolCallEnd` 仍携带解析后的完整参数。
- 测试：先新增 `test_stream_emits_tool_call_argument_deltas` 并确认缺少 delta 导致失败，再做最小实现。
- 验证：`pytest tests/ai/test_openai.py::test_stream_emits_tool_call_argument_deltas -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/ai/test_openai.py -q`，5 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，67 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - mock provider usage 注入

- 模块：`nanoagent.ai.providers.mock`
- 改动：脚本化 mock response 现在可通过 `usage` 字段设置最终 `AssistantMessage.usage`，便于框架测试覆盖 usage 传播。
- 约束：只增强 mock provider；不改真实 provider、token 预算策略、默认 provider 或 API key 行为。
- 测试：先新增 `test_mock_response_can_set_usage` 并确认 usage 仍为默认 0 导致失败，再做最小实现。
- 验证：`pytest tests/ai/test_mock.py::test_mock_response_can_set_usage -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/ai/test_mock.py -q`，3 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，68 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - utils ID 前缀类型边界

- 模块：`nanoagent.utils`
- 改动：`new_id` 现在会拒绝非字符串 `prefix`，避免调用方意外把数字等对象格式化进 ID 前缀。
- 约束：不改变空前缀和普通字符串前缀的 ID 格式；不引入 harness 策略或全局配置。
- 测试：先新增 `test_new_id_rejects_non_string_prefix` 并确认当前实现没有抛错导致失败，再做最小实现。
- 验证：`pytest tests/utils/test_ids.py::test_new_id_rejects_non_string_prefix -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/utils/test_ids.py -q`，2 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，69 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - agent abort 原因稳定化

- 模块：`nanoagent.agent`
- 改动：`AbortSignal.abort()` 在信号已经触发后保持第一次取消原因，后续重复调用不会覆盖根因。
- 约束：不改变 `.aborted`、`.wait()` 或 provider-facing signal duck type；不引入取消策略或 harness 行为。
- 测试：先新增 `test_abort_signal_keeps_first_reason` 并确认第二次 abort 覆盖 reason 导致失败，再做最小实现。
- 验证：`pytest tests/agent/test_control.py::test_abort_signal_keeps_first_reason -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/agent/test_control.py -q`，3 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/agent -q`，41 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，70 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - ai Context 输入列表隔离

- 模块：`nanoagent.ai`
- 改动：`Context` 构造时会浅拷贝 `system_prompt`、`messages` 和 `tools` 列表，避免调用方后续修改原列表污染已构造上下文。
- 约束：只隔离列表结构，不深拷贝消息或工具对象；不改变 provider 选择、API key 或上下文裁剪策略。
- 测试：先新增 `test_context_copies_input_lists` 并确认外部列表追加/清空会影响 `Context` 导致失败，再做最小实现。
- 验证：`pytest tests/ai/test_messages.py::test_context_copies_input_lists -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/ai/test_messages.py -q`，5 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/ai -q`，25 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，71 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - agent 工具开始事件参数快照

- 模块：`nanoagent.agent`
- 改动：`ToolExecutionStart.args` 初始化时会深拷贝工具参数，避免事件消费者修改 start 事件后污染后续工具执行。
- 约束：不改变工具审批、执行顺序、参数校验或并发策略；只隔离事件负载和内部 tool call 参数。
- 测试：先新增 `test_tool_execution_start_args_are_event_snapshot` 并确认修改 `event.args` 会让工具收到变更后的参数导致失败，再做最小实现。
- 验证：`pytest tests/agent/test_loop_tools.py::test_tool_execution_start_args_are_event_snapshot -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/agent/test_loop_tools.py -q`，2 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/agent -q`，42 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，72 passed，1 个 pytest cache 写入警告。

## 2026-06-23 - ai Tool schema 输入隔离

- 模块：`nanoagent.ai`
- 改动：`Tool` 构造时会深拷贝 `parameters` JSON Schema，避免调用方后续修改原 schema 污染 provider 可见工具定义。
- 约束：不改变 schema 内容、工具选择、provider 编码或执行策略；只隔离 wire tool 的输入字典。
- 测试：先新增 `test_tool_copies_json_schema_on_init` 并确认外部嵌套 schema 修改会影响 `Tool.parameters` 导致失败，再做最小实现。
- 验证：`pytest tests/ai/test_model_tool.py::test_tool_copies_json_schema_on_init -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/ai/test_model_tool.py -q`，5 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/ai -q`，26 passed，1 个 pytest cache 写入警告。
- 验证：`pytest tests/test_import_contract.py -q`，1 passed，1 个 pytest cache 写入警告。
- 验证：`pytest -q`，73 passed，1 个 pytest cache 写入警告。
