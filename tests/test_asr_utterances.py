"""The time axis must reach the watch, or a transcript cannot be replayed."""

import asyncio

import pytest

from app.asr import RealtimeASRSession


def test_utterances_are_kept_from_the_provider_response():
    session = RealtimeASRSession.__new__(RealtimeASRSession)
    session.last_utterances = [
        {"text": "第一句", "start_ms": 0, "end_ms": 1200},
        {"text": "第二句", "start_ms": 1200, "end_ms": 2600},
    ]
    assert len(session.utterances) == 2
    assert session.utterances[-1]["end_ms"] == 2600


def test_a_session_without_timings_reports_an_empty_list():
    session = RealtimeASRSession.__new__(RealtimeASRSession)
    assert session.utterances == []


class _FakeSession:
    pcm_bytes = 32000
    sample_rate = 16000
    channels = 1
    sample_width = 2
    utterances = [
        {"text": "提醒我下午三点开会", "start_ms": 0, "end_ms": 2400},
    ]

    async def final(self):
        return "提醒我下午三点开会"


def test_speech_final_carries_utterances(tmp_path):
    from app.db import Database
    from app.service import JarvisService

    database = Database(str(tmp_path / "utt.db"))
    database.init()
    service = JarvisService(database, artifacts_root=tmp_path / "artifacts")
    service._speech_sessions["s1"] = _FakeSession()

    result = asyncio.run(service.ingest_audio_message(
        "watch-1", {"type": "audio_end", "session_id": "s1"}
    ))
    assert result["type"] == "speech_final"
    assert result["utterances"][0]["end_ms"] == 2400
