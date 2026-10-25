# P1：文件工具、上下文压缩、取消

- 状态：**设计**，实现派给 Codex
- 前置：P0 已上线（Job + Workspace + 长循环，零新依赖）
- 沙箱：**本轮不做**，等换服务器后单独加（原因见 `JOB_WORKSPACE_LOOP.md` 与提交记录：
  这台 1.9G 机器上 `docker ps` 会掐断 SSH，`unshare -r`/`bwrap` 不可用）

---

## 1. 文件工具（三个，不多加）

mini-swe-agent 证明了一个 shell 工具就够，但实测下来模型用 `cat > file <<'EOF'` 拼多行文件
又费 token 又容易写坏。补三个最小工具，命名与 shell 工具一致使用下划线：

| 工具 | 参数 | 行为 | 拒绝条件 |
| --- | --- | --- | --- |
| `workspace_write` | `path`, `content`, `overwrite?` | 写文件，自动建父目录 | 路径逃出工作区；已存在且未设 overwrite |
| `workspace_read` | `path`, `max_chars?` | 读文件，默认截断 8000 字符并标注 | 逃出工作区；不存在 |
| `workspace_list` | `path?="."`, `depth?=1` | 列出目录树（文件/目录 + 字节数） | 逃出工作区 |

**路径规则**：只接受相对路径；`..`、绝对路径、`~` 一律拒绝——与 shell 守卫同一套判断，
复用 `validate_path()`，避免两套逻辑漂移。

## 2. 上下文压缩

现状：`_shrink()` 只把过长的工具结果**截断**。长任务里消息会一直增长，
截断不解决总量。

### 关键约束

provider 要求 **assistant(tool_calls) 与 tool(tool_call_id) 严格配对**
（P0 实测：不配对直接 400）。所以压缩**必须成对删除**，不能只删 tool 消息。

### 策略

```
当 messages 估算字节 > COMPACT_AT (18000)：
  1. 保留    system 消息 + 原始目标（第 2 条）
  2. 保留    最近 KEEP_RECENT = 6 条消息（成对边界对齐）
  3. 中间部分折叠成一条摘要消息：
       {"role":"user","content":"[早前步骤摘要] 共 N 步；执行的命令：cmd1 / cmd2 …；关键输出：…"}
     摘要由本地规则生成（提取每条 tool_call 的命令与结果首行），**不额外调用模型**
  4. 写审计 compacted，记录折叠的消息数与字节数
```

不做模型摘要，理由：多一次调用 = 多一次失败面与延迟；命令列表本身就足够模型继续。

### 边界

- 折叠后若仍超预算，再对剩余 tool 消息做 `_shrink` 截断。
- 折叠永远不动最后一条 assistant/tool 对。

## 3. 取消

`POST /api/jobs/{id}/cancel` 已存在，但未经端到端验证。要求：

- 置 `cancelled` 后，循环在下一次迭代前退出（循环条件已检查 status）。
- 正在执行的命令**不强杀**（60 秒超时兜底），但不再发起新命令。
- 取消后写审计 `job_cancelled`。

## 4. 验收

1. `pytest` 全绿，新增 `tests/test_workspace_files.py`、`tests/test_compaction.py`。
2. 压缩测试必须断言：折叠后**每个 tool 消息都能找到对应的 assistant tool_calls**（配对完整）。
3. 生产跑一个 ≥12 步的真实 job（多文件站点：index.html + style.css + 一个脚本 + 起服务验证），
   成功且工作区文件齐全。
4. 该次运行中压缩被触发（审计有 `compacted`）。
5. 取消：对一个 running 的 job 调 cancel，状态变 `cancelled` 且不再新增步骤。

## 5. 不做

- 不做沙箱（等换机器）。
- 不做模型摘要式压缩。
- 不做并行子任务。
