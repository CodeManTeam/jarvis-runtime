# Watch Realtime WebSocket Events

手表端通过已有设备 WebSocket 接收实时事件。所有 TTS 事件都使用同一个
`run_id`，手表按顺序排队播放，不阻塞 WebSocket 或 Android UI 线程。

## TTS 播放

开始一段播报。服务器会先发送这个事件，再按短句发送音频分片：

```json
{
  "type": "tts_start",
  "run_id": "run-123",
  "text": "",
  "audio_format": "audio/mpeg",
  "audio_provider": "edge"
}
```

追加一个服务器生成的 MP3 分片。手表写入缓存文件后用 `MediaPlayer`
顺序播放，播放完成后立即删除临时文件：

```json
{
  "type": "tts_chunk",
  "run_id": "run-123",
  "sequence": 0,
  "text": "今天下午三点有一个会议。",
  "audio_base64": "<base64 encoded audio/mpeg>"
}
```

`audio_base64` 不可用时，手表会退回本地 Android TTS；这只用于兼容
Edge TTS 暂时不可用的情况。结束输入后，手表会等所有音频分片播放完成：

```json
{
  "type": "tts_end",
  "run_id": "run-123"
}
```

完成回执：

```json
{
  "type": "tts_completed",
  "run_id": "run-123",
  "request_id": "tts-..."
}
```

取消当前播报：

```json
{
  "type": "tts_cancel",
  "run_id": "run-123"
}
```

手表停止播放并回执 `tts_canceled`。音频解码、播放或 WebSocket 处理失败时回执
`tts_error`，其中 `error` 字段包含错误原因。服务器端使用
`TTS_PROVIDER=edge`、`TTS_VOICE=zh-CN-XiaoxiaoNeural` 和 `TTS_RATE=+0%`。

## Agent 事件

```json
{
  "type": "agent_event",
  "event": "reminder.started",
  "text": "提醒任务已经开始执行。",
  "requires_attention": true
}
```

手表会显示事件文本；`requires_attention` 或 `notify` 为 `true` 时会震动。

## 保留的旧事件

- `task_due`：显示提醒并震动。
- `command`：继续执行 `vibrate`、`show_notification`、`open_url` 和插件命令。
- `command_ack`：本地命令执行后仍按原协议回执。

