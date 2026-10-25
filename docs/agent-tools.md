# Agent Tools / MCP 验收设计

## 当前状态

当前仓库已经有可复用的 Agent 基础，但还不是 MCP 风格的工具运行时：

- `app.agents.Agent` 通过 `execute(capability, payload)` 执行能力。
- `app.agents.AgentRegistry` 负责注册、查找和描述 Agent。
- `app.policy.PolicyEngine` 负责权限、确认和自动化等级边界。
- `app.orchestrator.TaskExecutor` 负责任务执行与开始/完成审计。
- `app.events` 负责事件归一化、短窗口去重和忽略规则。
- `JarvisService.audit()` 与 `JarvisOrchestrator.audit()` 负责写入 `audit_log`。

本轮只增加测试和本文档，没有修改 `app` 源码。

## 已覆盖的验收项

运行：

```powershell
python -m pytest -q tests/test_tool_architecture.py
```

测试不调用真实模型、ASR、设备或公网接口，使用临时 SQLite 和测试 Agent。

当前测试覆盖：

1. Agent 注册、能力查找和能力元数据。
2. 未知 Agent 能力返回 `ask_user/capability_unavailable`。
3. 缺少权限时，执行决策被转成 `confirm`。
4. `OBSERVE` 能力可自动执行，`PROPOSE` 能力需要确认。
5. TaskExecutor 在执行前检查权限。
6. TaskExecutor 成功执行时写入 `task_started` 和 `task_finished` 审计。
7. 事件文本归一化、UTC 时间和 `content_hash`。
8. 用户对话不被事件去重吞掉，外部事件在 45 秒窗口内去重。
9. 忽略规则命中和 `hit_count` 增长。
10. 设备命令回执和 `device_command_ack` 审计。
11. DeviceAgent 缺少 `device_id` 时不发送命令。

## 目标 MCP 接口契约

当前工作树已经包含初版 `app.tools` 和 `app.tool_loop`，测试会直接验收它们。它们还没有接入 `JarvisOrchestrator` 的主消息链路，因此“模块级验收通过”不等于手表消息已经自动走 MCP Tool Loop。

当前模块：

- `app.tools.definitions.ToolDefinition` / `ToolResult` / `ToolRisk`
- `app.tools.registry.ToolRegistry`
- `app.tools.runtime.ToolRuntime`
- `app.tools.mcp.MCPToolSource`
- `app.tool_loop.AgentLoop`

建议的工具描述最少包含：

```json
{
  "name": "device.get_status",
  "description": "查询设备在线、电量、网络和存储状态",
  "inputSchema": {
    "type": "object",
    "properties": {
      "device_id": {"type": "string"}
    },
    "required": ["device_id"],
    "additionalProperties": false
  },
  "permissions": [],
  "risk": "observe",
  "can_interrupt": false
}
```

建议的 `ToolRegistry`：

```python
class ToolRegistry:
    def register(self, tool: ToolSpec) -> None: ...
    def list_schemas(self) -> list[dict]: ...
    def get(self, name: str) -> ToolSpec | None: ...
```

建议的 `ToolRuntime.execute()` 必须按以下顺序处理：

```text
解析工具名
→ 查找工具
→ 校验 JSON Schema 参数
→ 校验用户权限
→ 校验风险/确认状态
→ 写入 tool_call 审计
→ 执行工具并应用超时
→ 写入 tool_result 审计
→ 返回结构化结果或结构化错误
```

建议的 `AgentLoop.run()`：

```text
messages = 当前会话上下文
最多 max_rounds 轮：
  调用模型
  没有 tool_calls → 返回最终文本
  有 tool_calls：
    按顺序执行工具
    把 assistant tool_calls 和 tool_result 追加到同一 messages
超过轮数 → 返回可理解错误并写入 loop_limit 审计
```

工具结果必须回到同一个上下文，不能只返回给 HTTP 层后丢失。每次调用至少应能关联：

- `conversation_id`
- `event_id`
- `tool_call_id`
- `tool_name`
- `arguments`（敏感值应脱敏）
- `result` 或 `error`
- `round`
- `created_at`

## 兼容性风险

### 1. 新 Tool Runtime 尚未接入现有 Orchestrator

`app.tool_loop.AgentLoop` 和 `app.tools.ToolRuntime` 可以独立运行，但 `JarvisOrchestrator.handle()` 仍然使用 `AIIntentClassifier` 的一次性 JSON 决策。当前手表消息不会自动经历多轮 `assistant → tool → assistant`。

建议先在一个 feature flag 下接入，并保留旧路径作为回退：

```text
message
→ AgentLoop
→ ToolRuntime
→ Policy/confirmation
→ final response
```

此外，当前工作树中 `JarvisService.__init__()` 已经构造 `ToolRegistry` 并把
`tools=self.tools` 传给 `JarvisOrchestrator`，但 `JarvisOrchestrator.__init__()`
尚未接收该参数。直接构造 `JarvisService` 会抛出 `TypeError`，这是当前最直接
的启动阻塞项；测试会以 `xfail` 标记它，而不会修改生产代码。

