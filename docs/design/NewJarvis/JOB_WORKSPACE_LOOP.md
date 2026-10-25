# Job / Workspace / 长循环（P0）

- 状态：**设计**，实现派给 Codex
- 目标：让"做一件需要很多步的事"成为可能，且**零新依赖**
- 已被研究验证的形状：mini-swe-agent 的核心循环 172 行 + 本地执行 80 行（MIT），
  证明「消息 + 一个 shell 工具 + 循环 + 轨迹落盘」就够了

---

## 1. 为什么是这三样

一次工具调用答不了"给我做一个终端"。中间差的不是能力数量，是：

| 缺的东西 | 后果 |
| --- | --- |
| 工作区 | 文件不能跨步累积，写一半没法改 |
| 长循环 | 第 8 步就断 |
| 轨迹落盘 | 进程重启一切归零，也无法审计 |

## 2. 数据模型

```sql
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued | running | done | failed | cancelled
    workspace TEXT NOT NULL,       -- data/jobs/<id>/workspace
    steps INTEGER NOT NULL DEFAULT 0,
    max_steps INTEGER NOT NULL DEFAULT 60,
    error TEXT,
    result TEXT,                   -- 最终摘要（JSON）
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE job_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- think | action | observation | note
    tool TEXT,
    detail TEXT,                   -- 命令 / 输出摘要（截断）
    status TEXT NOT NULL,          -- ok | error
    created_at TEXT NOT NULL
);
```

轨迹（完整 messages）落盘：`data/jobs/<id>/trajectory.json`，**每步覆盖写**，
所以恢复只需要重新加载它。

## 3. 循环

```python
async def run_job(job, emit):
    messages = load_trajectory(job) or [system_prompt, user_goal]
    while job.steps < job.max_steps and job.status == "running":
        messages = shrink(messages)              # 复用 tool_loop 的预算逻辑
        reply = await complete_with_tools(messages, [shell_tool])
        if reply.stop: break
        for action in reply.actions:
            output = await workspace_shell(job, action.command)
            messages.append(observation(output))
            save_trajectory(job, messages)
            emit_progress(job)                   # 手表 / 终端订阅
```

要点：

- **一个工具**：`workspace.shell`。不做一堆细粒度文件工具——mini-swe-agent 的经验是
  `cat`/`sed`/`python` 就够，工具越少模型越不容易乱选。
- **每步落盘**：崩溃后可恢复，且审计能复原每一步。
- **失败即失败**：连续 3 次同一命令报错或超时，标记 `failed` 并说明原因；
  不得把没验证的产物说成"已完成"。

## 4. 工作区与安全

```
data/jobs/<id>/workspace/     ← 唯一可写区域
```

- `workspace.shell` 命令在 workspace 内以 `cwd=workspace` 执行；
- 路径逃逸检测：命令里出现 `..`、绝对路径指向工作区外、`sudo`、`rm -rf /` 一律拒绝执行；
- 权限：能力 `job.run_shell` 默认 **per_action**（与 `code.run` 一致），可在中控下调；
- 超时 60 秒，输出截断 4000 字符（复用 `MAX_TOOL_RESULT_CHARS` 的一半）。

## 5. 接口

```
POST /api/jobs          {goal, max_steps?}         → {id, status}
GET  /api/jobs                                     → 最近 N 个 + 状态
GET  /api/jobs/{id}                                → job + 步骤 + 产物
POST /api/jobs/{id}/cancel                         → 标记 cancelled
WS   job_progress       {job_id, seq, kind, detail} → 已有推送通道复用
```

执行方式：**进程内 asyncio 任务**（单用户、单进程足够）。恢复：启动时把
`running` 状态的 job 重新放回队列——这是"持久"的最小实现，零依赖。

## 6. 验收

1. `pytest` 全绿（新旧都过）；新增 `tests/test_jobs.py`。
2. 生产上创建 job「在工作区里建一个能跑的静态页面并起服务」，最终工作区里真的有
   `index.html`，且 job 状态 `done` 并附产物路径。
3. 失败场景：让模型执行一条必然失败的命令，job 必须是 `failed` + 原因，**不得** `done`。
4. 内存增量 < 50MB（同进程，应当接近 0）。
5. 轨迹文件每步更新，重启后能读到完整历史。

## 7. 不做

- 不做多进程 worker / 分布式队列（单用户不需要；将来要就换 huey，它零依赖且支持 SQLite）。
- 不接 ACP / codex-acp（Node + ~300MB 内存，这台机器负担不起；接口留好将来可换）。
- 不做子 Agent 并行。
