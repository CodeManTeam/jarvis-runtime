# Jarvis Runtime Architecture

## 目标与现状

2026-10-03 用户需求更新：[需求基线](docs/REQUIREMENTS_BASELINE.md)、
[目标门控/记忆/行动架构](docs/GATING_MEMORY_ARCHITECTURE.md)。
下文为现有代码结构说明，不代表目标架构已经实现。
目标新增服务端语义门控、God Agent 协调、海马体增量整理、活动版本流及模型降级。
手表仅做阈值切段与采集反馈；个人单服务器优先，不做多实例扩展。
受控远程执行和自我修改是后续需求，必须保留人类审批及独立于模型的策略执行。

Jarvis 的运行时链路现在对应 `structure.py` 的核心设计：

```text
Input / Watch / Audio / Image
        |
        v
Event -> EventBus Worker -> Normalizer -> Dedup/Ignore
        |
        v
ContextBuilder
  |        |        |
Memory  Tasks    User Policy
        |
        v
AIIntentClassifier
        |
        v
PolicyEngine
  |       |       |       |
Reply  Memory  Task   Agent
                  |       |
                Planner  Executor
                    \     /
                    Response
                       |
             Audit + Device Push
```

第二阶段运行时增加：

- `EventBus` 通过后台 Worker 串联 HTTP、音频和设备事件，调用方等待 Future 结果，不在 API 路由内绕过编排器。
- 事件以内容哈希做短窗口去重，用户规则持久化在 `ignore_rules`，命中会记录审计和命中次数。
- 任务状态支持 `waiting_confirmation`、`waiting_user`、`paused`，需要确认的动作先写入 `pending_actions`，确认后才执行。
- 手表端在 WebSocket 断线后自动重连，每 25 秒发送心跳，并在连接时注册设备。

## 模块对应关系

| 结构设计 | 实现 |
| --- | --- |
| `Event` | `app/domain.py` |
| `EventBus` | `app/events.py` |
| `EventNormalizer` | `app/events.py` |
| `RelevanceFilter` | `app/events.py` |
| `MemoryStore` | `app/memory.py` |
| `AgentRegistry` | `app/agents.py` |
| `PolicyEngine` | `app/policy.py` |
| `InterruptionManager` | `app/policy.py` |
| `Planner` | `app/orchestrator.py` |
| `TaskExecutor` | `app/orchestrator.py` |
| `JarvisOrchestrator` | `app/orchestrator.py` |
| HTTP/ASR/WebSocket adapter | `app/main.py` |

## 设计边界

- AI 负责理解输入、提取实体、判断意图、选择 action 和生成回复。
- PolicyEngine 负责权限、确认、自动化等级和风险边界。
- Agent 只暴露声明过的 capability，不执行任意代码。
- 数据库负责任务、记忆、事件和审计的持久化。
- 手表只负责采集输入、展示结果、震动和接收推送。

## 当前 Agent

`ReminderAgent` 已提供：

- `create`
- `complete`
- `cancel`

新增 Agent 应继承 `Agent`，声明 `capabilities`，再注册到 `AgentRegistry`。

