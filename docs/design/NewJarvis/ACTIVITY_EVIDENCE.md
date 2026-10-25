# 活动与依据契约

- 状态：**草案**，在现有契约上**追加字段**，不破坏旧客户端
- 现状：[app/activity.py](../../../app/activity.py) 的 `project_event()`，
  已有 `id/source/status/tags/revision/etag/available_actions/detail`
- 相关界面：[WATCH_UI.md](WATCH_UI.md) §2–4、[CONSOLE_UI.md](CONSOLE_UI.md) §01

---

## 1. 现状缺口

现有 `detail` 只有 `reason / reply / waiting_for / task / memory_ids / history`，
能回答"做了什么"，回答不了草图要求的四件事：

| 草图要求 | 现状 |
| --- | --- |
| 这条为什么值得注意？（注意力分类） | ❌ 只有 status |
| 有多确定？（置信度 94%） | ❌ 无字段 |
| 依据是什么？（记忆/事件/工具可点引用） | ⚠️ 只有 `memory_ids`，且只有 ID |
| 有没有降级？降级损失了什么？ | ❌ 无字段 |
| 许可覆盖到哪？ | ❌ 无字段 |

## 2. 注意力分类 `attention`

四类互斥，**由服务端判定，客户端不猜**：

| 值 | 判定条件 | 卡片样式 | 允许的动作 |
| --- | --- | --- | --- |
| `high_risk_waiting` | 存在未过期 grant 请求，且 `level=per_action` | 红底整卡 | 允许 / 拒绝 / 修改 |
| `suggest_once` | 来源是 `ai_candidate` 记忆或习惯候选 | 琥珀描边 | 仅今天应用 / 忽略 |
| `auto_done` | `level=auto` 且已执行 | 绿描边 | 撤销 / 编辑 / 删除 |
| `ignored` | 用户忽略或命中规则 | 弱色 | 恢复 / 查看原记录 |

排序：`high_risk_waiting > suggest_once > auto_done > ignored`，同类按 `updated_at` 倒序。

**判定优先级**：只要存在待许可 grant，就一定是 `high_risk_waiting`，与其它条件无关。

## 3. 置信度 `confidence`

来源必须明确，不允许客户端推断：

| 场景 | 取值来源 |
| --- | --- |
| AI 判断 | `PolicyDecision.confidence`（**新增字段**，由模型输出） |
| 用户明确操作 | 恒为 `1.0`，并附 `confidence_basis="user_action"` |
| 规则命中 | `1.0`，`confidence_basis="rule"` |
| 无模型参与 | `null`，UI 显示"置信度 未提供" |

`confidence_basis` 枚举：`model | user_action | rule | none`。
**禁止**在没有依据时填 1.0 伪装确定。

## 4. 四层依据 `evidence`

```jsonc
"evidence": {
  "conclusion": "14:30 复核提醒已添加，可撤销。",
  "judgment": {
    "text": "15:00 截止，预留 30 分钟复核。",
    "confidence": 0.94,
    "confidence_basis": "model",
    "risk": "low"
  },
  "refs": {
    "memories": [
      {"id": "M-021", "title": "提案提交前预留 30 分钟复核", "source": "用户确认", "available": true},
      {"id": "M-034", "title": "今日 15:00 提案截止", "source": "用户语音 + 日历 E-108", "available": true}
    ],
    "events": [
      {"id": "E-108", "title": "北岸提案今日截止", "source": "calendar", "available": true}
    ],
    "tools": [
      {"id": "T-044", "name": "reminder.write", "result": "ok", "idempotency_key": "…", "available": true}
    ]
  },
  "raw": [
    {"id": "V-012", "kind": "voice", "duration_ms": 4000, "needs_grant": true},
    {"id": "N-108", "kind": "notification", "package": "com.example", "needs_grant": false}
  ],
  "gate": {"id": "G-044", "decision": "auto", "rule": "reminder.write/auto"}
}
```

规则：

- `refs.*[].available=false` 表示来源已被删除，UI 必须显示"资料已清理"而不是空白。
- `raw[].needs_grant=true` 的条目点击时才申请许可，且**查看原文本身写审计**。
- `gate` 是门控记录，没有它就不能声称"自动处理"。
- 列表接口只返回 `refs` 的 ID 与标题，正文走详情接口，避免列表膨胀。

## 5. 降级 `degradation`

