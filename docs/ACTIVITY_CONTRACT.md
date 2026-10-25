# Jarvis Activity Contract

版本：v0.3；服务端已提供只读 `GET /api/activities` 和单项查询接口，手表端尚未接入。
日期：2026-10-03

## 1. 目标

手表首页、日志详情和中控台需要读取同一条活动流。前端不应根据任意文本猜测“已记录”“已拒绝”或“已完成”。

读取接口支持：

```text
GET /api/activities?limit=50&cursor=...
  &source=microphone&status=waiting_user
  &device_id=watch-001&project_id=project-001
GET /api/activities/{event_id}
```

接口返回 `items` 和 `next_cursor`。游标是不透明值，客户端不得自行解析或拼接。
当前投影以既有 `events`、`audit_log`、`pending_actions`、`tasks` 和 `memories` 为事实来源；
写操作仍使用现有确认/拒绝接口，后续再统一为 Activity action endpoint。

### 当前实现边界

- 下文 JSON 为目标协议，不是当前完整返回格式。当前使用内容哈希 `etag` 检测变化，
  未实现持久化递增 `revision`、乐观写入、增量同步游标或 `activity.updated` 推送。
- 一条已有事件投影为一个稳定 `event:{event_id}` 卡片；详情路径使用原始 `event_id`，
  例如 `timer:task-id`，不要再次加 `event:` 前缀。
- 按原事件创建时间和 ID 降序分页，更新状态不改变历史分页位置。
  `limit` 为 1–200；非法游标/状态/范围返回 400，不存在的详情返回 404；接口复用 Token 鉴权。
- 默认隐藏 `ignored`，可通过 `status=ignored` 查看；拒绝保留在原卡片。
- `source` 使用现有事件来源（如 user/microphone/notification），不将 watch 虚构为事件来源；
  手表按 `device_id` 筛选。当前项目筛选为单个 context.project_id，多对多关联待实现。
- 仅有 AI 意图不构成保存成功。记忆需要实际存储记录；待办存在不表示已执行完成。
- 独立手工创建且没有关联事件的任务/记忆尚不纳入活动流。
- 详情提供审计动作及时间，不直接下发内部工具参数/原始上下文，避免设备流泄露认证信息。
  完整工具详情、原始资料引用和版本快照待下一阶段实现。
- 读取旧数据不做数据搬迁；重复初始化新增索引安全。终端尚未消费此接口。

## 2. Activity 对象

```json
{
  "id": "activity-uuid",
  "revision": 1,
  "project_ids": [],
  "source_refs": [],
  "event_id": "event-uuid",
  "task_id": null,
  "pending_action_id": null,
  "kind": "event",
  "source": "watch",
  "event_type": "speech",
  "title": "已记录「周五取快递」",
  "content": "周五取快递",
  "decision_action": "remember",
  "status": "completed",
  "created_at": "2026-10-03T14:18:00+08:00",
  "updated_at": "2026-10-03T14:18:00+08:00",
  "confidence": 0.96,
  "detail": {},
  "available_actions": []
}
```

## 3. 字段规则

| 字段 | 说明 |
| --- | --- |
| `id` | 活动稳定 ID |
| `event_id` | 原始事件 ID |
| `task_id` | 关联任务，可为空 |
| `pending_action_id` | 待确认操作 ID，可为空 |
| `kind` | `event`、`decision`、`task`、`memory`、`system` |
| `source` | `watch`、`notification`、`calendar`、`location`、`system` 等 |
| `title` | 面向用户的短标题 |
| `content` | 原始或结构化内容 |
| `decision_action` | AI 选择的动作 |
| `status` | 活动当前状态 |
| `detail` | 可展开详情 |
| `available_actions` | 当前允许的操作 |

## 4. 动作枚举

```text
reply
remember
create_task
confirm
execute
ignore
ask_user
```

## 5. 状态枚举

```text
observed       已观察
processing     处理中
waiting_user   等待用户决定
accepted       用户接受
rejected       用户暂不接受
completed      已完成
cancelled      已取消
failed         执行失败
expired        已过期
```

## 6. 用户操作

```json
{
  "action": "accept",
  "activity_id": "activity-uuid",
  "pending_action_id": "pending-uuid",
  "revision": 3
}
```

服务端必须返回新的 Activity，而不是只返回一句字符串。

## 7. 设备推送信封

```json
{
  "type": "activity.updated",
  "message_id": "message-uuid",
  "sequence": 42,
  "device_id": "watch-001",
  "activity": {}
}
```

设备处理规则：

- `message_id` 用于去重。
- `sequence` 用于检测缺失消息。
- 重复消息不得重复震动、创建任务或写入日志。
- 收到无法识别的版本时返回错误回执，不执行未知动作。

## 8. 新问答后的约束

- 用户时间线仅展示有意义输入及结果；调试审计在中控台另查，不能全部塞入首页。
- 忽略、拒绝、修改、删除和撤销更新原活动版本；不额外生成重复的用户日志行。
- 服务端同时保留变更历史及执行审计，后续门控和 Agent 能读取拒绝反馈。
- 一张卡可关联多个事件/任务/项目；上例单个 event_id/task_id 仅为兼容锚点，正式实现需引用集合。
- source_refs 指向原始资料，注明保留/已清理状态；详情包含依据摘要、步骤、工具记录，而非内部思维链。
- 首页一张主卡，展开更多；日志采用游标分页实现无限滚动和增量刷新，不一次加载所有数据。
- 建议读取返回 items/next_cursor/sync_cursor；按设备、项目、来源、状态过滤，分页排序使用时间加稳定 ID。
- 修改携带 expected revision 与 idempotency key；过期版本应冲突返回，不覆盖更新数据。
- 手表按钮传明确 activity/action ID；服务端决定下一步动作，不能一律“接受=新建任务”。
- 传输送达、用户已见、业务完成分别记录；重试不重复震动，不将工具失败标为 completed。
- 暂时性删除可保留墓碑和版本；用户明确彻底销毁时，删除策略优先于永久保留正文。
