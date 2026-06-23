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
