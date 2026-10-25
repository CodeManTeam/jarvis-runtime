# 权限模型：能力 × 范围 × 有效期

- 状态：**草案**
- 取代：现有 `AutonomyLevel` 五档全局等级 + `trusted_mode` 单一开关（[app/policy.py](../../../app/policy.py)、[app/domain.py](../../../app/domain.py)）
- 相关界面：[CONSOLE_UI.md](CONSOLE_UI.md) §04、[WATCH_UI.md](WATCH_UI.md) §4

---

## 1. 为什么现有的不够用

现有模型只有两个维度：`capability.autonomy_level`（0–4）与全局 `trusted_mode`。
它能表达"这个能力有多危险"，但表达不了草图的三个问题：

| 草图提出的问题 | 现有模型 |
| --- | --- |
| 这个许可覆盖**哪一条**消息？ | ❌ 无作用域概念 |
| 这个许可**什么时候失效**？ | ❌ 只有 `pending_actions.expires_at`，与动作解耦 |
| 这个 Agent 能读到**哪些项目**？ | ❌ 无范围概念 |
| 修过内容后旧许可是否还有效？ | ❌ 未定义 |
| 这次许可被用过了吗？ | ❌ 未定义 |

## 2. 四档门控

```text
level = auto          低风险直接执行，写审计，可撤销
level = once          限定"本次范围 + 有效期"，用户点一次，用一次
level = per_action    每次动作前都要一次新许可，不继承
level = deny          拒绝执行，记录拒绝原因
```

**判定优先级（固定，不可配置）**：

```text
deny  >  per_action  >  once  >  auto
```

高风险类别永远至少是 `per_action`。**任何输入都不能把它降到 `once` 或 `auto`** ——
包括：信任 Agent、模型降级、重试、网络恢复、离线补传、用户偏好设置。
这条在代码里体现为"高风险类别的 level 是常量，不在矩阵的行内"。

## 3. 能力清单（v1.0 只开这些）

| capability | 默认档 | 可调范围 | 是否需要 scope |
| --- | --- | --- | --- |
| `reminder.write` | auto | auto / once / deny | 项目 |
| `memory.organize_short` | auto | auto / once / deny | 无 |
| `memory.write_long` | once | once / per_action / deny | 项目 |
| `reminder.adjust_today` | once | once / per_action / deny | 当天 |
| `file.read` | deny | deny / once | 路径白名单 |
| `message.send` | per_action | **不可调** | 单条消息 |
| `payment.execute` | per_action | **不可调** | 单笔 |
| `device.configure` | per_action | **不可调** | 设备 |
| `device.self_iterate` | per_action | **不可调** | 设备 + 版本 |
| `device.remote_exec` | deny | deny / per_action | 命令白名单 |
| `escalate.read` | auto | auto / once / deny | 无（只读沙箱） |
| `escalate.write` | per_action | **不可调** | 任务内容哈希 |

"不可调"意味着矩阵里不渲染单选框，只显示锁定说明——避免用户误以为自己关掉了保护。

### 3.1 升级执行（escalate）

没有现成工具时，Agent 可以写一小段脚本并执行（`system.escalate`）。
它被拆成两项能力，因为读与写的风险完全不同：

| 项 | 沙箱 | 许可 | 说明 |
| --- | --- | --- | --- |
| `escalate.read` | `read-only` | 默认自动 | 抓取、转换、一次性计算 |
| `escalate.write` | `workspace-write` | 逐次确认，**失败即拒绝** | 需要已消费的 `escalate.write` 许可；许可绑定任务内容哈希 |

两条硬约束：

1. 子进程一律以 `approval_policy=never` + 明确 `sandbox_mode` 启动——
   "不问"之所以安全，是因为沙箱本身决定了能不能做。
2. 失败必须原样上报（额度不足 / 鉴权失败 / 超时 / 找不到程序），
   不得包装成"暂时无法完成"。

## 4. 数据结构

### 4.1 `permission_grants` 表

```sql
CREATE TABLE permission_grants (
  id              TEXT PRIMARY KEY,
  capability      TEXT NOT NULL,
  level           TEXT NOT NULL,          -- auto | once | per_action | deny
  scope_kind      TEXT NOT NULL,          -- project | device | message | day | path | none
  scope_value     TEXT,                   -- 项目 ID / 设备 ID / 消息哈希 / 日期 / 路径
  subject         TEXT NOT NULL DEFAULT 'user',   -- user | agent:<name>
  granted_at      TEXT NOT NULL,
  expires_at      TEXT,                   -- once 必填；auto 可空
  single_use      INTEGER NOT NULL DEFAULT 1,
  consumed_at     TEXT,                   -- 非空即已用掉
  consumed_by     TEXT,                   -- event_id / task_id
  revoked_at      TEXT,
  revoked_reason  TEXT,
  parent_grant_id TEXT                    -- Agent 委托时指向调用者
);
CREATE INDEX idx_grants_lookup ON permission_grants(capability, scope_kind, scope_value);
```

