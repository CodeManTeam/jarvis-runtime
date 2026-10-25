import asyncio

from app.eventbus import EventBus


def test_publish_and_subscribe():
    bus = EventBus()
    queue = bus.subscribe()
    bus.publish({"event": "job_status", "job_id": "j1"})
    assert queue.get_nowait()["job_id"] == "j1"
    bus.unsubscribe(queue)
    assert bus.stats()["subscribers"] == 0


def test_slow_subscriber_drops_oldest():
    bus = EventBus(capacity=2)
    queue = bus.subscribe()
    for value in range(3):
        bus.publish({"value": value})
    assert [queue.get_nowait()["value"], queue.get_nowait()["value"]] == [1, 2]
    assert bus.stats()["dropped"] == 1


def test_publish_survives_broken_subscriber():
    bus = EventBus()
    queue = bus.subscribe()

    class BrokenQueue(asyncio.Queue):
        def put_nowait(self, item):
            raise RuntimeError("broken")

    broken = BrokenQueue()
    bus._queues[broken] = 0
    bus.publish({"event": "activity"})
    assert queue.get_nowait()["event"] == "activity"
