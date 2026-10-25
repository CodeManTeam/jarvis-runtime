# Event Runtime 定时唤醒协议

## 目标

定时任务不是“到点发送一条固定提醒”，而是一个可以唤醒 Agent 的事件源：

```text
任务到期
  -> Scheduler 找到 due task
  -> 生成 timer.fired 事件
  -> Agent Runtime 读取同一上下文
  -> Agent 决定查询、调用工具、TTS 或创建后续任务
  -> 记录运行结果和审计
```

当前服务已经有 `JarvisService.get_due_tasks()`、
`JarvisService.publish_event()` 和 `JarvisService.dispatch_due_tasks()`。
本文档定义它们的 Event Runtime 契约。当前无 worker 的调度路径已经能够
向终端发送 `timer.fired`；worker 已运行时仍必须保持相同的事件身份和
载荷语义，不能退化成旧的 `task_due` 消息。

当前 `app.runtime` 已提供无存储依赖的核心：

- `AgentRun` 记录事件触发的一次 Agent 执行及其状态迁移。
- `EventEnvelope` 统一包装现有 `domain.Event` 和外部事件。
- `TimerFired` 提供定时唤醒的标准事件结构。
- `EventRuntime.accept()` 按 `event_id` 幂等接收事件。
- `EventRuntime.dispatch()` 创建、唤醒、挂起、完成或失败 AgentRun。

当前生产服务已经在 Event Worker 运行时用 `EventRuntime` 创建 AgentRun，
并写入 `agent_run_started`、`agent_run_completed` 或
`agent_run_failed` 审计。AgentRun 状态本身仍是进程内存储，后续需要迁移到
SQLite 才能支持进程重启恢复。

## 到期任务选择

`get_due_tasks()` 只应返回同时满足以下条件的任务：

1. `status = 'pending'`
2. `run_at IS NOT NULL`
3. `run_at <= now`
4. `next_attempt_at IS NULL OR next_attempt_at <= now`
5. 任务依赖全部处于 `done`

结果按 `run_at ASC` 排序。`waiting`、`running`、`done`、`failed`、
`cancelled` 和 `paused` 任务不会再次被调度。没有 `run_at` 的任务也不会
被 Scheduler 当作定时任务。

## `timer.fired` 事件

每个到期任务应转换为一个结构化事件，并通过统一 Event Runtime 入口发布。
推荐的最小载荷如下：

```json
{
  "type": "timer.fired",
  "event_type": "timer.fired",
  "event_id": "timer:<task_id>",
  "dedupe_key": "timer:<task_id>",
  "task_id": "task-123",
  "title": "带伞",
  "payload": {
    "device_id": "watch-6110d460",
    "instruction": "如果下雨就提醒我"
  },
  "device_id": "watch-6110d460",
  "scheduled_at": "2026-10-03T08:00:00+00:00"
}
```

字段要求：

| 字段 | 要求 |
| --- | --- |
| `type` | 固定为 `timer.fired`，供终端和事件网关路由 |
| `event_type` | 固定为 `timer.fired`，供 Event Normalizer/Agent Runtime 使用 |
| `event_id` | 稳定值，推荐 `timer:<task_id>`；重试不能生成新 ID |
| `dedupe_key` | 与一次任务触发绑定，推荐 `timer:<task_id>` |
| `task_id` | 原任务主键，必须存在 |
| `title` | 原任务标题 |
| `payload` | 原任务 JSON payload，必须是可序列化对象 |
| `device_id` | 目标设备；优先使用 payload 中的 `device_id`，没有则为空或进入用户路由 |
| `scheduled_at` | 原任务 `run_at`，不能用实际发送时间替代 |

`task` 数据库整行可以保留在内部审计中，但不能直接作为终端协议载荷：
SQLite row、锁字段和内部错误字段不应泄漏到手表或外部 MCP。

## 幂等语义

调度器至少要保证“同一个任务的同一次触发”不会因为轮询或进程重启而
无限生成不同事件：

- `event_id` 和 `dedupe_key` 必须稳定地绑定 `task_id`。
- 任务领取使用原子 claim；只有成功 claim 的 worker 才能发布事件。
- 发布前后发生进程崩溃时，恢复逻辑必须按 `event_id` 检查已存在的事件、
  AgentRun 或投递记录，避免重复执行。
