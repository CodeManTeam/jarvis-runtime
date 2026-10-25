# 主 Agent 与项目治理架构

更新日期：2026-10-04

本文档定义 Jarvis 作为「主 Agent 管理多对话、多任务、多项目」的目标架构，
以及支撑它的 `ProjectService` 数据模型、状态机与上下文预算。

本文档只描述设计，不声称已实现。实现状态见文末「落地状态」。

## 1. 目标与边界

### 1.1 目标

Jarvis 是**一个**主 Agent，同时：

- 在不同对话通道（手表、网页、语音）里保持同一个身份和记忆；
- 管理多个并行项目，知道每个项目的状态与阻塞点；
- 在自然间隙汇报进展，而不是每条信息都打断用户。

### 1.2 核心认知

> Jarvis「知道」所有项目，不等于「把所有项目装进脑子里」。

单一上下文装不下「管所有」：10 个活跃对话 × 20 轮 × 500 token 已是 100K，
再加项目过程记录会彻底击穿语音延迟预算。因此主 Agent 持有的是**索引**，
不是**全量内容**。

### 1.3 明确不做

| 不做 | 理由 |
| --- | --- |
| 把全部项目状态写进 system prompt | 击穿 KV cache，项目数一多就爆 |
| 让主 Agent 持有项目完整工作日志 | 上下文失控，中段信息被稀释 |
| 项目会话之间直接通信 | 形成网状依赖，状态无法推理 |
| 主 Agent 主动轮询项目状态 | 浪费 token，应由事件推送 |
| 一个 Session 装多个项目 | 项目 A 的探索污染项目 B 的决策 |
| 项目嵌套（子项目） | 渲染复杂度爆炸，扁平足够 |
| 项目级权限隔离 | 单用户场景无意义 |

## 2. 三层架构

```text
┌─────────────────────────────────────────────────────┐
│ L1  主会话（常驻 Session，唯一的「Jarvis 本体」）      │
│                                                       │
│  持有：人格 · 用户模型摘要 · 项目索引（每项一行）       │
│        待办摘要 · 当前对话                             │
│  不持有：任何项目的详细过程 · 其他对话的原文            │
└───────────────┬─────────────────────────────────────┘
                │
      ┌─────────┴─────────┬──────────────┐
      ▼                   ▼              ▼
┌───────────┐      ┌───────────┐   ┌───────────┐
│ L2 项目会话 │      │ L2 项目会话 │   │ L2 对话会话 │
│ project A │      │ project B │   │ 手表闲聊   │
│ + 子Agent树│      │ + 子Agent树│   │           │
└─────┬─────┘      └─────┬─────┘   └─────┬─────┘
      └──────────────────┴───────────────┘
                         ▼
┌─────────────────────────────────────────────────────┐
│ L3  共享状态层（Python 侧车 / SQLite）                 │
│  user_model · projects · tasks · inbox · memories     │
│  所有会话读写同一份 —— 这是「同一个 Jarvis」的物理基础  │
└─────────────────────────────────────────────────────┘
```

分层理由：

- **L1 上下文恒定小**（< 8K），保证语音首字延迟可控；
- **L2 每项目独立压缩**，互不污染，可独立归档；
- **L3 保证身份统一**，记忆不因会话不同而分裂。

## 3. 三档感知机制

主 Agent 如何「知道」其他会话，成本递增。

### 3.1 档 1：推送摘要（零成本，被动）

```text
项目会话：任务完成 / 阻塞 / 失败
    ↓  log-only 事件 + 一条摘要
主会话：收到「项目 A 完成了，结论是 X」
```

实现方式：项目会话通过 `steer()` 向主会话插一条消息。
只推**结果**，不推过程。

### 3.2 档 2：索引查询（低成本，按需）

用户问「我手头有什么」时，主 Agent 调用 `jarvis_projects` 工具，
返回每项目一行：

