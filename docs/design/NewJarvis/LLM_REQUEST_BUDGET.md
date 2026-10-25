# 请求体积预算（LLM Request Budget）

- 状态：**已实现**（`app/tool_loop.py`）
- 起因：一次真实失败，不是理论风险

---

## 1. 现象

生产上「今天都干了什么」稳定失败：

```
LLMError: LLM Responses stream failed:
{'code': 'upstream_error', 'message': 'Upstream request failed'}
```

同一句话在别的请求里能成功，说明不是模型或额度问题。

## 2. 定位过程

给每次调用加了一行日志（`llm request ... bytes=`），抓到的序列：

| 轮次 | 请求字节 | 结果 |
| --- | --- | --- |
| 1 | 10,111 | 成功 |
| 2 | 27,067 | 成功 |
| 3 | 41,389 | **失败** |

结论：**该 provider 的 `/v1/responses` 在约 30KB 附近开始失败**（27K 可、41K 不可）。
不是代码 bug，是上游的隐性上限，没有任何文档说明。

体积从哪里来：`event.list_recent(limit=20)` 单次返回 **25KB**。

## 3. 修法（两层）

### 3.1 单条工具结果封顶（`MAX_TOOL_RESULT_CHARS = 6000`）

超长结果被截断并**显式标注**：

```
...[truncated 3009 chars; narrow the query for more]
```

模型因此知道"还可以再问"，而不是以为看到了全部。

### 3.2 整个请求封顶（`MAX_REQUEST_BYTES = 26000`）

每次调用模型前检查总字节数；超了就**从最旧的工具消息开始压缩**，
用户消息与系统提示**永不改写**（改写它们会改变请求含义）。

压缩同样带标注：`...[trimmed to fit the request budget]`。

### 3.3 数据源头收紧

`event.list_recent` 默认 `limit` 从 20 降到 10。

## 4. 验收

- 同一句「今天都干了什么」现在三轮走完（10K → 26K → 21K → 21K）并正常回复。
- `tests/test_request_budget.py`：小对话不动、大对话压到预算内、用户消息不被改写。
- 日志保留每次请求的体积，便于将来换 provider 时重新标定。

## 5. 遗留

- 预算值是按当前 provider 实测标定的，**换 provider 或换模型需要重新测**。
- 更好的做法是压缩语义（摘要旧工具结果）而不是截断字符串；当前选择截断，
  因为它可预测、无额外模型调用。
