# 工具级权限闸（Tool Capability Gate）

- 状态：**已定**，需要实现
- 起因：生产上 `device.open_url` 被直接执行，`confirmation_id` 为空
- 结论：**闸门必须在工具执行之前，而不是决策之后**

---

## 1. 问题

现在的链路：

```
模型在工具循环里选工具 → call_tool() 直接执行 → 循环结束 → Policy.validate(decision)
```

Policy 校验的是**最终决策**，但副作用已经发生。审计里那条 `action=execute`,
`confirmation=None` 就是证据：一台在线设备的话，链接已经被打开了。

## 2. 设计

### 2.1 一个映射：工具 → 能力

能力包自带 `capability`；核心工具用一张静态表补齐：

```python
TOOL_CAPABILITY = {
    "device.get_status": None,        # 只读，不设闸
    "device.list": None,
    "task.list": None,
    "memory.search": None,
    "event.list_recent": None,
    "calendar.list_recent": None,
    "location.get_latest": None,
    "device.open_url": "device.open_url",
    "device.vibrate": "device.vibrate",
    "device.notify": "device.notify",
    "system.escalate": "escalate.read",   # 写由 handler 内部再查 escalate.write
}
```

新增权限条目：

| 能力 | 默认 | 可调 | 说明 |
| --- | --- | --- | --- |
| `device.open_url` | `per_action` | **不可调（锁定）** | 打开外部链接 |
| `device.vibrate` | `auto` | auto/once/deny | 可撤销 |
| `device.notify` | `auto` | auto/once/deny | 可撤销 |

### 2.2 闸门放在哪

```python
async def call_tool(name, arguments):
    tool = resolve(name)
    capability = capability_for(tool)
    if capability:                                  # 有闸的能力
        verdict = policy.gate(capability, scope_for(tool, arguments))
        if verdict is DENY:
            audit("tool_blocked", ...); return denied_result
        if verdict is NEEDS_PERMISSION:
            audit("tool_blocked", ...); return permission_result   # 不执行
    return await tool_runtime.execute(...)
```

返回值不是抛异常，而是结构化结果：

```json
{"ok": false, "blocked": "needs_permission", "capability": "device.open_url",
 "scope": "<sha256>", "reason": "这个动作需要你逐次许可"}
```

循环看到 `blocked` 就**停止调用**，把这个事实带回最终决策；
orchestrator 把决策转成 `CONFIRM`，pending action 里带
`capability` / `scope_kind` / `scope_value`，与已有许可流程完全复用。

### 2.3 作用域

- `device.open_url`：`scope_kind="tool"`, `scope_value=sha256(tool + 规范化参数)`
- 规范化：参数按 key 排序的 JSON，`separators=(",", ":")`，`ensure_ascii=False`
- 改参数即换 scope，旧许可自动失效（与消息哈希同一套规则）

### 2.4 不许绕过的路径

1. **Agent 委托**：权限只减不增（已有规则），闸门在每次 `call_tool` 都跑。
2. **重试**：重试复用同一个 scope，但许可是一次性的——第二次仍会被挡，并写
   `permission_reuse_blocked`。
3. **system.escalate**：它的 handler 内部还要查 `escalate.write`，两道闸。

### 2.5 审计

| 事件 | 何时 |
| --- | --- |
| `tool_gate_passed` | 无闸或已授权 |
| `tool_blocked` | deny / needs_permission，含 capability 与 scope |
| `tool_executed` | 真正执行后（复用现有 tool_call 审计） |

## 3. 验收

1. 未授权调用 `device.open_url`：**handler 一次都不执行**（假工具计数为 0），
   返回 `blocked=needs_permission`，审计有 `tool_blocked`。
2. 授权后同一 scope 可执行一次；第二次被挡并写 `permission_reuse_blocked`。
3. 只读工具不产生额外闸门开销（不查 grant）。
4. 参数变化导致 scope 变化，旧许可失效。
5. `system.escalate` 写路径仍需 `escalate.write`。

## 4. 不做

- 不做"每个工具一个审批弹窗"——只对声明了能力的工具设闸。
- 不改 Policy 对**最终决策**的校验逻辑，两者并存：工具闸挡副作用，决策闸定动作。
