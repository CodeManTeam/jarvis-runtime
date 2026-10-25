"""The console needs the partial transcript too, not only the device socket."""

import asyncio

from app.asr import ASRError


class _FakeSession:
    pcm_bytes = 32000

    async def partial(self):
        return "我正在说这句话"


def test_a_partial_is_published_to_the_bus(tmp_path):
    from app.db import Database
    from app.service import JarvisService

    database = Database(str(tmp_path / "p.db"))
    database.init()
    service = JarvisService(database, artifacts_root=tmp_path / "artifacts")

    published = []
    pushed = []

    service.event_publisher = published.append

    async def fake_push(device_id, payload):
        pushed.append(payload)
        return True

    service.push_callback = fake_push
    service._speech_sessions["s1"] = _FakeSession()

    async def run():
        task = asyncio.create_task(service._emit_speech_partial("w1", "s1"))
        await asyncio.sleep(1.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())

    kinds = [event.get("event") for event in published]
    assert "asr_partial" in kinds, published
    partial = next(event for event in published if event["event"] == "asr_partial")
    assert partial["text"] == "我正在说这句话"
    assert partial["final"] is False
    # the device push is unchanged
    assert any(payload.get("type") == "speech_partial" for payload in pushed)


def test_a_quiet_session_publishes_nothing(tmp_path):
    from app.db import Database
    from app.service import JarvisService

    database = Database(str(tmp_path / "q.db"))
    database.init()
    service = JarvisService(database, artifacts_root=tmp_path / "artifacts")
    published = []
    service.event_publisher = published.append

    class Quiet:
        pcm_bytes = 100  # below the threshold

        async def partial(self):
            raise AssertionError("should not be called")

    service._speech_sessions["s2"] = Quiet()

    async def run():
        task = asyncio.create_task(service._emit_speech_partial("w1", "s2"))
        await asyncio.sleep(1.1)
        await task

    asyncio.run(run())
    assert published == []