- 事件重复到达时，Event Runtime 应返回原处理结果或标记为 duplicate，
  不应再次产生震动、TTS、通知等副作用。
- 只把任务置为 `waiting` 视为“事件已成功交给下游”的当前兼容语义；
  完整实现应区分 `claimed`、`published`、`delivered` 和 `completed`。

推荐的持久化顺序是：

```text
claim task
  -> insert timer.fired event with unique event_id
  -> create AgentRun / enqueue event
  -> mark task published or waiting
```

`events.id` 当前可以承载 `event_id`，但需要主线明确唯一约束或在写入前
做幂等查询。仅依靠进程内锁不够，因为服务器重启后锁状态会丢失。

## 任务状态语义

当前状态与定时唤醒的约定如下：

```text
pending
  -> running       claim 成功，防止多个 worker 同时处理
  -> waiting       推送/发布成功，等待 Agent 或设备后续完成
  -> done          任务的业务目标完成
  -> failed        重试耗尽或不可恢复错误
  -> cancelled     用户取消
```

需要注意：`waiting` 不等于“用户已经收到”或“Agent 已经完成”。后续
Event Runtime 应用独立的 AgentRun 状态记录：

```text
queued -> running -> waiting_tool -> waiting_device -> completed
                                      \-> failed
```

只有 AgentRun 完成任务定义的业务目标后，才应调用 `complete_task()`。
如果只是 timer 事件成功发布，不应直接把任务标记为 `done`。

## 失败和重试

当前 `dispatch_due_tasks()` 的投递失败语义：

- `retry_count` 加一。
- 未超过 `max_retries` 时，任务回到 `pending`。
- `next_attempt_at` 使用指数退避：`5s, 10s, 20s ...`，上限 `3600s`。
- 达到最大重试次数后，状态变为 `failed`，写入 `dead_letter_at`。
- 每次失败都应写入 `task_delivery_failed` 审计；最终失败写入
  `task_dead_lettered` 审计。

重试必须复用同一个 `event_id`，而不是把每次尝试当作新的定时任务。
如果失败发生在事件已经持久化、但设备未确认的阶段，应重试“下游投递”
或恢复 AgentRun，而不是再次创建新的业务任务。

错误应分类：

| 类型 | 是否重试 |
| --- | --- |
| 设备离线、WebSocket 暂时断开、网络超时 | 是 |
| 事件队列暂时不可用 | 是 |
| payload/schema 非法 | 否，直接 `failed` 并告警 |
| 任务已取消或过期 | 否 |
| Agent 明确拒绝执行 | 通常否，等待用户决策 |

## 当前服务接口

当前调度器使用以下接口：

```python
async def publish_event(event: Event) -> dict[str, Any]:
    """发布事件；生产定时事件会唤醒 AgentRun。"""


async def dispatch_due_tasks() -> int:
    """claim due tasks, publish timer.fired, update task state."""
```

Scheduler 只负责领取任务和发布事件。当前的 `timer.fired` 终端载荷是
没有 Event Worker 或设备需要直接接收事件时的兼容适配层：

```python
await event_runtime.publish(
    event_type="timer.fired",
    event_id=f"timer:{task['id']}",
    payload={
        "task_id": task["id"],
        "title": task["title"],
        "payload": payload,
        "device_id": payload.get("device_id", ""),
        "scheduled_at": task["run_at"],
    },
)
```

Event Runtime 会决定是否唤醒 Agent、执行 MCP 工具、发送 `agent_event`
和 TTS，并记录 AgentRun，而不是由 Scheduler 直接拼接一条提醒文本。

## 测试契约

`tests/test_scheduler_runtime.py` 覆盖以下边界：

- 只选择真正到期且可重试的 pending 任务。
- 到期任务必须转换为 `timer.fired`，并包含 `task_id`、`title`、
  `payload`、`device_id`、`scheduled_at`。
- 成功调度后再次轮询不能重复投递。
- 暂时投递失败会回到 `pending` 并设置退避时间。
- 超过最大重试次数进入 `failed`/dead-letter。
- 事件载荷必须能脱离 SQLite row 直接 JSON 序列化。
- 稳定 `event_id` 和 `dedupe_key` 已进入终端事件载荷；后续主线还必须
  将它们持久化到事件/AgentRun 表，并覆盖进程崩溃后的恢复窗口。