### 2. 现有 `AgentRegistry.describe()` 不是完整 MCP Schema

当前描述包含名称、描述、权限和自动化等级，但没有暴露 `input_schema`、`output_schema`、`can_interrupt`。直接把它喂给支持工具调用的模型会丢失参数约束和行为边界。

建议新增 MCP 适配层，不要直接改变现有 Agent API：

```text
AgentCapability → ToolSpec
agent.name + capability.name → tool.name
input_schema → JSON Schema
required_permissions → permissions
autonomy_level → risk/confirmation policy
```

### 3. 现有 capability schema 只是字符串映射

例如 `{"device_id": "string"}` 还不能约束缺失字段、额外字段、数字范围或 URL 格式。工具运行时必须使用真正的 JSON Schema 或 Pydantic 模型校验。

### 4. 权限拒绝目前没有审计记录

`TaskExecutor` 在发现缺少权限时直接返回错误，不会写 `task_failed` 或独立的 `permission_denied` 审计。生产环境需要记录拒绝原因、工具名、权限名和会话关联信息，但不能记录 Token 等敏感数据。

### 5. 现有 PolicyEngine 以 Agent/能力寻址

未来 MCP 工具使用 `device.get_status` 这类扁平名称时，需要保持旧的 `target_agent + capability` 兼容，或增加明确的名称解析层。不要让模型自由拼接 Python 方法名。

### 6. 当前编排器每个事件只做一次模型决策

`JarvisOrchestrator.handle()` 只产生一个 `PolicyDecision`，不存在把 `tool_result` 放回同一上下文并继续推理的循环。接入 Agent Loop 时必须避免重复创建用户事件、重复去重和重复写入对话。

### 7. 确认流程需要绑定 tool call

当前 `pending_actions` 主要绑定 `event_id`、Agent 和 capability。多轮工具调用后，确认记录还应绑定 `conversation_id`、`tool_call_id` 和参数摘要，防止用户确认的不是当前展示的动作。

### 8. ToolRisk 与 PolicyEngine 的风险语义尚未统一

`ToolRisk` 已提供 `observe`、`propose`、`execute` 三个值，但 `ToolRuntime` 当前只校验
`permissions`，不会根据 risk 自动要求确认；旧 `PolicyEngine` 也只会对
`AutonomyLevel` 大于 `LOW_RISK_AUTO` 的 Agent 能力自动升级确认。当前测试因此只验收
风险元数据能被暴露，并明确记录该边界，不能把 `propose` 误认为已经具备确认保护。

个人实例可以设置 `PERSONAL_MODE=1` 和 `JARVIS_TRUST_MODE=personal`。
此时 Jarvis 会授予内置任务、设备、日历和位置能力所需权限，并自动执行低风险操作；
取消任务、打开外部链接和未来标记为高风险的动作仍需要确认。确认可以通过自然语言
发送“确认/允许/执行”，拒绝可以发送“取消/不要”，不要求调用单独的确认接口。

### 9. 工具执行需要超时、取消和调用上限

现有 Agent 执行接口没有统一超时或取消协议。Agent Loop 必须设置最大轮数、单工具超时、总耗时和错误格式，避免远程 MCP Server 或设备连接导致请求永久挂起。

### 9. 并行工具调用的顺序语义尚未定义

模型可能一次返回多个工具调用。需要规定哪些工具可并行，哪些工具必须串行，例如先读取位置再搜索附近地点。所有结果都必须带 `tool_call_id`，不能只依赖返回顺序。

### 10. 审计详情需要脱敏

当前审计使用 JSON 序列化保存 detail。接入 MCP 后，URL 参数、通知正文、图片地址、认证头和第三方 API 响应可能包含敏感信息，必须在写审计前做字段级脱敏和大小限制。

### 11. 远端部署不能直接依赖大型 Agent 框架

当前服务器依赖很少，且目标机器只有 2H2G。第一版建议先实现轻量 Tool Registry、Runtime 和 Loop，再评估 MCP SDK 或 LangGraph；避免把 CLI 编程 Agent 直接作为长期服务进程。

## 验收顺序

1. 先让当前测试全绿，确认旧 Agent/Policy/Event/Audit 行为没有回归。
2. 将现有 Agent capability 适配为只读/提议/执行工具。
3. 增加工具参数校验、权限拒绝审计和确认绑定。
4. 把 `AgentLoop` 接入 Orchestrator 的实验路径，保持同一 conversation 上下文。
5. 用一个假的 LLM 响应序列测试：
   - 第一轮调用 `location.get_current`
   - 第二轮调用 `device.get_status`
   - 第三轮输出最终回复
6. 最后再接入真实 DeepSeek 工具调用和手表端展示。