```text
[项目 3] 提醒免打扰联动   active    进度 60%  阻塞：等你确认改策略引擎
[项目 1] 通知摘要优化     done      已完成
```

成本：一次工具调用，几百 token。

### 3.3 档 3：委派深潜（高成本，低频）

用户问「项目 A 卡在哪」时，主 Agent 委派 subagent 读取项目会话：

```text
subagent("读取项目 project-3 的会话记录，归纳当前进展和阻塞点，200 字以内")
```

子 Agent 读完只回传摘要，**过程中读取的数万 token 不进主会话**。

### 3.4 三档不可缺一

- 缺档 1：主 Agent 永远不知道发生了什么；
- 缺档 2：退化成「每件事都深入」，成本失控；
- 缺档 3：只能泛泛而谈，无法回答细节。

## 4. 上下文预算

这是决定性约束。

| 内容 | 位置 | 大小 | 刷新时机 |
| --- | --- | --- | --- |
| 人格 + 决策规则 | system prompt 静态段 | ~2K | 会话内不变 |
| 用户模型摘要 | system prompt 静态段 | ~500 | 日级 |
| **项目索引（每项一行）** | 每轮注入 | ~50/项 | 项目状态变化 |
| 待办摘要 | 每轮注入 | ~200 | 任务变化 |
| 设备状态 | 每轮注入 | ~100 | 每轮 |
| 记忆 Top20 | 每轮注入 | ~800 | 每轮 |
| 当前对话 | Session 历史 | 增长 | 每轮 |
| 深入项目的详情 | **不进主会话** | — | 由 subagent 消化 |

主会话稳态目标：**< 8K + 当前对话**。
20 个项目的索引也只有约 1000 token。

## 5. 项目数据模型

### 5.1 表结构变更

现有 `projects` 表只有 `id/name/goal/status/metadata/created_at/updated_at`，
需补充九个字段。沿用 `db.py` 的 `_add_column_if_missing` 幂等模式：

```python
for column, definition in (
    ("session_id", "TEXT"),
    ("one_liner", "TEXT NOT NULL DEFAULT ''"),
    ("context_snapshot", "TEXT NOT NULL DEFAULT '{}'"),
    ("origin", "TEXT NOT NULL DEFAULT 'user'"),
    ("blocked_reason", "TEXT"),
    ("progress", "INTEGER NOT NULL DEFAULT 0"),
    ("revision", "INTEGER NOT NULL DEFAULT 0"),
    ("last_activity_at", "TEXT"),
    ("closed_at", "TEXT"),
):
    self._add_column_if_missing(connection, "projects", column, definition)

connection.execute(
    "CREATE INDEX IF NOT EXISTS idx_projects_status "
    "ON projects(status, updated_at DESC)"
)
```

### 5.2 字段语义

| 字段 | 写入方 | 读取方 | 说明 |
| --- | --- | --- | --- |
| `session_id` | ProjectService | 主 Agent（档 3） | DSH Session id，懒创建 |
| `one_liner` | ProjectService | **主 Agent（档 2）** | 最关键字段，≤40 字 |
| `context_snapshot` | 立项时冻结 | 项目会话启动 | 防止项目漂移 |
| `origin` | 创建方 | 决策流 | `user` / `auto` / `scheduled` |
| `blocked_reason` | 状态机 | 主 Agent | `blocked` 时必填 |
| `progress` | 任务完成度推算 | 主 Agent | 粗粒度 0-100 |
| `revision` | 每次变更 +1 | 调用方 | CAS 校验 |
| `last_activity_at` | 活动更新时 | 陈旧检测 | 超期可提醒 |
| `closed_at` | 进入终态时 | 归档查询 | — |

### 5.3 `context_snapshot` 的作用

项目可能运行数小时，期间用户状态会变化，但**项目应基于立项时的理解推进**。
快照冻结当时的设备状态、近期事件、记忆摘要，避免项目目标漂移。

## 6. 项目状态机

### 6.1 合法转移

