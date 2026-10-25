from datetime import datetime, timedelta, timezone

import pytest

from app.domain import Event, EventSource, EventType
from app.runtime import (
    AgentRun,
    EventEnvelope,
    InMemoryRunStore,
    InvalidRunTransition,
    RunResult,
    RunStatus,
    TimerFired,
)


UTC = timezone.utc


def test_agent_run_transitions_and_result_are_structured() -> None:
    started = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
    finished = started + timedelta(seconds=2)
    run = AgentRun.create("event-1", conversation_id="conversation-1", run_id="run-1")

    run.start(step="load_context", at=started)
    run.complete({"answer": "已完成"}, metadata={"tool_calls": 2}, at=finished)

    assert run.status is RunStatus.COMPLETED
    assert run.started_at == started
    assert run.finished_at == finished
    assert run.current_step == "load_context"
    assert run.result == RunResult.ok({"answer": "已完成"}, metadata={"tool_calls": 2})
    assert run.to_dict()["result"]["success"] is True


def test_invalid_transition_and_terminal_immutability() -> None:
    run = AgentRun.create("event-1")

    with pytest.raises(InvalidRunTransition):
        run.complete("too early")

    run.start()
    run.fail("provider unavailable", code="llm_error")

    with pytest.raises(InvalidRunTransition):
        run.resume()

    assert run.error is not None
    assert run.error.code == "llm_error"
    assert run.result is not None
    assert run.result.success is False


def test_run_round_trip_and_memory_store() -> None:
    run = AgentRun.create("event-1", conversation_id="c-1", run_id="r-1")
    run.start(step="tool_call")
    run.complete({"value": 42})

    restored = AgentRun.from_dict(run.to_dict())
    store = InMemoryRunStore()
    store.create(restored)

    assert store.get("r-1") is restored
    assert store.list(statuses=[RunStatus.COMPLETED]) == [restored]
    with pytest.raises(ValueError):
        store.create(restored)


def test_event_envelope_round_trips_existing_domain_event() -> None:
    timestamp = datetime(2026, 10, 3, 2, 0, tzinfo=UTC)
    event = Event(
        event_id="event-2",
        source=EventSource.MICROPHONE,
        event_type=EventType.SPEECH,
        timestamp=timestamp,
        content={"text": "提醒我"},
        context={
            "conversation_id": "c-2",
            "correlation_id": "corr-1",
            "device_id": "watch-1",
        },
    )

    envelope = EventEnvelope.from_event(event)
    restored = EventEnvelope.from_dict(envelope.to_dict()).to_event()

    assert restored.event_id == event.event_id
    assert restored.source is EventSource.MICROPHONE
    assert restored.event_type is EventType.SPEECH
    assert restored.timestamp == timestamp
    assert restored.content == event.content
    assert restored.context["conversation_id"] == "c-2"
    assert restored.context["device_id"] == "watch-1"


def test_timer_fired_is_a_standard_timer_event() -> None:
    scheduled = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
    fired = scheduled + timedelta(seconds=3)
    timer = TimerFired(
        timer_id="timer-1",
        scheduled_for=scheduled,
        fired_at=fired,
        payload={"instruction": "检查天气"},
        event_id="event-timer-1",
    )

    event = timer.to_event()
    assert event.source is EventSource.SYSTEM
    assert event.event_type is EventType.TIMER
    assert event.content["timer_id"] == "timer-1"
    assert event.content["instruction"] == "检查天气"
    assert event.context["timer_id"] == "timer-1"

    restored = TimerFired.from_dict(timer.to_dict())
    assert restored == timer