```jsonc
"degradation": [
  {"stage": "model", "from": "J-Pro", "to": "J-Lite", "capability_loss": ["复杂附件判断"], "recovered": false},
  {"stage": "tool",  "from": "file.preview", "to": "retry",  "capability_loss": [], "recovered": true, "attempt": "1/2"}
]
```

- 空数组 = 无降级。客户端不得自行编造降级链。
- `capability_loss` 必填（可以为空数组），用于回答"损失了什么"。
- `recovered=true` 也必须保留，因为它解释了为什么界面上出现过重试。

## 6. 许可 `grant`

仅在 `attention=high_risk_waiting` 时出现：

```jsonc
"grant": {
  "id": "G-061",
  "capability": "message.send",
  "level": "per_action",
  "scope": {"kind": "message", "value": "sha256:…"},
  "expires_at": "2026-10-24T09:45:00+08:00",
  "single_use": true,
  "revocable_after_send": false,
  "compensation_only": true
}
```

`compensation_only=true` 就是手表那句"送达后无法保证撤回；只能追加更正"的数据来源。

## 7. 完整契约（`GET /api/activities` 单条）

```jsonc
{
  "id": "event:e1",
  "event_id": "e1",
  "kind": "event",

  "attention": "auto_done",
  "title": "已添加今天 14:30 的北岸提案复核提醒。",
  "status": "completed",

  "confidence": 0.94,
  "confidence_basis": "model",
  "risk": "low",

  "source": "microphone",
  "event_type": "speech",
  "device_id": "watch-a",
  "project_ids": ["p-northbank"],
  "tags": ["麦克风", "ASR", "调用工具"],

  "created_at": "2026-10-24T09:12:04+08:00",
  "updated_at": "2026-10-24T09:12:06+08:00",
  "revision": 7,
  "etag": "…",

  "available_actions": ["undo", "edit", "delete"],
  "grant": null,
  "degradation": [],
  "evidence": { "…": "见 §4" },

  "detail": {
    "reason": "reminder.write 低风险自动",
    "history": [{"audit_id": 41, "action": "tool_call", "created_at": "…"}],
    "activity_actions": [],
    "history_meta": {"created": "09:12", "modified": 0, "deleted": 0, "undone": 0}
  }
}
```

`detail.history_meta` 直接喂给卡片底部那行"创建 09:12 · 修改 0 · 删除 0 · 撤销 0"。

## 8. 与现有字段的映射

| 新字段 | 现有来源 | 改动 |
| --- | --- | --- |
| `attention` | 无 | 新增纯函数 `classify_attention(event, audits, pending)` |
| `confidence` | `events.confidence`（输入置信度，语义不同） | 新增 `PolicyDecision.confidence`，不挪用输入置信度 |
| `risk` | 无 | 由 `capability.autonomy_level` 映射：≥4 → high，3 → medium，其余 low |
| `evidence` | `detail.reason / memory_ids / history` | 重组为结构化对象，`memory_ids` 保留兼容 |
| `degradation` | 无 | 编排器在切换模型/工具重试时写入 `audit_log`，投影时聚合 |
| `grant` | `pending_actions` | 新增 grant 关联字段 |
| `history_meta` | 由 `activity_actions` 计数 | 新增聚合 |

**兼容策略**：所有新字段可空；旧客户端只读原有字段即可正常渲染。新增字段一律走 `detail`
之外的顶层，避免旧客户端把新结构当未知内容报错。

## 9. 接口

```text
GET /api/activities?view=attention|all&attention=&risk=&project=&device=&source=&tag=&cursor=
GET /api/activities/{event_id}                    含完整 evidence
GET /api/activities/{event_id}/evidence/raw/{ref} 访问原文（需 grant，写审计）
POST /api/activities/{event_id}/actions           accept|reject|modify|delete|undo|apply_today
```

`apply_today` 是新增动作，对应"仅今天应用"，**不写记忆**，只写当天覆盖规则。

## 10. 测试点

| 测试 | 断言 |
| --- | --- |
| 分类函数 | 四类各一个用例；有 pending 时必为 `high_risk_waiting` |
| 无置信度 | `confidence=null`，渲染为"未提供"而不是 100% |
| 引用缺失 | 来源被删后 `available=false`，序列化不抛错 |
| 降级聚合 | 模型切换 + 工具重试同时发生，`degradation` 两条且 `recovered` 正确 |
| 旧客户端兼容 | 去掉新字段后旧快照仍能通过 schema 校验 |
| `apply_today` | 不产生 memories 行，产生一条当日覆盖规则 |