```python
PROJECT_TRANSITIONS: dict[str, set[str]] = {
    "proposed":  {"active", "cancelled"},
    "active":    {"paused", "blocked", "done", "failed", "cancelled"},
    "paused":    {"active", "cancelled"},
    "blocked":   {"active", "failed", "cancelled"},
    "done":      set(),
    "failed":    set(),
    "cancelled": set(),
}

TERMINAL_STATUSES = {"done", "failed", "cancelled"}
```

### 6.2 状态图

```text
                  ┌──────────┐
   自动识别 ──────►│ proposed │
                  └────┬─────┘
                       │ 用户确认
                  ┌────▼─────┐
   用户显式创建 ──►│  active  │◄──────┐
                  └──┬─┬─┬─┬─┘       │
                     │ │ │ │         │
          ┌──────────┘ │ │ └─────────┘
          │            │ │     恢复
    ┌─────▼────┐  ┌────▼──┴──┐
    │  paused  │  │ blocked  │
    └────┬─────┘  └────┬─────┘
         │             │
         └──────┬──────┘
                │
    ┌───────┬───▼───┬──────────┐
    ▼       ▼       ▼          ▼
  done   failed  cancelled  (终态)
```

### 6.3 约束

- **状态转移由代码强制**，模型只能在合法转移中选；
- `blocked` 必须携带 `blocked_reason`，否则拒绝；
- 终态不可回转（`done`/`failed`/`cancelled` 无出边）；
- 并发更新用 `revision` 做 compare-and-set，陈旧写入被拒绝而非覆盖。

## 7. ProjectService

### 7.1 职责边界

| 负责 | 不负责 |
| --- | --- |
| 项目状态、摘要、Session 绑定 | 执行具体工作（属 L2 项目会话） |
| 状态机校验与 CAS | 生成自然语言摘要（由调用方或 LLM 提供） |
| 索引视图渲染 | 任务调度 |

### 7.2 方法清单

| 方法 | 用途 |
| --- | --- |
| `create(name, goal, origin, metadata, context_snapshot)` | 创建项目；`origin=user` 直接 `active`，否则 `proposed` |
| `get(project_id)` | 读取单个项目 |
| `list(status, limit)` | 列表查询 |
| `update_status(project_id, target, reason, one_liner, progress, expect_revision)` | 状态流转 |
| `touch_activity(project_id, one_liner, progress)` | 刷新摘要，不改状态 |
| `index_view(limit)` | 主 Agent 用的紧凑视图 |
| `render_index_text(limit)` | 渲染成可注入上下文的文本 |
| `bind_session(project_id, session_id)` | 绑定 DSH Session，幂等 |

### 7.3 核心实现

```python
class ProjectService:
    """项目生命周期管理。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        name: str,
        goal: str = "",
        *,
        origin: str = "user",
        metadata: dict[str, Any] | None = None,
        context_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project_id = str(uuid.uuid4())
        timestamp = _now()
        status = "active" if origin == "user" else "proposed"
        self.database.execute(
            """
            INSERT INTO projects
            (id, name, goal, status, metadata, context_snapshot,
             origin, one_liner, progress, revision, last_activity_at,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
            """,
            (
                project_id, name, goal, status,
                json.dumps(metadata or {}, ensure_ascii=False),
                json.dumps(context_snapshot or {}, ensure_ascii=False),
                origin,
                self._initial_one_liner(name, goal, status),
                timestamp, timestamp, timestamp,
            ),
        )
        return self.get(project_id) or {}

    def update_status(
        self,
        project_id: str,
        target: str,
        *,
        reason: str | None = None,
        one_liner: str | None = None,
        progress: int | None = None,
        expect_revision: int | None = None,
    ) -> dict[str, Any]:
        project = self.get(project_id)
        if project is None:
            raise KeyError(f"unknown project: {project_id}")
        if expect_revision is not None and project["revision"] != expect_revision:
            raise StaleProjectRevision(
                f"项目已被更新（当前 revision={project['revision']}，"
                f"你的 revision={expect_revision}）"
            )
        check_transition(project["status"], target)
        if target == "blocked" and not reason:
            raise ValueError("转为 blocked 必须提供原因")
        # ... 组装 fields 并 UPDATE
```

