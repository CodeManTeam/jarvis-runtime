# 设备与模型运维

- 状态：**草案**（指标与路由为待实现；自迭代 / 远程执行 / 升级为**仅设计**）
- 相关界面：[CONSOLE_UI.md](CONSOLE_UI.md) §05
- 现有基础：`devices(battery_level, network_type, app_version, storage_free_mb, charging, tts_*)`、
  `device_commands(pending→delivered→acknowledged/failed/expired)`、`usage_daily`

---

## 1. 原则

1. **指标先有口径，再上界面。** 每个数字都必须能回答"分子分母是什么、统计窗口多长"。
2. **失败必须可见。** 已恢复的失败也保留，不能因为最后成功就把失败抹掉。
3. **对设备的写操作先许可。** 配置、升级、回滚、执行，全部走 [PERMISSION_MODEL.md](PERMISSION_MODEL.md) 的二次确认。
4. **未接入的能力，界面默认折叠并显式标注"未接入"。** 不允许出现"看起来能用"的按钮。

## 2. 指标口径

| 指标 | 定义 | 数据来源 |
| --- | --- | --- |
| 24h 在线率 | `在线秒数 / 86400`，在线 = WebSocket 连接存在且有心跳 | 新增 `device_sessions(started_at, ended_at)` |
| 离线累计 | `86400 - 在线秒数`，UI 显示为分钟 | 同上 |
| 请求延迟 P50 / P95 | 服务端处理时延（收到请求 → 返回），不含网络；按 5 分钟分桶 | `request_metrics(bucket, p50, p95, count)` |
| 请求错误率 | `5xx + 超时 / 总请求`，24h 滑动窗口；单独标"含已恢复失败" | 同上 |
| 今日 Token | `usage_daily.total_tokens`，附输入/输出拆分 | 已有 |
| 平均电流 | 设备端 `BatteryManager` 采样的移动平均，按小时上报 | 新增心跳字段 `current_ma` |

**明确不做**：不把"命令已入队"算作送达，不把"已推送"算作执行完成，不估算未采集的指标。
缺数据时显示 `—`，不显示 0。

## 3. 设备遥测扩展

```sql
ALTER TABLE devices ADD COLUMN model          TEXT;      -- 展讯 8521
ALTER TABLE devices ADD COLUMN battery_mah    INTEGER;   -- 1380
ALTER TABLE devices ADD COLUMN current_ma     REAL;      -- 平均电流
ALTER TABLE devices ADD COLUMN cache_limit    INTEGER;   -- 滚动缓存条数
ALTER TABLE devices ADD COLUMN input_caps     TEXT;      -- 仅触屏/无按键/有扬声器
ALTER TABLE devices ADD COLUMN sensing_policy TEXT;      -- ≤15% 关闭持续感知
```

心跳上报增加：`current_ma`、`cache_size`、`queue_size`（待补传条数）、`sensing_state`。

近 15 分钟连接条：按分钟聚合 `device_sessions`，每格 `online | degraded | offline`。
**degraded 的定义**：有连接但 60 秒内无成功往返。

## 4. 模型与语音路由

> **实现状态**：**模型**路由与降级已实现（`app/routing.py`，
> `GET /api/routes`，中控已接入）。ASR / TTS 的备用路由仍是设计，未实现。
> 未配置 `FALLBACK_MODEL` 时不会发生降级——这是刻意的：宁可不降级，也不假装有备份。

### 4.1 能力声明

每个端点声明能力，路由只按能力匹配，不按模型名硬编码：

```jsonc
{"id": "J-Pro",  "capabilities": ["plan", "tool_call", "vision", "complex_attachment"],
 "limits": {"timeout_ms": 8000, "max_tokens": 8192}}
{"id": "J-Lite", "capabilities": ["plan", "tool_call"],
 "limits": {"timeout_ms": 4000, "max_tokens": 2048}}
```

### 4.2 降级矩阵

| 阶段 | 主 | 备 | 降级后**必须**失去的能力 |
| --- | --- | --- | --- |
| 规划/工具 | J-Pro | J-Lite | 复杂附件判断 → 转人工 |
| ASR | ASR-A | ASR-B | 两路都失败 → 不提交，提供文本 |
| TTS | Voice-A | 文字 | 不播报，屏上文字替代 |
| 工具 | 原调用 | 同路重试 | 只对**幂等**操作重试 |

**硬约束**：降级只能收窄能力，不能放宽权限。降级后的模型无法获得 `per_action` 之外的许可，
也不允许它自己发起 `message.send`。

### 4.3 探活与恢复

```text
连续失败 2 次 → 切备用（写 degradation，UI 显示）
备用连续成功 N 次 → 仍不自动切回
主路由连续 3 次探活成功 → 恢复用于**新任务**
当前任务不重跑已成功的工具调用（幂等键保护）
```

### 4.4 超时与首响应

