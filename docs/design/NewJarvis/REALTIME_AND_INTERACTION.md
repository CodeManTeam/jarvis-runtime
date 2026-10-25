# 实时流与交互重做

- 状态：**设计**，两份实现（后端 SSE / 中控 SSE+折叠）
- 起因：中控与手机都在**轮询**（5 秒一次），用户点开任务看不到进展，感觉"不是实时的"

---

## 1. 现状与问题

| 位置 | 现在 | 症状 |
| --- | --- | --- |
| 中控长任务 | `setInterval` 5s 拉一次 | 新建后要等最多 5 秒才出现；步骤跳变 |
| 手表 | WebSocket 只收推送，活动靠 HTTP 轮询 | 同款延迟 |
| 语音 | 说完就等 TTS | 用户对"即时回复"过度敏感，反而焦虑 |

**根因**：服务端没有问题，问题是**没有一条服务端 → 客户端的持续通道**给控制面用。

## 2. 事件总线与 SSE

WebSocket 已经给手表用了（`/ws/device`，需要设备身份）。控制面用 **SSE** 更简单：单向、可断线重连、浏览器原生 `EventSource`。

### 2.1 服务端

```
app/eventbus.py
    class EventBus:
        subscribe() -> asyncio.Queue[str]      # 每个订阅者一个队列
        publish(event: dict)                    # 扇出，慢订阅者丢弃而非阻塞
        unsubscribe(queue)
```

- 单进程内存总线（我们本来就是单进程，不做跨机）；
- 每个订阅者有界队列（容量 200），满了**丢最旧**并计入 `dropped`，绝不让推送拖死业务；
- `JarvisService.audit` 与 job 推进处调用 `bus.publish(...)`。

### 2.2 端点

```
GET /api/stream?token=<DEVICE_TOKEN>      (text/event-stream)
```

事件格式统一：

```
event: job_progress
data: {"job_id":"...","seq":3,"kind":"observation","detail":"...","status":"ok","ts":"..."}
```

四类事件：

| event | 何时 | 关键字段 |
| --- | --- | --- |
| `job_status` | 创建/批准/完成/失败/取消 | `job_id, status, steps, max_steps, error` |
| `job_progress` | 每步 | `job_id, seq, kind, detail, status` |
| `activity` | 新活动产生 | `event_id, attention, title, reply` |
| `asr_partial` | 手表识别中继 | `event_id, text, final` |

心跳：每 15 秒发一行注释 `: ping`，防止代理掐断。

### 2.3 订阅鉴权

`EventSource` 不能带自定义头，所以 token 走查询参数；端点校验失败返回 401 并**立即关闭**。日志里不记录完整 token。

## 3. 中控

- 页面加载后建立一条 `EventSource`；`onmessage` 按 event 分发到 state；
- **保留轮询作为兜底**：SSE 断开（`onerror`）时退化为 15 秒轮询，并在顶部显示"实时连接已断开，正在重试"；
- 新建 job：先本地插入一条 `queued` 占位（乐观更新），随后由 `job_status` 校正；
- 步骤追加：`job_progress` 直接 `push` 到该 job 的步骤数组并重渲染**仅该卡片**，不整页重绘。

## 4. 折叠分区（accordion）

每个页面的模块改成可折叠 `section`：

```
[▸ 长任务            3 个运行中]     ← 收起
[▾ 最近交付物        8 项]           ← 展开
```

规则：

- 每页**第一个分区默认展开**，其余收起；
- 折叠状态存 `sessionStorage`（`console.sections`），刷新后保持；
- 标题右侧显示**计数徽标**，收起时也能看出里面有多少东西；
- 展开/收起用已有过渡，不引入新动画库；
- 长任务、交付物、材料、轨迹这些"可能很长"的分区**默认收起**，避免一屏塞满。

## 5. 弱化即时 TTS（手表）

现状：说完 → 等 TTS → 说话。用户对秒回敏感，反而焦虑。

改成**文本优先**：

```
说完 → 立刻显示 ASR 结果（带时间轴）→ 可重放 → 结果到达后追加文本
```

- ASR 服务返回带时间轴的片段（`sentences[].start_ms/end_ms`），手表保存；
- 录制结束先渲染**完整转写**（不是一行闪现），保留在屏幕上；
- 提供「重放」按钮：用保存的时间轴重放 TTS（可选，不自动播）；
- 默认**不自动 TTS**，除非该条事件被判定为"必须打断"（沿用注意力分级）；
- 结果到达后**追加**在当前转写下方，不覆盖。

> 这一条改的是产品气质：从"语音助手"变成"能读能听的笔记本"。用户没说错——即时语音让人焦虑。

## 6. 验收

1. `pytest` 全绿，新增 `tests/test_eventbus.py`、`tests/test_stream.py`。
2. 生产：`curl -N /api/stream` 能收到 `job_status` 与 `job_progress`，心跳存在。
3. 中控：新建 job 后**首帧 < 2 秒**出现（浏览器实测，用 `performance.now()` 记录）。
4. 折叠：刷新后状态保持；收起时计数可见。
5. 手表：录制结束后转写常驻、可重放、默认不自动播。

## 7. 不做

- 不做跨进程/跨机总线（单进程够用；将来要就换 Redis）。
- 不做 SSE 之外的第二套通道（手表继续用 WebSocket）。
- 不做语音流的实时字幕（那是 ASR 中继，已有 `speech_partial`）。