### 7.4 索引视图

```python
def index_view(self, *, limit: int = 20) -> list[dict[str, Any]]:
    """返回给主 Agent 的紧凑索引，每个项目一行。"""
    rows = self.database.fetch_all(
        """
        SELECT id, name, status, one_liner, progress,
               blocked_reason, last_activity_at
        FROM projects
        WHERE status NOT IN ('done', 'cancelled')
        ORDER BY
            CASE status
                WHEN 'blocked'  THEN 0
                WHEN 'active'   THEN 1
                WHEN 'proposed' THEN 2
                WHEN 'paused'   THEN 3
                ELSE 4
            END,
            updated_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [...]
```

排序刻意把 `blocked` 放最前，因为那是唯一需要用户决策的状态。

### 7.5 异常类型

```python
class InvalidProjectTransition(ValueError):
    """非法状态转移。"""


class StaleProjectRevision(ValueError):
    """CAS 版本冲突。"""
```

## 8. Session 绑定

### 8.1 懒创建

项目创建时**不建 Session**：`proposed` 状态的项目可能被否掉，提前建会浪费。

### 8.2 状态对应

| 项目状态 | Session 状态 | 说明 |
| --- | --- | --- |
| `proposed` | 无 | 还没开工 |
| `active` | 存在，可能活跃或休眠 | 首次工作时创建 |
| `paused` | 存在，休眠 | 保留可恢复 |
| `blocked` | 存在，休眠 | 保留可恢复 |
| `done`/`failed`/`cancelled` | 存在，归档 | 供档 3 深潜 |

**不销毁 Session 的理由**：归档项目的会话是宝贵历史，
档 3 深潜（「上次那个项目是怎么解决的」）需要它。

### 8.3 绑定流程

```text
jarvis_project_open(project_id)
  1. GET /projects/{id}          → 拿到 session_id
  2. session_id 存在 → 直接 resume
  3. session_id 为空 → 请求 DSH 创建新 Session
     → POST /projects/{id}/bind { session_id }
     → 返回
```

方向是**插件创建 Session，然后告知 Python**。Python 只负责记录绑定。

## 9. one_liner 生成策略

这是主 Agent 唯一的项目信息来源，必须准确。

### 9.1 三层策略

| 层次 | 触发时机 | 成本 | 质量 |
| --- | --- | --- | --- |
| 规则模板 | 状态变更时 | 0 | 够用 |
| 任务标题 | 任务完成时 | 0 | 较好 |
| LLM 精炼 | `blocked` / `done` 时 | 低 | 最好 |

### 9.2 规则模板

```python
ONE_LINER_TEMPLATES = {
    "proposed":  "待确认：{goal}",
    "active":    "进行中：{goal}",
    "paused":    "已暂停：{goal}",
    "blocked":   "阻塞：{reason}",
    "done":      "已完成：{goal}",
    "failed":    "失败：{reason}",
    "cancelled": "已取消",
}
```

### 9.3 任务驱动更新

```python
project_service.touch_activity(
    project_id,
    one_liner=f"已完成「{task.title}」，下一步：{next_task.title or '待定'}",
    progress=int(done_count / total_count * 100),
)
```

### 9.4 LLM 精炼

只在 `blocked` 和 `done` 触发一次（这两个状态最需要说清楚），
用便宜模型，单次约 200 token，输出限制 25 字。

## 10. 对话划分

### 10.1 现状问题

```python
# service.py:538
conversation_id = conversation_id or device_id or "default"
```

按设备划分，导致换设备就丢上下文，而手表和手机本是同一个人。

### 10.2 建议改为按场景划分

