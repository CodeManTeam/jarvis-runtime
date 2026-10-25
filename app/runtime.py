"""Persistent-agent runtime contracts without a storage dependency.

The application can later persist these objects in SQLite, Redis, or another
backend.  This module intentionally only defines state, transitions, and
serialization; it does not start workers or depend on a web framework.
"""

from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable
from uuid import uuid4

from .domain import Event, EventSource, EventType


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidRunTransition(ValueError):
    """Raised when an AgentRun is moved through an unsupported transition."""


@dataclass(frozen=True)
class RunError:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": deepcopy(self.details),
        }

    @classmethod
    def from_value(cls, value: Any) -> RunError | None:
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(code="runtime_error", message=value)
        if isinstance(value, dict):
            return cls(
                code=str(value.get("code", "runtime_error")),
                message=str(value.get("message", "")),
                details=dict(value.get("details") or {}),
            )
        return cls(code="runtime_error", message=str(value))


@dataclass(frozen=True)
class RunResult:
    """Stable result envelope for a completed or failed AgentRun."""

    success: bool
    output: Any = None
    error: RunError | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(
        cls,
        output: Any = None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> RunResult:
        return cls(True, output, None, dict(metadata or {}))

    @classmethod
    def failed(
        cls,
        error: str | RunError,
        *,
        code: str = "runtime_error",
        details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunResult:
        normalized = (
            error
            if isinstance(error, RunError)
            else RunError(code=code, message=error, details=dict(details or {}))
        )
        return cls(False, None, normalized, dict(metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "output": deepcopy(self.output),
            "error": self.error.to_dict() if self.error else None,
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> RunResult | None:
        if value is None:
            return None
        return cls(
            success=bool(value.get("success")),
            output=deepcopy(value.get("output")),
            error=RunError.from_value(value.get("error")),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass
class AgentRun:
    """A stateful execution started by one event."""

    run_id: str
    event_id: str
    conversation_id: str | None = None
    status: RunStatus = RunStatus.QUEUED
    current_step: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: RunError | None = None
    result: RunResult | None = None

    _TRANSITIONS = {
        RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED},
        RunStatus.RUNNING: {
            RunStatus.WAITING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        },
        RunStatus.WAITING: {
            RunStatus.RUNNING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        },
        RunStatus.COMPLETED: set(),
        RunStatus.FAILED: set(),
        RunStatus.CANCELLED: set(),
    }

    @classmethod
    def create(
        cls,
        event_id: str,
        *,
        conversation_id: str | None = None,
        run_id: str | None = None,
    ) -> AgentRun:
        return cls(
            run_id=run_id or str(uuid4()),
            event_id=event_id,
            conversation_id=conversation_id,
        )

    @property
    def terminal(self) -> bool:
        return self.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }

    def transition(
        self,
        status: RunStatus | str,
        *,
        current_step: str | None = None,
        result: RunResult | None = None,
        error: RunError | str | None = None,
        at: datetime | None = None,
    ) -> AgentRun:
        target = status if isinstance(status, RunStatus) else RunStatus(status)
        if target != self.status and target not in self._TRANSITIONS[self.status]:
            raise InvalidRunTransition(
                f"cannot transition run {self.run_id} "
                f"from {self.status.value} to {target.value}"
            )

        timestamp = at or utc_now()
        if target == RunStatus.RUNNING and self.started_at is None:
            self.started_at = timestamp
        if current_step is not None:
            self.current_step = current_step
        if error is not None:
            self.error = RunError.from_value(error)
        if result is not None:
            self.result = result
            if result.error is not None:
                self.error = result.error
        self.status = target
        if target in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            self.finished_at = timestamp
        return self

    def start(self, *, step: str | None = None, at: datetime | None = None) -> AgentRun:
        return self.transition(RunStatus.RUNNING, current_step=step, at=at)

    def wait(self, *, step: str | None = None, at: datetime | None = None) -> AgentRun:
        return self.transition(RunStatus.WAITING, current_step=step, at=at)

    def resume(self, *, step: str | None = None, at: datetime | None = None) -> AgentRun:
        return self.transition(RunStatus.RUNNING, current_step=step, at=at)

    def complete(
        self,
        output: Any = None,
        *,
        metadata: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> AgentRun:
        return self.transition(
            RunStatus.COMPLETED,
            result=RunResult.ok(output, metadata=metadata),
            at=at,
        )

    def fail(
        self,
        error: str | RunError,
        *,
        code: str = "runtime_error",
        details: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> AgentRun:
        result = RunResult.failed(error, code=code, details=details)
        return self.transition(RunStatus.FAILED, result=result, at=at)

    def cancel(self, *, reason: str | None = None, at: datetime | None = None) -> AgentRun:
        return self.transition(
            RunStatus.CANCELLED,
            error=RunError("cancelled", reason or "run cancelled") if reason else None,
            at=at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "event_id": self.event_id,
            "conversation_id": self.conversation_id,
            "status": self.status.value,
            "current_step": self.current_step,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "error": self.error.to_dict() if self.error else None,
            "result": self.result.to_dict() if self.result else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AgentRun:
        return cls(
            run_id=str(value["run_id"]),
            event_id=str(value["event_id"]),
            conversation_id=value.get("conversation_id"),
            status=RunStatus(value.get("status", RunStatus.QUEUED.value)),
            current_step=value.get("current_step"),
            started_at=_datetime(value.get("started_at")),
            finished_at=_datetime(value.get("finished_at")),
            error=RunError.from_value(value.get("error")),
            result=RunResult.from_dict(value.get("result")),
        )


class InMemoryRunStore:
    """A storage-independent state object useful for tests and early runtime work."""

    def __init__(self) -> None:
        self._runs: dict[str, AgentRun] = {}

    def create(self, run: AgentRun) -> AgentRun:
        if run.run_id in self._runs:
            raise ValueError(f"run already exists: {run.run_id}")
        self._runs[run.run_id] = run
        return run

    def get(self, run_id: str) -> AgentRun | None:
        return self._runs.get(run_id)

    def transition(self, run_id: str, status: RunStatus | str, **kwargs: Any) -> AgentRun:
        run = self._require(run_id)
        return run.transition(status, **kwargs)

    def list(self, *, statuses: Iterable[RunStatus | str] | None = None) -> list[AgentRun]:
        runs = list(self._runs.values())
        if statuses is None:
            return runs
        allowed = {item.value if isinstance(item, RunStatus) else item for item in statuses}
        return [run for run in runs if run.status.value in allowed]

    def _require(self, run_id: str) -> AgentRun:
        run = self.get(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        return run


@dataclass(frozen=True)
class EventEnvelope:
    """Transport-neutral event wrapper compatible with :class:`domain.Event`."""

    event_id: str
    source: str
    event_type: str
    occurred_at: datetime
    payload: Any
    context: dict[str, Any] = field(default_factory=dict)
    conversation_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None

    @classmethod
    def from_event(cls, event: Event) -> EventEnvelope:
        context = dict(event.context)
        return cls(
            event_id=event.event_id,
            source=getattr(event.source, "value", str(event.source)),
            event_type=getattr(event.event_type, "value", str(event.event_type)),
            occurred_at=event.timestamp,
            payload=deepcopy(event.content),
            context=context,
            conversation_id=context.get("conversation_id"),
            correlation_id=context.get("correlation_id"),
            causation_id=context.get("causation_id"),
        )

    def to_event(self) -> Event:
        context = dict(self.context)
        for key, value in (
            ("conversation_id", self.conversation_id),
            ("correlation_id", self.correlation_id),
            ("causation_id", self.causation_id),
        ):
            if value is not None:
                context.setdefault(key, value)
        return Event(
            event_id=self.event_id,
            source=_enum_or_value(EventSource, self.source),
            event_type=_enum_or_value(EventType, self.event_type),
            timestamp=self.occurred_at,
            content=deepcopy(self.payload),
            context=context,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": deepcopy(self.payload),
            "context": deepcopy(self.context),
            "conversation_id": self.conversation_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EventEnvelope:
        return cls(
            event_id=str(value["event_id"]),
            source=str(value["source"]),
            event_type=str(value["event_type"]),
            occurred_at=_datetime(value["occurred_at"]) or utc_now(),
            payload=deepcopy(value.get("payload")),
            context=dict(value.get("context") or {}),
            conversation_id=value.get("conversation_id"),
            correlation_id=value.get("correlation_id"),
            causation_id=value.get("causation_id"),
        )


@dataclass(frozen=True)
class TimerFired:
    """Canonical scheduler event payload, still convertible to domain.Event."""

    timer_id: str
    scheduled_for: datetime
    fired_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    conversation_id: str | None = None
    correlation_id: str | None = None

    def to_envelope(self) -> EventEnvelope:
        return EventEnvelope(
            event_id=self.event_id,
            source=EventSource.SYSTEM.value,
            event_type=EventType.TIMER.value,
            occurred_at=self.fired_at,
            payload={
                **deepcopy(self.payload),
                "timer_id": self.timer_id,
                "scheduled_for": self.scheduled_for.isoformat(),
            },
            context={"timer_id": self.timer_id},
            conversation_id=self.conversation_id,
            correlation_id=self.correlation_id,
        )

    def to_event(self) -> Event:
        return self.to_envelope().to_event()

    def to_dict(self) -> dict[str, Any]:
        return {
            "timer_id": self.timer_id,
            "scheduled_for": self.scheduled_for.isoformat(),
            "fired_at": self.fired_at.isoformat(),
            "payload": deepcopy(self.payload),
            "event_id": self.event_id,
            "conversation_id": self.conversation_id,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TimerFired:
        return cls(
            timer_id=str(value["timer_id"]),
            scheduled_for=_datetime(value["scheduled_for"]) or utc_now(),
            fired_at=_datetime(value["fired_at"]) or utc_now(),
            payload=dict(value.get("payload") or {}),
            event_id=str(value.get("event_id") or uuid4()),
            conversation_id=value.get("conversation_id"),
            correlation_id=value.get("correlation_id"),
        )


EventInput = EventEnvelope | TimerFired | Event
EventHandler = Callable[
    [EventEnvelope, AgentRun],
    Any | Awaitable[Any],
]


@dataclass(frozen=True)
class EventDispatchResult:
    """Outcome of accepting or executing one event."""

    accepted: bool
    duplicate: bool
    envelope: EventEnvelope
    run: AgentRun

    @property
    def completed(self) -> bool:
        return self.run.status is RunStatus.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "event": self.envelope.to_dict(),
            "run": self.run.to_dict(),
        }


class EventRuntime:
    """Small event-driven AgentRun coordinator.

    This is deliberately transport- and storage-neutral.  ``accept`` performs
    idempotent event admission; ``dispatch`` wakes the associated AgentRun and
    invokes the supplied handler.  A database-backed store or a queue can later
    replace the in-memory pieces without changing the event contract.
    """

    def __init__(
        self,
        handler: EventHandler,
        *,
        run_store: InMemoryRunStore | None = None,
    ) -> None:
        self.handler = handler
        self.run_store = run_store or InMemoryRunStore()
        self._events: dict[str, EventEnvelope] = {}

    def accept(self, event: EventInput) -> EventDispatchResult:
        envelope = _as_envelope(event)
        existing = self._events.get(envelope.event_id)
        if existing is not None:
            run = self.run_store.get(f"run:{envelope.event_id}")
            if run is None:
                raise RuntimeError(
                    f"event {envelope.event_id} exists without its AgentRun"
                )
            return EventDispatchResult(
                accepted=False,
                duplicate=True,
                envelope=existing,
                run=run,
            )

        run = AgentRun.create(
            envelope.event_id,
            conversation_id=envelope.conversation_id,
            run_id=f"run:{envelope.event_id}",
        )
        self._events[envelope.event_id] = envelope
        self.run_store.create(run)
        return EventDispatchResult(
            accepted=True,
            duplicate=False,
            envelope=envelope,
            run=run,
        )

    def get_event(self, event_id: str) -> EventEnvelope | None:
        return self._events.get(event_id)

    async def dispatch(self, event: EventInput | str) -> EventDispatchResult:
        admitted = (
            self.accept(event)
            if not isinstance(event, str)
            else self._existing_result(event)
        )
        run = admitted.run
        if run.terminal:
            return admitted

        if run.status is RunStatus.WAITING:
            run.resume(step="agent.handle")
        elif run.status is RunStatus.QUEUED:
            run.start(step="agent.handle")

        try:
            output = self.handler(admitted.envelope, run)
            if inspect.isawaitable(output):
                output = await output
            result = output if isinstance(output, RunResult) else RunResult.ok(output)
            if run.status is RunStatus.WAITING:
                # The handler explicitly yielded to a device, user, or timer.
                # The next dispatch resumes this same AgentRun.
                pass
            elif result.success:
                run.complete(
                    result.output,
                    metadata=result.metadata,
                )
            else:
                run.transition(RunStatus.FAILED, result=result)
        except Exception as exc:
            run.fail(
                str(exc) or exc.__class__.__name__,
                code="handler_error",
                details={"exception": exc.__class__.__name__},
            )
        return admitted

    def _existing_result(self, event_id: str) -> EventDispatchResult:
        envelope = self._events.get(event_id)
        run = self.run_store.get(f"run:{event_id}")
        if envelope is None or run is None:
            raise KeyError(f"unknown event: {event_id}")
        return EventDispatchResult(
            accepted=False,
            duplicate=True,
            envelope=envelope,
            run=run,
        )


def _as_envelope(event: EventInput) -> EventEnvelope:
    if isinstance(event, EventEnvelope):
        return event
    if isinstance(event, TimerFired):
        return event.to_envelope()
    if isinstance(event, Event):
        return EventEnvelope.from_event(event)
    raise TypeError(f"unsupported event input: {type(event).__name__}")


def _enum_or_value(enum_type: type[Enum], value: str) -> Any:
    try:
        return enum_type(value)
    except ValueError:
        return value


__all__ = [
    "AgentRun",
    "EventDispatchResult",
    "EventEnvelope",
    "EventRuntime",
    "InMemoryRunStore",
    "InvalidRunTransition",
    "RunError",
    "RunResult",
    "RunStatus",
    "TimerFired",
    "utc_now",
]
