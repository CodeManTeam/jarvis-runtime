import asyncio
import base64
import json
import wave
from types import SimpleNamespace

import app.asr as asr_module
from app.asr import RealtimeASRSession
from app.asr import _pack_message, _unpack_message
from app.db import Database
from app.domain import EventSource
from app.service import JarvisService


def test_asr_protocol_frames_round_trip() -> None:
    frame = _pack_message(0x2, 0x1, b"pcm", 7)
    decoded = _unpack_message(frame)

    assert decoded["message_type"] == 0x2
    assert decoded["flags"] == 0x1
    assert decoded["sequence"] == 7
    assert decoded["payload"] == b"pcm"


def test_realtime_session_builds_pcm_wav(monkeypatch) -> None:
    session = RealtimeASRSession(sample_rate=16000, channels=1, sample_width=2)
    session.append(b"\x01\x02" * 160)
    wav = session.wav_bytes()

    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    assert len(wav) > 44


def test_asr_empty_final_result_is_not_an_error(tmp_path, monkeypatch) -> None:
    audio_path = tmp_path / "silence.wav"
    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 1600)

    class EmptyResultSocket:
        def __init__(self):
            self.sent_final = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def send(self, _):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent_final:
                raise StopAsyncIteration
            self.sent_final = True
            payload = json.dumps({"result": {"text": ""}}).encode()
            return _pack_message(0x9, 0x2, payload, sequence=1)

    monkeypatch.setattr(
        asr_module,
        "settings",
        SimpleNamespace(
            asr_provider="volcengine",
            asr_api_key="test-key",
            asr_app_id="test-app",
            asr_access_key="",
            asr_resource_id="test-resource",
            asr_endpoint="wss://example.invalid",
        ),
    )
    monkeypatch.setattr(
        asr_module.websockets,
        "connect",
        lambda *_args, **_kwargs: EmptyResultSocket(),
    )

    assert asyncio.run(asr_module.recognize_file(audio_path)) == ""


def test_audio_end_creates_one_speech_run(tmp_path, monkeypatch) -> None:
    database = Database(str(tmp_path / "speech.db"))
    database.init()
    service = JarvisService(database)
    pushed = []
    handled = []

    async def push(device_id, payload):
        pushed.append((device_id, payload))
        return True

    async def publish(event):
        handled.append(event)
        return {
            "event_id": event.event_id,
            "action": "reply",
            "reply": "收到",
        }

    async def fake_final(self):
        return "提醒我下午三点开会"

    service.set_push_callback(push)
    service.publish_event = publish
    monkeypatch.setattr(RealtimeASRSession, "final", fake_final)

    async def run():
        await service.ingest_audio_message(
            "watch-1",
            {
                "type": "audio_start",
                "session_id": "speech-1",
                "sample_rate": 16000,
                "channels": 1,
                "sample_width": 2,
            },
        )
        await service.ingest_audio_message(
            "watch-1",
            {
                "type": "audio_chunk",
                "session_id": "speech-1",
                "sequence": 0,
                "data": base64.b64encode(b"\x00\x00" * 16000).decode("ascii"),
            },
        )
        return await service.ingest_audio_message(
            "watch-1",
            {"type": "audio_end", "session_id": "speech-1"},
        )

    result = asyncio.run(run())

    assert result["type"] == "speech_final"
    assert result["text"] == "提醒我下午三点开会"
    assert len(handled) == 1
    assert handled[0].event_type.value == "speech"
    assert handled[0].source.value == "microphone"
    # Contract change: audio is opt-in. A voice note still gets a text answer
    # first, and speaking only happens on request or when the event must
    # interrupt. Asserting tts_start here was asserting the old product decision.
    types = [payload["type"] for _, payload in pushed]
    assert types[0] == "agent_event"
    assert "tts_start" not in types


def test_microphone_message_keeps_source_and_skips_confirmation(tmp_path) -> None:
    database = Database(str(tmp_path / "microphone-message.db"))
    database.init()
    service = JarvisService(database)
    captured = []

    async def submit(event):
        captured.append(event)
        return {
            "event_id": event.event_id,
            "action": "ignore",
            "reply": "",
        }

    service.orchestrator.submit = submit

    def unexpected_confirmation(*_args):
        raise AssertionError("ambient microphone speech cannot confirm an action")

    service._handle_natural_language_confirmation = unexpected_confirmation
    asyncio.run(
        service.handle_message(
            "可以",
            device_id="watch-1",
            event_source=EventSource.MICROPHONE,
        )
    )

    assert len(captured) == 1
    assert captured[0].source is EventSource.MICROPHONE
    assert captured[0].event_type.value == "speech"
    assert captured[0].context["input_mode"] == "ambient_microphone"