| 维度 | 现在 | 建议 |
| --- | --- | --- |
| 划分依据 | `device_id` | 场景 + 时间窗 |
| device | 会话标识 | 降级为通道标记 |
| 生命周期 | 2 小时 | 对话 7 天；项目至关闭 |

```python
@dataclass
class Conversation:
    conversation_id: str
    scene: str              # chat / project:<id> / voice
    device_id: str | None   # 通道，不是主键
    session_id: str         # 映射到 DSH Session
    last_active: datetime
```

效果：

- 手表问天气、手机问天气 → 同一 `chat` 场景，共享上下文；
- 手表说「项目 A」 → 自动切到 `project:3` 场景；
- 多设备同时说话 → 同场景并发输入，由 `steer` 排队。

## 11. 迁移与测试

### 11.1 迁移步骤

| 步骤 | 操作 | 风险 |
| --- | --- | --- |
| 1 | `db.py` 加 ALTER 语句与索引 | 低（幂等） |
| 2 | 新建 `app/projects.py` | 无 |
| 3 | `service.py` 的 `create_project`/`list_projects` 委托 | 低 |
| 4 | `main.py` 加 `PATCH /api/projects/{id}` 状态流转接口 | 无 |
| 5 | 存量 `active` 项目保持不变 | 无 |

**注意**：存量数据没有 `one_liner`，默认空串。
`render_index_text` 需跳过空摘要、只显示项目名，不得报错。

### 11.2 测试清单

```text
test_create_user_project_is_active
test_create_auto_project_is_proposed
test_invalid_transition_rejected          # done → active 必须失败
test_blocked_requires_reason
test_stale_revision_rejected
test_index_view_excludes_terminal
test_index_view_orders_blocked_first
test_bind_session_is_idempotent
test_render_index_text_empty              # 无项目时返回固定文案
```

### 11.3 兼容点

| 现有用法 | 影响 |
| --- | --- |
| `ContextBuilder` 读 `projects` 表 | 不受影响，`SELECT *` 兼容新列 |
| `activity.py` 按 `project_id` 投影 | 不受影响 |
| `main.py` 的 `POST /api/projects` | 改为委托，新增 `origin` 参数 |
| `pending_actions.project_id` | 不受影响 |

## 12. 落地顺序

```text
第 1 步  projects 表补字段 + 状态机 + ProjectService
         ↓  验收：能创建项目、查列表、状态流转

第 2 步  档 2：jarvis_projects 工具 + 主会话每轮注入索引
         ↓  验收：问「我手头有什么」能列出全部项目

第 3 步  档 1：项目结算推送（steer 到主会话）
         ↓  验收：项目完成时主会话能主动说一句

第 4 步  L2 项目会话（每项目一个 DSH Session）+ 独立压缩
         ↓  验收：两个项目并行互不干扰

第 5 步  档 3：深潜（subagent 读项目会话）
         ↓  验收：问「项目 A 卡在哪」能给出具体进展
```

**顺序理由**：索引是地基。没有 `one_liner` 和 `session_id`，
档 1 推送与档 3 深潜都无处落脚。

## 13. 已决策问题

### 13.1 项目立项方式：混合（已定）

**决策：混合模式。**

| 入口 | `origin` | 初始状态 | 说明 |
| --- | --- | --- | --- |
| 用户显式创建 | `user` | `active` | 说了就建，直接开工 |
| Jarvis 自动识别 | `auto` | `proposed` | 需用户一句话确认 |

自动识别的触发条件（由主 Agent 判断，不写死关键词）：

- 用户表达的事需要**跨多轮或多天**才能完成；
- 或涉及**对外部状态的持续跟踪**（等回复、等结果、持续观察）；
- 单轮可答完的事不立项。

`proposed` 状态的项目**不创建 Session、不占用执行资源**，
只在档 2 索引里占一行，等用户确认后转 `active` 才开始工作。

提案措辞要求：一句话说清「要做什么 + 为什么值得跟踪」，
用户回「嗯」「好」即视为确认。

