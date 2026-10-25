# Agent 框架设计（v2 后端）

- 状态：**设计**，v2 后端重写基线
- 前提：旧后端不保留兼容（D6）。旧库只作为只读档案，不做迁移。
- 输入：[生产环境使用证据](PRODUCTION_EVIDENCE.md) —— 本文的每条设计都对应那里的一个数字。

---

## 0. 从证据里读出的五条硬需求

| 证据 | 结论 | 影响 |
| --- | --- | --- |
| 用户在三周里反复问「你是谁 / 你有什么功能 / 你被设计是什么」 | 系统**无法说明自己** | 自描述必须是一等能力，不是一句 prompt |
| 反复出现「我刚刚问了什么 / 你还记得吗」 | **会话连续性不足** | Thread 要持久化 + 可回忆，不靠上下文窗口 |
| 口述「今天的作业是同步导练 38 页」被判 `ignore` | **门控在误丢真信息** | 门控默认「先留住」，丢弃必须留回执 |
| 定时提醒与每日推荐是最高频真实请求 | **定时与重复任务是主战场** | 调度是一等公民，不是任务表的附属 |
| 说「定时给家人发条消息」 | **社交动作是刚需且高危** | 必须走逐次许可，且可撤回窗口要写清楚 |

再加一条来自记忆库的：用户明确要求记录「与学校/考试/课堂相关话题需谨慎」。
这不是一句备注，是**策略层**：语气与触发都要过这道闸。

## 1. 总体形状

```text
感知            门控            会话              管家 Agent         技能           边界            副作用
Event   ──▶  Triage  ──▶  Thread  ──▶  Butler(plan)  ──▶  Skills  ──▶  Policy  ──▶  Effects
                                   │                        │                        │
                                   ├──▶ Specialist Agents ───┘                        ▼
                                   │                                                  Audit
                                   └──▶ Memory Curator ──▶ Memory（分层）
```

六个概念，其余都是它们的投影：

| 概念 | 定义 | 为什么需要 |
| --- | --- | --- |
| **Event** | 任何输入：语音、文字、通知、日历、位置、定时器 | 统一入口，避免每个来源各写一套 |
| **Thread** | 一段有状态对话，含消息、摘要、钉住的事实 | 回答"你还记得吗" |
| **Run** | 一次 Agent 调用：计划、步骤、产物、状态 | 可恢复、可回放、可审计 |
| **Agent** | 角色声明（能力 + 工具 + 默认权限 + 语气） | 分工而非一个大 prompt |
| **Skill/Tool** | 带 schema 的原子动作，可幂等、可超时、可 dry-run | 让权限与审计有抓手 |
| **Effect** | 真正改变外部世界的动作（发消息、开网页、震动） | 只有它需要许可，只有它不可逆 |

## 2. 门控：先留住，再判断

旧系统 407 次 `ignore`，其中包含"今天的作业是同步导练 38 页"这种真信息。v2 规则：

```text
triage(event) -> 三选一，且必须留回执

act       内容指向一个动作        → 进 Thread
remember  内容是事实/状态/偏好    → 进短期记忆，不一定打扰
drop      明确噪声（静音、重复、系统回显） → 写 event_receipts，可在中控查到
```

**硬规则**

1. 任何 `drop` 必须写 `event_receipts(reason, decided_by, at)`，且中控"全量记录"里可见。
2. 语音内容**不允许**因"像是闲谈"被丢弃——那是旧系统犯的错。
3. 原始遥测（位置、心跳）**不进门控、不进记忆**，只按小时聚合成 `telemetry_hourly`。
   这条直接针对一天 1345 条位置事件的教训。

## 3. Thread：连续性的载体

```text
Thread
├─ messages[]        原始消息，保留 N 轮
├─ summary           滚动摘要（超预算时压缩，旧消息留 ID 引用）
├─ pinned_facts[]    用户明确要求记住的（如"某类话题要轻拿轻放"）
└─ open_questions[]  待用户回答的问题（"你指的是哪一条提醒？"）
```

