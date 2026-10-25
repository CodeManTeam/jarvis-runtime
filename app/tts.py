from __future__ import annotations

from typing import AsyncIterator

from .config import settings


class TTSError(RuntimeError):
    pass


async def edge_audio(text: str) -> bytes:
    """Render one short speech segment on the server with Edge TTS."""
    if not text.strip():
        return b""
    try:
        import edge_tts
    except ImportError as exc:
        raise TTSError("edge-tts is not installed") from exc

    communicate = edge_tts.Communicate(
        text,
        settings.tts_voice,
        rate=settings.tts_rate,
    )
    data = bytearray()
    try:
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                data.extend(chunk.get("data") or b"")
    except Exception as exc:  # upstream handshake/network failures are unavailability
        raise TTSError(f"edge-tts unavailable: {type(exc).__name__}") from exc
    if not data:
        raise TTSError("edge-tts returned no audio")
    return bytes(data)


async def edge_audio_stream(text: str) -> AsyncIterator[bytes]:
    """Yield audio chunks as they arrive for clients that support raw streams."""
    if not text.strip():
        return
    try:
        import edge_tts
    except ImportError as exc:
        raise TTSError("edge-tts is not installed") from exc

    communicate = edge_tts.Communicate(
        text,
        settings.tts_voice,
        rate=settings.tts_rate,
    )
    try:
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                yield bytes(chunk["data"])
    except Exception as exc:  # upstream handshake/network failures are unavailability
        raise TTSError(f"edge-tts unavailable: {type(exc).__name__}") from exc
