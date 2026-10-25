# 记忆模型：分层、候选、修订与忘记

- 状态：**草案**
- 现状：`memories(id, content, memory_type, project_id, source_event_id, importance, created_at)`，
  检索为 `LIKE` 匹配（[app/memory.py](../../../app/memory.py)）
- 相关界面：[CONSOLE_UI.md](CONSOLE_UI.md) §03、[WATCH_UI.md](WATCH_UI.md) §6

---

## 1. 核心主张

草图用两句话定义了记忆的边界：**"记得有出处"** 与 **"忘记有边界"**。

翻译成工程约束：

1. 任何进入检索的记忆，必须能回溯到 `source_event_id` 或明确的用户确认动作。
2. AI 可以**提议**记忆，但不能**决定**长期记忆——候选与确认分表存放。
3. 忘记是"移出检索"，不是"抹掉历史"；历史活动里对它的引用仍然可打开，标记为已忘记。
4. 删除原始内容与忘记是两件事，前者需要单独确认。

## 2. 四层

| 层 | `layer` | 内容 | 进上下文 | 保留 |
| --- | --- | --- | --- | --- |
| 原始资料 | `raw` | 音频、图片、原文及哈希 | 按需回溯，默认不进 | 按单条许可 |
| 工作记忆 | `working` | 当前对话、短期意图 | 直接进 | 会话结束 + 24h |
| 长期记忆 | `long` | 稳定偏好、习惯、已确认事实 | 选相关部分注入 | 直到忘记 |
| 活动与审计 | — | 发生过什么、版本变更 | 检索后引用 | 90 天（可配） |

短期事件（`short`）是从活动派生的低价值摘要，保留 7 天后按策略清理，**不进入长期**，
除非被每周总结提升为候选。

## 3. 归属

记忆与项目是**多对多**，但每条记忆只有一个"归属标签"用于界面分组：

| 归属 | 含义 | 界面位置 |
| --- | --- | --- |
| `user_project` | 用户创建的项目 | 记忆库 · 项目视图 |
| `user_preference` | 用户确认过的个人偏好 | 记忆库 · 个人偏好 |
| `ai_candidate` | AI 从事件里归纳的候选 | 记忆库 · AI 自建项目（隔离） |

`ai_candidate` 的规则：**不自动激活、不参与检索、不注入上下文**，除非用户在候选卡上确认。

## 4. 表结构

```sql
ALTER TABLE memories ADD COLUMN layer        TEXT NOT NULL DEFAULT 'short';   -- raw|working|short|long
ALTER TABLE memories ADD COLUMN status       TEXT NOT NULL DEFAULT 'confirmed'; -- candidate|confirmed|forgotten
ALTER TABLE memories ADD COLUMN confidence   REAL;              -- 0..1，候选与 AI 归纳必填
ALTER TABLE memories ADD COLUMN valid_from   TEXT;
ALTER TABLE memories ADD COLUMN valid_to     TEXT;
ALTER TABLE memories ADD COLUMN superseded_by TEXT;             -- 被哪条新记忆取代
ALTER TABLE memories ADD COLUMN revision     INTEGER NOT NULL DEFAULT 1;
ALTER TABLE memories ADD COLUMN raw_ref      TEXT;              -- 原始资料引用（音频/图片/原文）
ALTER TABLE memories ADD COLUMN tags         TEXT NOT NULL DEFAULT '[]';
ALTER TABLE memories ADD COLUMN confirmed_by TEXT;              -- user | auto
ALTER TABLE memories ADD COLUMN confirmed_at TEXT;
```

检索索引：`(status, layer, project_id, importance DESC, created_at DESC)`。

## 5. 冲突与取代

相反证据**先标记冲突，不无声覆盖**：

```text
新记忆与既有长期记忆冲突
  → 新记忆 status=candidate, supersedes=<旧记忆 ID>
  → 旧记忆保持 confirmed，标 conflict=1
  → 界面出现"待裁决"卡：保留旧的 / 采用新的 / 两条都留
```

裁决后：旧记忆 `superseded_by` 指向新记忆，`valid_to` 置为裁决时间；新记忆 `status=confirmed`。
**两条记录都保留**，历史活动引用旧记忆时不至于断链。

一次行为不自动升级为长期偏好。提升条件（任一）：

- 同类事件出现 ≥3 次且无相反证据；
- 用户显式确认；
- 周总结提出并被用户接受。

## 6. 候选生命周期