`recall(thread, query)` 必须能回答"我们刚才聊了什么"，实现为
`最近 N 轮 + summary + 语义检索 messages`，三者拼装，不依赖模型记忆。

**验收**：任意时刻问"我上一个问题是什么"，必须能不借助模型猜测答对。

## 4. Agent 角色

| Agent | 职责 | 默认自主度 | 关键工具 |
| --- | --- | --- | --- |
| `butler` | 面向用户、理解意图、编排、决定是否打断 | 自动决定说什么 | `thread.*`、`agent.delegate`、`self.describe` |
| `schedule` | 提醒、重复任务、日历 | `auto`（可撤销提醒）/ `once`（改期） | `reminder.*`、`calendar.*`、`cron.*` |
| `study` | 教案/作业/答疑/课堂回顾 | `auto` 读，`once` 写 | `homework.*`、`lesson.*`、`qa.*`、`recall.*` |
| `device` | 手表与电脑端动作 | `auto` 震动/通知，`per_action` 打开外部 | `device.*`、`desktop.*` |
| `social` | 对外发消息 | **永远 `per_action`** | `message.send` |
| `librarian` | 记忆整理、去重、冲突裁决 | 后台，不打扰 | `memory.*` |

**委托规则**：Agent 之间可委托，但权限只减不增、深度 ≤3、总时限与预算继承调用者
（沿用 [PERMISSION_MODEL.md](PERMISSION_MODEL.md) §6）。

**新增：`self.describe`** —— 这是回应"你是谁/你能做什么"的直接答案来源：

```json
{
  "name": "Jarvis",
  "role": "私人管家",
  "can": [{"what": "定时提醒", "detail": "可撤销，自动执行", "examples": ["9:40 提醒我吃早饭"]}],
  "cannot": [{"what": "读取未授权文件", "reason": "权限禁止"}],
  "needs_permission": [{"what": "发微信给他人", "detail": "每次都要你确认"}],
  "remembers": {"count": 11, "recent": ["..."], "note": "可查看与忘记"},
  "limits": {"quiet_hours": "23:00–07:00", "daily_tokens": 50000000}
}
```

它必须由**真实注册表**生成，不能是文案。能力变了它自动变。

## 5. Run：可恢复的执行

```text
Run(id, thread_id, agent, goal, plan[], steps[], status, budget, deadline, artifacts)
Step(index, title, tool, args, idempotency_key, status, attempts, result, error)
```

状态机：`queued → running → (waiting_permission | waiting_user | waiting_tool) → done | failed | cancelled`

- 每步执行前落盘，崩溃后可续跑；已完成且幂等的步骤不重跑。
- 失败重试 ≤2 次，之后进 dead-letter，**向用户报告失败事实**，不静默。
- 外部副作用步骤（`message.send`）永远不重试，重试的是"查询结果"。

> 旧系统里 `早餐提醒`/`开会提醒` 卡在 `waiting`、`会议提醒` 直接 `failed`，
> 就是缺这套状态机与恢复语义。

## 6. 调度是一等公民

真实请求里最高频的是"每天 12 点提醒吃饭"、"每天 11:46 推荐午饭"、"
10:30 去学校"等等。所以：

```text
Schedule(id, cron|interval, timezone, agent, goal, payload, enabled, next_run_at,
         last_run_at, last_status, misfire_policy, budget_per_run)
```

- 用**用户时区**（Asia/Shanghai）计算下一次触发，存 UTC。
- 错过触发（设备离线/服务重启）按 `misfire_policy`：`skip` | `run_once` | `catch_up`。
  提醒类默认 `run_once`，推荐类默认 `skip`。
- 每次触发是一次 Run，可查历史与失败原因。

## 7. 记忆：只固化模式，不固化噪声

