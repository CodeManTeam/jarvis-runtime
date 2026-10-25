"""Audio is opt-in: text arrives first, and speaking is a deliberate act."""

import pytest

from app.service import JarvisService, should_auto_speak


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"action": "reply", "reply": "好的"}, False),
        ({"action": "reply", "reply": "好的", "urgency": 1}, False),
        ({"action": "reply", "reply": "门没关", "urgency": 4}, True),
        ({"action": "reply", "reply": "现在播报", "speak": True}, True),
        ({"action": "reply", "reply": "x", "urgency": "not-a-number"}, False),
    ],
)
def test_only_interruptions_speak_on_their_own(result, expected):
    assert should_auto_speak(result) is expected


def test_a_normal_reply_does_not_start_tts(tmp_path):
    from app.db import Database

    database = Database(str(tmp_path / "tts.db"))
    database.init()
    service = JarvisService(database, artifacts_root=tmp_path / "artifacts")

    pushed = []

    async def fake_push(device_id, payload):
        pushed.append(payload)
        return True

    import asyncio

    service.push_callback = fake_push
    database.execute(
        "INSERT INTO devices (id, name, platform, last_seen, tts_enabled, tts_ready) "
        "VALUES (?, ?, ?, ?, 1, 1)",
        ("w1", "watch", "android-watch", "2026-10-25T00:00:00+00:00"),
    )
    asyncio.run(service.push_agent_result(
        "w1", {"action": "reply", "reply": "普通回复", "event_id": "e1"}, event_id="e1",
    ))

    kinds = [payload.get("type") for payload in pushed]
    assert "agent_event" in kinds
    assert "tts_start" not in kinds, "a plain reply must not start speaking by itself"
