# Realtime Input Events

## Speech

手表通过设备 WebSocket 把录音拆成事件发送到中控：

```json
{"type":"audio_start","session_id":"speech-1","sample_rate":16000,"channels":1,"sample_width":2}
{"type":"audio_chunk","session_id":"speech-1","sequence":0,"data":"<base64 pcm>"}
{"type":"audio_end","session_id":"speech-1"}
```

中控在音频流期间可以推送：

```json
{"type":"speech_partial","session_id":"speech-1","text":"提醒我下午"}
```

partial 结果只用于显示，不会触发 Agent。`audio_end` 后，中控才创建一个
`EventSource.MICROPHONE + EventType.SPEECH` 事件，交给 EventRuntime 和 Agent
决定下一步。最终结果通过 `speech_final` 和普通 `agent_event` 返回。

当前火山引擎适配器使用单向流式 ASR WebSocket：
`wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream`。中控会把 WAV
中的 PCM 音频按二进制协议帧发送，配置帧使用 JSON，音频帧使用正序号，结束帧
使用负序号。鉴权优先使用 `ASR_API_KEY`，也兼容旧的
`ASR_APP_ID` + `ASR_ACCESS_KEY` 配置。

## Timers

提醒不是一条待发送的文本消息。调度器到点后创建稳定事件：

```text
timer task -> timer.fired Event -> AgentRun -> tools/agents -> result -> device
```

Agent 会在唤醒后读取任务内容、设备状态、记忆和其他工具结果，再决定是播报、
继续执行、创建后续任务，还是等待用户。
