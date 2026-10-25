from __future__ import annotations

import io
import json
import os
import tempfile
import uuid
import wave
import gzip
from pathlib import Path
from typing import Any

import websockets

from .config import settings


class ASRError(RuntimeError):
    pass


class RealtimeASRSession:
    """Accumulates watch PCM frames and exposes partial/final transcription.

    The transport is realtime even when the configured provider only exposes a
    file-style endpoint.  A native provider websocket can replace ``transcribe``
    later without changing the device or event contracts.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        sample_width: int = 2,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self._pcm = bytearray()
        self._last_partial = ""

    @property
    def pcm_bytes(self) -> int:
        return len(self._pcm)

    def append(self, data: bytes) -> int:
        self._pcm.extend(data)
        return len(self._pcm)

    def wav_bytes(self) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(self.channels)
            wav.setsampwidth(self.sample_width)
            wav.setframerate(self.sample_rate)
            wav.writeframes(bytes(self._pcm))
        return output.getvalue()

    @property
    def utterances(self) -> list[dict[str, Any]]:
        """Time-stamped segments of the last transcription, if the server sent them."""
        return getattr(self, "last_utterances", [])

    async def transcribe(self) -> str:
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp:
                temp_path = temp.name
                temp.write(self.wav_bytes())
                temp.flush()
            collected: list[dict[str, Any]] = []
            text = await recognize_file(temp_path, collected)
            self.last_utterances = collected
            return text
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    async def partial(self) -> str:
        text = (await self.transcribe()).strip()
        if text:
            self._last_partial = text
        return text

    async def final(self) -> str:
        text = (await self.transcribe()).strip()
        return text or self._last_partial


async def recognize_file(path: str | Path, collected: list[dict[str, Any]] | None = None) -> str:
    if settings.asr_provider == "none":
        raise ASRError("ASR_PROVIDER is not configured")
    if not settings.asr_api_key and (
        not settings.asr_app_id or not settings.asr_access_key
    ):
        raise ASRError(
            "ASR_API_KEY is missing, or legacy ASR_APP_ID/ASR_ACCESS_KEY are incomplete"
        )

    with wave.open(str(path), "rb") as wav:
        pcm = wav.readframes(wav.getnframes())
        sample_rate = wav.getframerate()
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()

    headers = {
        "X-Api-Resource-Id": settings.asr_resource_id,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    if settings.asr_api_key:
        headers["X-Api-Key"] = settings.asr_api_key
    else:
        headers["X-Api-App-Key"] = settings.asr_app_id
        headers["X-Api-Access-Key"] = settings.asr_access_key
    request: dict[str, Any] = {
        "user": {"uid": settings.asr_app_id},
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": sample_rate,
            "bits": sample_width * 8,
            "channel": channels,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
        },
    }
    try:
        async with websockets.connect(
            settings.asr_endpoint,
            additional_headers=headers,
            open_timeout=15,
            close_timeout=5,
            max_size=16 * 1024 * 1024,
        ) as socket:
            await socket.send(
                _pack_message(
                    0x1,
                    0x1,
                    json.dumps(request, ensure_ascii=False).encode("utf-8"),
                    sequence=1,
                )
            )

            sequence = 2
            for offset in range(0, len(pcm), 3200):
                chunk = pcm[offset:offset + 3200]
                await socket.send(
                    _pack_message(0x2, 0x1, chunk, sequence)
                )
                sequence += 1
            await socket.send(
                _pack_message(0x2, 0x3, b"", -sequence)
            )

            text = ""
            utterances: list[dict[str, Any]] = []
            async for message in socket:
                if not isinstance(message, bytes):
                    continue
                response = _unpack_message(message)
                if response["message_type"] == 0x0F:
                    raise ASRError(_server_error(response["payload"]))
                if response["message_type"] != 0x09:
                    continue
                payload = response["payload"]
                if isinstance(payload, dict):
                    result = payload.get("result") or {}
                    text = str(result.get("text") or payload.get("text") or text)
                    # The provider returns a time axis when show_utterances is on.
                    # Keeping it is what lets the watch replay a transcript later
                    # instead of racing to speak the moment the user stops.
                    for item in result.get("utterances") or []:
                        if not isinstance(item, dict):
                            continue
                        rendered = str(item.get("text") or "").strip()
                        if not rendered:
                            continue
                        utterances.append({
                            "text": rendered,
                            "start_ms": item.get("start_time"),
                            "end_ms": item.get("end_time"),
                        })
                if response["flags"] & 0x2:
                    break
            if collected is not None:
                collected.extend(utterances)
            return text
    except ASRError:
        raise
    except Exception as exc:
        raise ASRError(f"ASR WebSocket request failed: {exc}") from exc


def _pack_message(
    message_type: int,
    flags: int,
    payload: bytes,
    sequence: int | None = None,
    serialization: int = 1,
    compression: int = 1,
) -> bytes:
    encoded = gzip.compress(payload) if compression == 1 else payload
    header = bytes(
        (0x11, (message_type << 4) | flags, (serialization << 4) | compression, 0x00)
    )
    body = (
        sequence.to_bytes(4, "big", signed=True) if sequence is not None else b""
    )
    body += len(encoded).to_bytes(4, "big") + encoded
    return header + body


def _unpack_message(message: bytes) -> dict[str, Any]:
    if len(message) < 8:
        raise ASRError("ASR returned a malformed WebSocket frame")
    message_type = message[1] >> 4
    flags = message[1] & 0x0F
    has_sequence = flags in {0x1, 0x2, 0x3}
    sequence = None
    payload_offset = 8
    if has_sequence:
        sequence = int.from_bytes(message[4:8], "big", signed=True)
        payload_offset = 12
    payload_size = int.from_bytes(message[payload_offset - 4:payload_offset], "big")
    payload = message[payload_offset:payload_offset + payload_size]
    if message[2] & 0x0F == 0x1:
        payload = gzip.decompress(payload)
    parsed: Any = payload
    if message_type in {0x09, 0x0F}:
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ASRError("ASR returned an invalid JSON payload") from exc
    return {
        "message_type": message_type,
        "flags": flags,
        "sequence": sequence,
        "payload": parsed,
    }


def _server_error(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or payload)
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return str(payload)