语音首响应目标 ≤5 秒，但**必须标注"目标/非实测"**。
实测前不得在产品文案、发布说明中出现"5 秒内响应"这类承诺。

## 5. 远程配置

```jsonc
{
  "id": "CFG-009",
  "device_id": "watch-a",
  "changes": [{"key": "cache_limit", "from": 16, "to": 32}],
  "risk": "high",
  "requires": ["diff_review", "rollback_plan", "second_confirm"],
  "ttl_seconds": 120,
  "single_use": true,
  "rollback": {"strategy": "restore_snapshot", "snapshot": "v1.8.2"}
}
```

流程见 [PERMISSION_MODEL.md](PERMISSION_MODEL.md) §7。额外约束：

- 只允许**白名单键**（`cache_limit`、`sensing_policy`、`sync_interval` 等），不接受任意键。
- 变更必须带 `from/to`，界面显示 diff，不许只显示目标值。
- 下发失败 → 自动回滚旧配置 → 在线后校验回执 → 三者都写审计。

## 6. APK 升级与回滚

**仅设计，未接入。**

| 步 | 内容 |
| --- | --- |
| 1 | 上传制品，校验签名与哈希，登记版本元数据 |
| 2 | 设备侧检查：充电中 + 联网 + 电量 >50% 才允许申请 |
| 3 | 二次确认（同远程配置），许可 TTL 10 分钟 |
| 4 | 下载 → 校验 → 安装 → 健康检查（自报版本 + 心跳正常） |
| 5 | 失败 → 自动回滚到 `known_good`，保留日志 |

**代码回滚不等于撤销外部效果**：已外发的消息、已支付的订单不受回滚影响，文案必须写清。

## 7. 自迭代（SI-006）

**仅设计，未接入。** 界面必须默认折叠并标注"未接入"。

```text
候选差异 → 沙箱跑测试 → 展示 diff + 测试结果 + 回退方案
        → 勾选"我已核对代码差异、测试与回滚方案"
        → 确认部署一次（per_action，single_use）
```

硬约束：

- 自迭代**只影响它声明的那一项行为**（如"通知摘要去重窗口 30→60 秒"），不允许改权限、改门控、改白名单。
- 沙箱必须与生产隔离，且不能访问真实用户数据。
- 部署前必须有可回退的快照；部署失败自动回退。

## 8. 远程代码执行（RCE）

**仅设计，默认禁止。** 当前实现中的 `sh -c` 路径按 [PROBLEM_REPORT_2026-10](../../PROBLEM_REPORT_2026-10.md) 的
结论应当移除，v1.0 不引入替代的任意命令通道。

若未来引入，最低门槛：

```text
任意命令接口：关闭
白名单诊断命令：仅 5 条以内，逐条声明参数与副作用
启用条件：独立授权 + 二次确认 + 命令级审阅 + 单次许可
不可由：自迭代、受信 Agent、模型降级、网络恢复 解锁
```

## 9. 紧急停止

中控顶部的 `[紧急停止全部执行]` 是全局 kill switch：

```text
1. 置 kill_switch，编排器停止领取新任务
2. 取消全部未开始的待发动作
3. 清空远程命令队列（pending/delivered 未 ack 的标记 expired）
4. 撤销未消费 grant
5. 保留全部审计，不删除任何记录
```

**它不撤销已经发生的效果**（已发送的消息、已执行的设备命令），文案必须写明。

## 10. 审计事件

`config_change_requested` · `config_change_approved` · `config_change_applied` ·
`config_change_rolled_back` · `apk_upgrade_requested` · `apk_upgrade_failed` ·
`self_iterate_candidate` · `self_iterate_deployed` · `rce_denied` · `rce_approved` ·
`kill_switch_on` / `kill_switch_off` · `route_degraded` / `route_recovered`

## 11. API

```text
GET  /api/devices/{id}/metrics?window=24h
GET  /api/devices/{id}/sessions?range=15m
GET  /api/devices/{id}/commands
POST /api/devices/{id}/config             申请变更（返回带 diff 的申请单）
POST /api/devices/{id}/config/{req}/confirm
POST /api/devices/{id}/upgrade            仅设计
POST /api/runtime/kill-switch             {"on": true}
GET  /api/routes                          当前路由与探活状态
```

## 12. 测试点

| 测试 | 断言 |
| --- | --- |
| 在线率口径 | 构造 12h 在线 + 12h 离线，得 50% |
| 缺数据显示 | 无电流数据时返回 `null`，序列化为 `—`，不是 0 |
| 降级不放权 | 降级到 J-Lite 后请求 `message.send` 仍被拒 |
| 配置必带 diff | 缺少 `from` 的变更请求被 400 |
| 配置自动回滚 | 下发失败后配置回到旧值，且写 rolled_back |
| kill switch | 停止后新任务不被领取，队列清空，审计保留 |
| 未接入能力 | `/api/devices/{id}/upgrade` 返回 501 且写明原因 |
