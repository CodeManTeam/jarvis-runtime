from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self, capacity: int = 200) -> None:
        self.capacity = capacity
        self._queues: dict[asyncio.Queue[dict[str, Any]], int] = {}

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.capacity)
        self._queues[queue] = 0
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.pop(queue, None)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    self._queues[queue] += 1
                except Exception:
                    continue
                try:
                    queue.put_nowait(event)
                except Exception:
                    pass
            except Exception:
                pass

    def stats(self) -> dict[str, Any]:
        return {"subscribers": len(self._queues), "dropped": sum(self._queues.values())}
