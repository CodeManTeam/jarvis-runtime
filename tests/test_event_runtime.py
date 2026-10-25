import asyncio
from datetime import datetime, timezone

from app.domain import Event, EventSource, EventType
from app.runtime import (
    EventEnvelope,
    EventRuntime,
    RunResult,
    RunStatus,
    TimerFired,
)


UTC = timezone.utc


def test_event_runtime_creates_one_run_and_executes_timer_event() -> None:
    calls = []

    async def handler(envelope, run):
        calls.append((envelope.event_id, run.run_id, envelope.event_type))
        return {"next": "tts"}

    runtime = EventRuntime(handler)
    timer = TimerFired(
        timer_id="timer-1",
        scheduled_for=datetime(2026, 10, 3, 8, tzinfo=UTC),
        fired_at=datetime(2026, 10, 3, 8, 0, 1, tzinfo=UTC),
        payload={"instruction": "检查天气"},
        event_id="timer:task-1",
        conversation_id="watch-1",
    )

    result = asyncio.run(runtime.dispatch(timer))

    assert result.accepted is True
    assert result.duplicate is False
    assert result.run.run_id == "run:timer:task-1"
    assert result.run.status is RunStatus.COMPLETED
    assert result.run.result is not None
    assert result.run.result.output == {"next": "tts"}
    assert calls == [("timer:task-1", "run:timer:task-1", "timer")]


def test_duplicate_event_returns_original_result_without_second_side_effect() -> None:
    calls = 0

    async def handler(_, __):
        nonlocal calls
        calls += 1
        return RunResult.ok("只执行一次")

    runtime = EventRuntime(handler)
    event = Event(
        event_id="event-1",
        source=EventSource.SYSTEM,
        event_type=EventType.NOTIFICATION,
        timestamp=datetime(2026, 10, 3, tzinfo=UTC),
        content={"text": "提醒"},
        context={"conversation_id": "c-1"},
    )

    first = asyncio.run(runtime.dispatch(event))
    second = asyncio.run(runtime.dispatch(event))
    by_id = asyncio.run(runtime.dispatch("event-1"))

    assert first.accepted is True
    assert second.accepted is False
    assert second.duplicate is True
    assert by_id.run is first.run
    assert calls == 1


def test_handler_failure_becomes_structured_failed_run() -> None:
    async def handler(_, __):
        raise RuntimeError("MCP unavailable")

    runtime = EventRuntime(handler)
    envelope = EventEnvelope(
        event_id="event-fail",
        source="system",
        event_type="notification",
        occurred_at=datetime(2026, 10, 3, tzinfo=UTC),
        payload={"text": "检查"},
    )

    result = asyncio.run(runtime.dispatch(envelope))

    assert result.run.status is RunStatus.FAILED
    assert result.run.error is not None
    assert result.run.error.code == "handler_error"
    assert result.run.error.message == "MCP unavailable"
    assert result.run.result is not None
    assert result.run.result.success is False


def test_handler_can_pause_a_run_and_resume_from_same_context() -> None:
    calls = 0

    async def handler(envelope, run):
        nonlocal calls
        calls += 1
        if calls == 1:
            run.wait(step="waiting_device")
            return None
        return {"done": True}

    runtime = EventRuntime(handler)
    event = EventEnvelope(
        event_id="event-wait",
        source="device",
        event_type="device_heartbeat",
        occurred_at=datetime(2026, 10, 3, tzinfo=UTC),
        payload={"device_id": "watch-1"},
    )

    first = asyncio.run(runtime.dispatch(event))
    assert first.run.status is RunStatus.WAITING
    assert first.run.current_step == "waiting_device"

    second = asyncio.run(runtime.dispatch("event-wait"))
    assert second.duplicate is True
    assert second.run.status is RunStatus.COMPLETED
    assert calls == 2