### 13.2 项目跨对话：可以（已定）

项目是逻辑单元，对话是交互通道。
`Conversation.scene = project:<id>` 即桥梁，同一项目可在手表、
网页、语音多个通道间流转，共享同一个 L2 Session。

### 13.3 项目治理归属：主 Agent 统一管理（已定）

**主 Agent 是所有项目的唯一治理者。**

| 主 Agent 拥有 | 主 Agent 不拥有 |
| --- | --- |
| 立项决策（含自动识别提案） | 项目的具体执行过程 |
| 状态流转的发起 | 项目内部的任务分解细节 |
| 优先级排序与冲突裁决 | 项目会话的原始记录 |
| 向用户汇报的口径 | — |

**关键约束**：项目会话（L2）**不能自行改变项目状态**，
只能通过 `touch_activity` 上报进展；
状态流转必须经主 Agent 决策后调用 `update_status`。

这条约束保证「统一管理」不是口号：状态机只有一个写入者。

### 13.4 主动汇报策略：默认不主动（已定）

默认不主动打断，只在用户发起的自然间隙附带一句，
并且必须接入 `InterruptionManager`（免打扰时段、紧急度阈值）。

例外：`blocked` 状态且阻塞原因需要用户决策时，允许一次主动上报。

## 14. 与 DSH 插件化的关系

本设计的 L2/L3 部分可由 DeepSeek Harness 插件承接。

| 需求 | DSH 对应 | 判断 |
| --- | --- | --- |
| 档 3 深潜 | `dsh-subagent` | 直接可用 |
| 档 2 索引的数据源 | `dsh-session-query-sqlite` | 可参考 |
| 项目任务板（DAG + CAS） | `dsh-experimental-agent-team` | 语义吻合但是实验包 |
| 多项目编排 | 无直接对应 | 需自建 |

**建议**：借用 `agent-team` 的语义模型（roster、task board、CAS），
但不依赖其实现——本项目所需更简单，不需要 teammate 互发消息，
只需主 Agent 单向查询与接收推送。

同时 `ProjectService` 应作为 Python 侧车暴露 HTTP 接口，
供 DSH 插件调用；只有 `/policy/check` 类裁决接口需要同步语义。

## 15. 落地状态

| 项 | 状态 |
| --- | --- |
| `projects` 表基础字段 | 已存在 |
| `projects` 新增 9 字段 + 索引 | **已实现**（`app/db.py`） |
| `ProjectService` | **已实现**（`app/projects.py`） |
| 状态机与 CAS | **已实现** |
| `one_liner` / `session_id` / `context_snapshot` | **已实现** |
| 混合立项（`origin` 决定初始状态） | **已实现** |
| HTTP 接口（列表/创建/流转/索引） | **已实现**（`app/main.py`） |
| 测试覆盖 | **33 项通过**（`tests/test_projects.py`） |
| 档 1 推送 | 未实现 |
| 档 2 索引工具（DSH 侧） | 部分：Python 侧 `project_index_text()` 已就绪，插件未接 |
| 档 3 深潜 | 未实现 |
| 按场景划分对话 | 未实现（现按 `device_id`） |

### 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/projects` | 列表，支持 `?status=` 过滤 |
| `POST` | `/api/projects` | 创建，`origin=user` 直接 active，否则 proposed |
| `PATCH` | `/api/projects/{id}` | 状态流转，支持 `expect_revision` CAS |
| `GET` | `/api/projects/index` | 主 Agent 用的紧凑索引（文本 + 结构化） |

### 未验证事项

- 未做 DSH 插件侧接入，索引文本目前由 Python 直接渲染；
- 未接 `InterruptionManager`，档 1 主动汇报尚未打通；
- 未做真机与生产部署验证。

本文档第 1-9 节大部分已落地；第 3 节的档 1、档 3 仍为设计目标。
实现前不应把未实现项视为已交付能力。
