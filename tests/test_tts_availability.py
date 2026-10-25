"""TTS outages must degrade to text instead of breaking delivery.

The watch spec says "TTS 不可用 → 仅播文字"; a failed upstream handshake is
therefore a normal, auditable condition, not an exception that aborts the task.
"""

import asyncio

import pytest

from app import tts


class _FailingCommunicate:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def stream(self):
        raise RuntimeError("handshake rejected")
        yield  # pragma: no cover - keeps this an async generator


class _SilentCommunicate:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def stream(self):
        yield {"type": "audio", "data": b"\x00\x01"}


def test_upstream_failure_is_reported_as_tts_error(monkeypatch):
    import edge_tts

    monkeypatch.setattr(edge_tts, "Communicate", _FailingCommunicate)
    with pytest.raises(tts.TTSError):
        asyncio.run(tts.edge_audio("你好"))


def test_upstream_failure_in_stream_is_reported_as_tts_error(monkeypatch):
    import edge_tts

    monkeypatch.setattr(edge_tts, "Communicate", _FailingCommunicate)

    async def drain():
        async for _ in tts.edge_audio_stream("你好"):
            pass

    with pytest.raises(tts.TTSError):
        asyncio.run(drain())


def test_blank_text_never_calls_upstream(monkeypatch):
    import edge_tts

    def explode(*args, **kwargs):
        raise AssertionError("must not construct a TTS client for blank text")

    monkeypatch.setattr(edge_tts, "Communicate", explode)
    assert asyncio.run(tts.edge_audio("   ")) == b""


def test_audio_chunks_are_concatenated(monkeypatch):
    import edge_tts

    monkeypatch.setattr(edge_tts, "Communicate", _SilentCommunicate)
    assert asyncio.run(tts.edge_audio("你好")) == b"\x00\x01"