| 层 | 写入者 | 内容 | 保留 |
| --- | --- | --- | --- |
| `raw` | 采集 | 音频/图片/原文引用 | 按条许可，默认 7 天 |
| `short` | 门控 `remember` | 事实、状态、待办摘要 | 7 天 |
| `long` | `librarian` 周整理 | 稳定偏好、习惯、人物关系 | 直到忘记 |
| `pinned` | 用户明确要求 | 如"敏感话题规则" | 直到用户解除 |

**禁止**：原始遥测写记忆（旧系统 7 条位置记忆全是噪声）。
**禁止**：一次行为直接升为长期偏好，必须 ≥3 次且无反例（沿用 M0.2）。

## 8. 边界与许可

直接复用 v1.0 已实现的能力矩阵（[PERMISSION_MODEL.md](PERMISSION_MODEL.md)）：

- 四档：`auto / once / per_action / deny`，优先级 `deny > per_action > once > auto`。
- `message.send`、`payment.*`、`device.configure`、`device.self_iterate`、`device.remote_exec`
  为**锁定档**，不可下调。
- 许可绑定内容哈希；用户确认即签发并消费；同一动作不得确认两次。

**新增：敏感话题闸**（`tone_policy`）。命中用户标记过的敏感话题时：

```text
- 不问"你准备好了吗"、不做进度催促
- 不主动把话题引向学业
- 若用户情绪低落，降低信息密度，只回应不追问
```

这条是策略层，不是 prompt 技巧；命中记录进审计，便于复核。

## 9. 与现有代码的关系

| 现有 | v2 处置 |
| --- | --- |
| `permission_grants` / 矩阵 / 内容哈希 / 确认即签发 | **保留**，已是正确形状 |
| 记忆分层与候选（M0.2） | **保留** |
| 活动四层依据（M0.3） | **保留**，需补 `thread_id` 与 `run_id` 关联 |
| 设备会话与指标、kill switch（M0.4） | **保留** |
| `JarvisService` 巨石（2000+ 行） | **拆**：`runtime/`（Run/Schedule）、`agents/`（角色）、`skills/`（工具）、`memory/`、`policy/` |
| `events`/`tasks`/`memories` 旧 schema | **不迁移**，新库从零建；旧库只读挂载为档案 |
| 1.0 的中控与手表 | 边缘 API 契约**保持不变**（活动流、许可、指标）。重写的是内部，不是界面 |

**为什么保留边缘契约**：中控与手表的 5 页 + 9 屏是刚花力气做完并验收过的，
换掉内部实现却保留 HTTP 形状，能让 M1/M2 的成果继续有效；
"不兼容旧后端"真正要丢掉的是**数据与内部结构**，不是用户已经能用的界面。

## 10. 实施顺序（建议）

| 阶段 | 内容 | 完成标志 |
| --- | --- | --- |
| V2-0 | 新库 schema + `Runtime`（Run/Step/Schedule）+ 状态机 | 崩溃后 Run 可续跑，有测试 |
| V2-1 | `Thread` 与 `recall` | "上一个问题是什么"能答对 |
| V2-2 | `self.describe` 从注册表生成 | 能力变化后描述自动更新 |
| V2-3 | 门控三态 + `event_receipts` | 口述作业不再被丢弃 |
| V2-4 | `schedule` Agent + 每日提醒 | 连续 7 天准点，错过有策略 |
| V2-5 | `study` Agent（教案/作业/回顾） | 一次真实课堂使用记录 |
| V2-6 | `social` + 许可闭环 | 发消息需逐次确认，可查回执 |

## 11. 明确不做

- 不做旧数据迁移（用户已确认）。
- 不做多用户、协作、水平扩展。
- 不做离线智能。
- 不做 TTS 默认播报（生产 10 天 TTS 字符数为 0，说明要么坏了要么不被需要；
  默认静音，作为可选能力保留）。