### 4.2 运行时对象

```jsonc
{
  "id": "G-061",
  "capability": "message.send",
  "level": "per_action",
  "scope": { "kind": "message", "value": "sha256:…" },   // 正文+收件人+附件的哈希
  "subject": "user",
  "expires_at": "2026-10-24T09:45:00+08:00",
  "single_use": true,
  "revocable_after_send": false,
  "compensation_only": true
}
```

`scope.value` 取**内容哈希**而不是 ID，是为了天然满足红线 3：正文一改，哈希变，旧许可查不到。

## 5. 生命周期

```text
申请 ──▶ 待许可 ──▶ 已许可 ──▶ 已消费
          │             │
          ├─ 过期 ──────┤
          └─ 撤销 ──────┘
```

| 阶段 | 触发 | 副作用 |
| --- | --- | --- |
| 申请 | Agent 请求高风险管理动作 | 写 `pending_actions` + 申请审计 |
| 待许可 | 推送到手表/中控 | 手表显示高风险许可屏 |
| 已许可 | 用户显式确认 | 生成 grant，记录 user/时间/来源设备 |
| 已消费 | 执行前原子占用 | `consumed_at` 置位；**同一 grant 二次使用直接拒绝** |
| 过期 | `expires_at` 到期 | 卡片标"许可已过期"，不得静默重发 |
| 撤销 | 用户取消 / 内容变更 | `revoked_at` + 原因写审计 |

**原子消费**必须用 `UPDATE … WHERE id=? AND consumed_at IS NULL` 并检查 `rowcount`，
不能先查后写——否则并发下会重复外发。

## 6. Agent 委托

Agent 不能持有权限，只能继承调用者的权限，且**只能收窄**：

```text
1. 委托深度 ≤ 3
2. 委托 grant 的 level ≥ 调用者的 level（更严格或相等）
3. 委托 scope ⊆ 调用者的 scope
4. 有效期 ≤ 调用者的剩余有效期
5. Agent 不能修改自己的 grant，不能续期，不能把 deny 改成其它
```

`trusted_mode` 的替代物就是一条 `subject='agent:<name>'` 的 grant 记录，
它的存在与否可在界面上被看见、被撤销、被审计——而不再是一个隐藏的布尔。

## 7. 远程配置的二次确认

远程配置不是"一次许可"，是**两步 + 时限**：

| 步 | 动作 | 记录 |
| --- | --- | --- |
| 1 | 申请变更，展示 diff（当前值 → 目标值）、目标设备、回滚方案 | `config_change_requested` |
| 2 | 勾选"已核对设备、差异与回滚方案"后二次确认 | `config_change_approved` |
| 3 | 下发；失败自动回滚 | `config_change_applied` / `config_change_rolled_back` |

许可有效期 **2 分钟**，`single_use=1`。开关同一条配置在 2 分钟内重复应用，需重新走两步。

## 8. 审计事件（全部写入 `audit_log`）

`permission_requested` · `permission_granted` · `permission_denied` · `permission_expired` ·
`permission_revoked` · `permission_consumed` · `permission_reuse_blocked` ·
`agent_delegated` · `agent_boundary_edit`

`permission_reuse_blocked` 是重点：它证明红线 1 被真正执行过，而不是只写在文档里。

## 9. API

```text
GET    /api/permissions/matrix            能力矩阵（能力 × 档 + 生效范围）
PUT    /api/permissions/matrix/{cap}      改档（写权限修订记录）
GET    /api/permissions/grants?capability= 许可列表（含已消费/已过期）
POST   /api/permissions/grants/{id}/revoke
GET    /api/permissions/agents            受信 Agent 边界
PUT    /api/permissions/agents/{name}     改边界（能力白名单/项目范围/有效期）
```

## 10. 迁移

1. 新增表，不动 `pending_actions` 结构（它继续承载"待办事项"，grant 承载"授权"）。
2. `capability.autonomy_level` 保留只读，作为"默认档"的来源之一，逐步退役。
3. `trusted_mode` 保留一个版本：为真时，非高风险能力自动生成 `auto` grant，并在审计里标注
   `granted_by=trusted_mode_legacy`；M3 结束后移除。

## 11. 测试点（每条对应一条红线）

| 测试 | 断言 |
| --- | --- |
| 二次消费同一 grant | 第二次返回 409，`permission_reuse_blocked` 被写入 |
| 修改正文后复用许可 | 哈希不匹配，旧 grant 查不到，需重新申请 |
| 并发两个执行者抢同一许可 | 只有一个成功 |
| 降级/重试后复用许可 | 被拒绝 |
| Agent 申请超出自身范围 | 被拒绝并审计 |
| Agent 给自己续期 | 接口不存在 / 403 |
| 过期后点击确认 | 409，UI 回到"许可已过期" |
