from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import Database
from app.service import JarvisService


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _service(tmp_path) -> JarvisService:
    database = Database(str(tmp_path / "scheduler.db"))
    database.init()
    return JarvisService(database)


def _due_task(
    service: JarvisService,
    *,
    title: str = "check weather",
    device_id: str = "watch-6110d460",
    payload: dict[str, Any] | None = None,
    max_retries: int = 3,
) -> dict[str, Any]:
    scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    task_payload = {
        "device_id": device_id,
        "location": "Shanghai",
        **(payload or {}),
    }
    return service.create_task(
        title=title,
        run_at=_iso(scheduled_at),
        source="test",
        payload=task_payload,
        max_retries=max_retries,
    )


def _dispatch_with_agent_result(
    service: JarvisService,
    messages: list[tuple[str, dict[str, Any]]],
) -> int:
    async def handle(event) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "reply": "test completed",
            "action": "reply",
        }

    service.orchestrator.handle = handle

    async def publish(event) -> dict[str, Any]:
        return await service.orchestrator.handle(event)

    service.publish_event = publish

    async def push(device_id: str, message: dict[str, Any]) -> bool:
        messages.append((device_id, message))
        return True

    service.set_push_callback(push)
    return asyncio.run(service.dispatch_due_tasks())


def _dispatch_as_published_event(
    service: JarvisService,
    messages: list[tuple[str, dict[str, Any]]],
) -> int:
    async def publish(event) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "action": "published",
            "reply": "",
        }

    service.publish_event = publish

    async def push(device_id: str, message: dict[str, Any]) -> bool:
        messages.append((device_id, message))
        return True

    service.set_push_callback(push)
    return asyncio.run(service.dispatch_due_tasks())


def test_due_task_is_published_as_timer_fired_event(tmp_path) -> None:
    service = _service(tmp_path)
    task = _due_task(
        service,
        title="bring umbrella",
        payload={"instruction": "remind me if it rains"},
    )
    messages: list[tuple[str, dict[str, Any]]] = []

    assert _dispatch_as_published_event(service, messages) == 1
    assert len(messages) == 1

    device_id, event = messages[0]
    assert device_id == "watch-6110d460"
    assert event["type"] == "timer.fired"
    assert event["event_type"] == "timer.fired"
    assert event["task_id"] == task["id"]
    assert event["title"] == "bring umbrella"
    assert event["payload"] == {
        "device_id": "watch-6110d460",
        "location": "Shanghai",
        "instruction": "remind me if it rains",
    }
    assert event["device_id"] == "watch-6110d460"
    assert event["scheduled_at"] == task["run_at"]


def test_due_task_selection_only_includes_pending_tasks_ready_to_run(tmp_path) -> None:
    service = _service(tmp_path)
    due = _due_task(service, title="due")
    future = service.create_task(
        title="future",
        run_at=_iso(datetime.now(timezone.utc) + timedelta(hours=1)),
        source="test",
        payload={"device_id": "watch-6110d460"},
    )
    service.create_task(
        title="no schedule",
        source="test",
        payload={"device_id": "watch-6110d460"},
    )
    waiting = _due_task(service, title="already notified")
    service.database.execute(
        "UPDATE tasks SET status = 'waiting' WHERE id = ?", (waiting["id"],)
    )
    retry_later = _due_task(service, title="backing off")
    service.database.execute(
        "UPDATE tasks SET next_attempt_at = ? WHERE id = ?",
        (
            _iso(datetime.now(timezone.utc) + timedelta(minutes=5)),
            retry_later["id"],
        ),
    )

    due_tasks = service.get_due_tasks()
    assert [task["id"] for task in due_tasks] == [due["id"]]
    assert future["id"] not in {task["id"] for task in due_tasks}


def test_due_task_accepts_zulu_timestamps_from_api(tmp_path) -> None:
    service = _service(tmp_path)
    scheduled_at = (datetime.now(timezone.utc) - timedelta(seconds=1))
    task = service.create_task(
        title="Zulu timestamp",
        run_at=scheduled_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        source="api",
        payload={"device_id": "watch-6110d460"},
    )

    assert [item["id"] for item in service.get_due_tasks()] == [task["id"]]


def test_successful_dispatch_is_idempotent_after_task_is_claimed(tmp_path) -> None:
    service = _service(tmp_path)
    task = _due_task(service)
    messages: list[tuple[str, dict[str, Any]]] = []

    assert _dispatch_with_agent_result(service, messages) == 1
    first_dispatch_count = len(messages)
    assert _dispatch_with_agent_result(service, messages) == 0

    stored = service.database.fetch_one(
        "SELECT * FROM tasks WHERE id = ?", (task["id"],)
    )
    assert stored is not None
    assert stored["status"] == "waiting"
    # A dispatch pushes the agent event; speech is no longer automatic, so the
    # old ">= 3 messages ending in tts_end" expectation encoded a decision we
    # deliberately reversed (text first, audio on demand).
    assert first_dispatch_count >= 1
    assert [message["type"] for _, message in messages[:1]] == ["agent_event"]
    assert all(message["type"] != "tts_start" for _, message in messages)
    assert len(messages) == first_dispatch_count


def test_failed_delivery_returns_task_to_pending_with_backoff(tmp_path) -> None:
    service = _service(tmp_path)
    task = _due_task(service, max_retries=2)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def publish(event) -> dict[str, Any]:
        return {"event_id": event.event_id, "action": "published", "reply": ""}

    service.publish_event = publish

    async def push(device_id: str, message: dict[str, Any]) -> bool:
        calls.append((device_id, message))
        return False

    service.set_push_callback(push)
    assert asyncio.run(service.dispatch_due_tasks()) == 1

    stored = service.database.fetch_one(
        "SELECT * FROM tasks WHERE id = ?", (task["id"],)
    )
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["retry_count"] == 1
    assert stored["last_error"] == "设备推送失败"
    assert stored["next_attempt_at"] is not None
    assert len(calls) == 1


def test_failed_delivery_is_dead_lettered_after_max_retries(tmp_path) -> None:
    service = _service(tmp_path)
    task = _due_task(service, max_retries=1)

    async def publish(event) -> dict[str, Any]:
        return {"event_id": event.event_id, "action": "published", "reply": ""}

    service.publish_event = publish

    async def push(_: str, __: dict[str, Any]) -> bool:
        return False

    service.set_push_callback(push)
    asyncio.run(service.dispatch_due_tasks())

    stored = service.database.fetch_one(
        "SELECT * FROM tasks WHERE id = ?", (task["id"],)
    )
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["retry_count"] == 1
    assert stored["dead_letter_at"] is not None


def test_timer_event_has_stable_idempotency_key(tmp_path) -> None:
    service = _service(tmp_path)
    task = _due_task(service)
    messages: list[tuple[str, dict[str, Any]]] = []

    _dispatch_as_published_event(service, messages)
    event = messages[0][1]

    assert event["event_id"] == f"timer:{task['id']}"
    assert event["dedupe_key"] == f"timer:{task['id']}"


def test_contract_payload_can_be_serialized_without_database_rows(tmp_path) -> None:
    service = _service(tmp_path)
    _due_task(service, payload={"nested": {"enabled": True}})
    messages: list[tuple[str, dict[str, Any]]] = []

    _dispatch_as_published_event(service, messages)

    # The terminal/event boundary must carry JSON data, not sqlite.Row objects.
    json.dumps(messages[0][1], ensure_ascii=False)
