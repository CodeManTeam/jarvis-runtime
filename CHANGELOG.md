# 更新记录

## v1.0.0 - 2026-10-24（开发中）

主题：可信运行时。设计基线见 `docs/design/NewJarvis/`。

### 新增

- 权限模型：能力 × 范围 × 有效期四档矩阵（自动 / 确认一次 / 每次确认 / 禁止），
  `permission_grants` 与 `permission_matrix_overrides` 落库；档位修改重启后保留。
- 许可单次消费为原子 `UPDATE ... WHERE consumed_at IS NULL`，重复消费写
  `permission_reuse_blocked` 审计。
- 记忆分层：`layer` / `status` / `confidence` / 有效期 / 修订号 / 原始引用；
  候选记忆不参与检索、不注入上下文；忘记保留行可恢复。
- 全量销毁：要求精确短语 + 已导出 + 10 分钟单次导出令牌，先 kill switch 再清库，
  回执只留计数。
- 活动投影新增 `attention` / `confidence` / `confidence_basis` / `risk` / `evidence` /
  `degradation` / `grant` / `detail.history_meta`；旧字段与旧客户端不受影响。
- 设备会话表与 24h 在线率（重叠会话先合并再求和）、15 分钟连接条、kill switch 接口。
- 中控台重写为深色五页驾驶舱；权限矩阵可点击改档；未接入能力标注"未接入"。
- 手表首页改为状态栏 + 注意力流；新增深色令牌。
- 手表新增「四层依据」与「高风险许可」两屏；高风险卡片必须先看草稿与不可逆声明才能批准。
- 手表新增离线 / 服务降级 / 低电量极暗三态，同一时刻只显示一个状态卡。
- 请求指标：分钟分桶记录（路径模板化）、P50/P95 与错误率；无样本时返回 null 而不是 0。
- 权限闭环：真实 Agent 能力映射到矩阵能力；消息类许可绑定内容哈希；
  用户确认即签发并消费该许可；同一动作不得确认两次。

### 修复

- `edge_audio` 未捕获 aiohttp 握手失败，导致 TTS 不可用会中断整个任务派发；
  现在降级为纯文字并写审计。
- 活动分类兜底把普通回复标为"已忽略"；改为自动完成，`ignored` 需明确证据。
- 中控顶栏、悬浮条与指标卡的"等待你许可"曾取不同数据源而互相矛盾，现统一。
- 审计表曾原样渲染 `detail` 的 JSON，现改为人类可读摘要。
- **测试会写真实数据库**：销毁用例与活动动作用例都从模块级全局取依赖，
  跑一次全量 `pytest` 会清空 `data/jarvis.db` 并写入审计行。两处已改为注入临时库，
  销毁用例在运行前断言库路径位于 `tmp_path` 内。
- 设备延迟/错误率曾把中控的格子写死成"未接入"，数值与备注也曾互相矛盾（有值却写"暂无数据"）。

### 验收

- `python -m pytest -q`：167 passed。
- `watch-client/gradlew.bat assembleDebug`：通过。
- 中控五页在无头 Edge 中渲染无控制台错误；权限矩阵改档可落库。
- **未验证**：真机 360×480 排版、中文字体、触摸热区、功耗。

## 未发布 - 2026-10-03

- 整理 PROJECT_MAP.md 和文档入口，明确唯一维护 Android 客户端为 watch-client。
- 新增鉴权活动列表/详情 API，按事件关联审计、待确认动作、记忆和任务。
- 新增稳定游标分页、来源/状态/设备/项目筛选；原卡显示拒绝和过期状态。
- 保存 event_result 审计，补齐直接编排路径的回复事实；读取不依据意图伪造保存成功。
- 添加事件排序和关联查询索引；没有搬迁或删除现有数据。
- 新增 11 个测试，全套 61 通过。尚未接入 Android/中控台或生产部署。

## v0.4.0 - 2026-10-03（开发中）

### 已完成

- 新增 storage-independent `AgentRun` 状态模型和严格状态迁移。
- 新增结构化 `RunResult` / `RunError` 结果封装。
- 新增 `EventEnvelope` 与现有 `domain.Event` 双向转换。
- 新增 `TimerFired` 定时唤醒事件结构。
- 新增内存 `EventRuntime`：事件幂等接收、AgentRun 关联、处理器唤醒、
  等待恢复和错误归一化。
- 新增运行时状态、事件往返和重复事件测试。

### 未完成

- 尚未替换生产服务的兼容调度推送路径。
- 尚未持久化 AgentRun/Event 或实现重启恢复。
- 尚未将 TTS 回执和 MCP Tool Loop 接入同一个 AgentRun。

## v0.3.0 - 2026-10-03（开发中）

### 进行中

- v0.3.0 可靠任务引擎第一批实现
- 任务依赖字段和依赖完成检查
- 任务领取锁，降低重复调度风险
- 推送失败指数退避
- 失败任务死信时间标记
- 失败或取消任务手动重试接口
- 持久化用户会话和会话过期清理
- 手表/中控台共享会话标识
- 统一外部 Event 接入接口
- Client Runtime 设备状态上报和消息序号去重
- 白名单本地命令：震动和通知
- Personal Mode 设备主动命令下发
- Client Runtime 存储空间和充电状态上报
- Android 前台 Runtime Service
- 设备离线命令队列、重连补发和执行回执
- DeviceAgent 纳入统一 Agent/Policy/审计执行链路
- Android 通知监听器接入 EventBus
- NotificationAgent 能力描述
- Android 日历未来 24 小时同步
- CalendarAgent 能力描述
- Android 位置变化同步
- LocationAgent 能力描述
- 修复手表端主动重连与旧 WebSocket 回调竞争导致的重复连接
- 修复服务端对已关闭 WebSocket 重复发送 close 导致的异常日志
- MCP 风格 Tool Registry 和统一 Tool Runtime
- 多轮 Agent Tool Loop 与工具结果上下文回传
- 内置设备、任务、记忆、位置、日历和事件查询工具
- 工具名称兼容 OpenAI-compatible API 的安全映射
- 定时任务改为 `timer.fired` 事件驱动
- AgentRun 状态和启动/完成/失败审计
- 手表端实时 TTS 播放、分片追加、取消和完成回执
- 修复 API 使用 `Z` 时间戳时定时任务不触发的问题

## v0.2.0 - 2026-10-03

### 新增

- 事件后台 Worker
- 事件内容短窗口去重
- 持久化忽略规则
- 任务暂停和恢复
- 待确认动作存储
- 确认后执行和拒绝清理
- 手表 WebSocket 自动重连
- 手表心跳
- 手表自动注册
- 中控台忽略规则页面

### 验收

- 本地 Python 编译通过
- Android Debug APK 构建通过
- 公网 AI 消息处理通过
- 公网忽略规则通过
- 公网任务暂停/恢复通过
- 公网 WebSocket 连接和心跳通过

## v0.1.0 - 2026-10-02

### 新增

- FastAPI 后端
- DeepSeek AI 接入
- JSON Output 结构化决策
- SQLite 任务、记忆、项目、设备和审计
- ReminderAgent
- 任务定时调度
- WebSocket 提醒推送
- Android 手表客户端
- Jarvis 浏览器中控台
- `jarvis.example.com` 生产部署