```text
事件/行为 ──▶ C-006 候选（candidate, confidence=0.81, 未固化）
                │
     ┌──────────┼───────────┐
     ▼          ▼           ▼
  仅今天应用   确认长期     忽略并留痕
  （不改候选）  （→confirmed）  （status=forgotten, 冷却 30 天）
```

"仅今天应用"**不写记忆**，只产生一条当天的覆盖规则。这是草图"不自动固化"的落地方式。

## 7. 忘记 / 删除 / 销毁

三者必须区分，UI 与接口都不允许混用同一个动词。

| 操作 | 效果 | 可恢复 | 需要什么 |
| --- | --- | --- | --- |
| **忘记** | `status=forgotten`，移出检索；引用处显示"已忘记" | 可撤销 | 一次确认 |
| **删除原文** | 删除 `raw_ref` 指向的原始资料，保留摘要 | 不可 | 单独确认 + 说明影响 |
| **销毁全部** | 停止 Agent、撤销未用授权、清除记忆/事件/原始输入/配置 | **不可** | 二次确认识别 + 导出勾选 |

**已送达的外部消息不受任何本地删除影响**，文案必须写明，不允许暗示"删了就没了"。

销毁的实现顺序（保证可回放）：

```text
1. 置全局 kill switch，停止调度
2. 撤销全部未消费 grant
3. 导出校验：要求用户先点"导出需要保留的数据"
4. 精确匹配输入「销毁全部」
5. 逻辑删除 → 物理删除主库 → 备份标记 7 天后到期删除
6. 保留不含内容的操作回执（grant ID、动作、时间、数量），用于事后核查
```

## 8. 保留策略

| 数据 | 默认 | 可配 | 清理方式 |
| --- | --- | --- | --- |
| 短期事件摘要 | 7 天 | 是 | 每日定时任务 |
| 长期记忆 | 永久 | — | 用户手动 |
| 原始录音 | 按条许可 | 是 | 到期自动删，摘要保留 |
| 审计记录 | 90 天 | 是 | 归档后删 |

清理必须先**生成清单**（理由、引用关系、影响、恢复窗口）再执行，草图中已表达为
"保留 7 天后按策略清理"，实现上对应一条可查看的清理计划。

## 9. 检索

v1.0 仍不做向量检索，但要把接口留出来：

```python
class MemoryStore:
    async def search(self, query, *, layer=None, status="confirmed",
                     project_id=None, tags=None, limit=20) -> list[MemoryItem]: ...
```

规则：

- 默认只搜 `status='confirmed'`；
- `ai_candidate` 除非显式 `include_candidates=True`，否则永不参与；
- `forgotten` 除非 `include_forgotten=True`（仅界面用），否则永不参与。

## 10. API

```text
GET    /api/memories?layer=&status=&project=&tag=&q=
POST   /api/memories                        手动写入（立即 confirmed）
PUT    /api/memories/{id}                   修订（revision+1，旧值进 audit）
POST   /api/memories/{id}/forget
POST   /api/memories/{id}/restore
DELETE /api/memories/{id}/raw               删除原始资料（单独确认）
GET    /api/memories/{id}/evidence          来源与引用链
POST   /api/memories/{id}/apply-today       仅今天应用（不写记忆）
GET    /api/memories/candidates             候选列表
POST   /api/memories/candidates/{id}/accept|ignore
POST   /api/memories/export                 导出（JSON/Markdown）
POST   /api/data/destroy                    销毁（二次确认识别）
```

## 11. 迁移

旧 `memory_type` 映射建议（**待确认**）：

| 旧值 | 新 `layer` | 新 `status` |
| --- | --- | --- |
| `episodic` | `short` | `confirmed` |
| `fact` | `long` | `confirmed` |
| `preference` | `long` | `confirmed` |
| 其它 | `short` | `confirmed` |

迁移脚本必须可重入，且迁移前先备份 `memories` 表到 `data/backup/`。

## 12. 测试点

| 测试 | 断言 |
| --- | --- |
| 忘记后检索 | 默认搜不到，`include_forgotten` 能搜到 |
| 候选不注入上下文 | 构造 prompt 断言不含候选文本 |
| 冲突不覆盖 | 新旧两条都还在，旧的 `superseded_by` 正确 |
| 修订保留旧值 | `audit_log` 有旧内容 |
| 销毁顺序 | kill switch 先于删除；grant 全部 revoked |
| 销毁后回执 | 不含任何原文，只含计数与 ID |
