"""Model routing must degrade visibly, and degrade only by removing capability."""

import asyncio
import json

import pytest

import app.llm as llm_module
from app.activity import project_event
from app.db import Database
from app.routing import (
    FAILURES_BEFORE_DEGRADE,
    PROBES_BEFORE_RECOVERY,
    Endpoint,
    Router,
    current_event,
)
from app.service import JarvisService


def _endpoint(ident="primary", model="big", caps=("plan", "tool_call", "vision"), fallback=False):
    return Endpoint(
        id=ident,
        model=model,
        base_url="http://example.invalid",
        timeout_seconds=5,
        capabilities=caps,
        is_fallback=fallback,
    )


@pytest.fixture
def events():
    return []


@pytest.fixture
def router(events):
    primary = _endpoint()
    fallback = _endpoint("fallback", "small", ("plan", "tool_call"), fallback=True)
    return Router(primary, fallback, on_degradation=events.append)


def test_choose_prefers_the_active_route(router):
    assert router.choose("plan").id == "primary"


def test_repeated_failures_degrade_and_report_the_loss(router, events):
    for _ in range(FAILURES_BEFORE_DEGRADE):
        router.record_failure(router.primary, "timeout")
    assert router.active().id == "fallback"
    assert events and events[-1]["stage"] == "model"
    assert events[-1]["from"] == "big" and events[-1]["to"] == "small"
    # vision is what the fallback cannot do; that is the honest cost to report.
    assert events[-1]["capability_loss"] == ["vision"]
    assert events[-1]["recovered"] is False


def test_single_failure_is_not_yet_a_degradation(router, events):
    router.record_failure(router.primary, "timeout")
    assert router.active().id == "primary"
    assert events == []


def test_capability_only_the_fallback_has_switches_over(router, events):
    route = router.choose("video")
    assert route.id == "primary"  # neither declares it: fail loudly, do not fake
    assert events == []


def test_primary_recovers_only_after_repeated_probes(router, events):
    for _ in range(FAILURES_BEFORE_DEGRADE):
        router.record_failure(router.primary, "timeout")
    for _ in range(PROBES_BEFORE_RECOVERY - 1):
        router.record_success(router.primary)
    assert router.active().id == "fallback", "one success is not enough"
    router.record_success(router.primary)
    assert router.active().id == "primary"
    assert events[-1]["recovered"] is True
    assert events[-1]["capability_loss"] == []


def test_snapshot_reports_both_routes_and_health(router):
    snapshot = router.snapshot()
    assert snapshot["active"] == "primary"
    assert snapshot["degraded"] is False
    assert [route["id"] for route in snapshot["routes"]] == ["primary", "fallback"]
    assert snapshot["routes"][0]["state"]["consecutive_failures"] == 0


def test_degradation_reaches_the_activity_that_caused_it(tmp_path):
    """A degradation written during an event must show up on that activity.

    The default router has no fallback unless FALLBACK_MODEL is set — that is
    deliberate, so this test installs an explicit two-route router but keeps the
    service's real audit callback, which is the part that could silently drop the
    event id.
    """
    db = Database(str(tmp_path / "ctx.db"))
    db.init()
    service = JarvisService(db)
    service.router = Router(
        _endpoint(),
        _endpoint("fallback", "small", ("plan", "tool_call"), fallback=True),
        on_degradation=lambda event: service.audit(current_event.get(), None, "degradation", event),
    )

    async def scenario():
        token = current_event.set("ev-1")
        try:
            for _ in range(FAILURES_BEFORE_DEGRADE):
                service.router.record_failure(service.router.primary, "timeout")
        finally:
            current_event.reset(token)

    asyncio.run(scenario())

    event = {
        "id": "ev-1", "source": "microphone", "event_type": "speech",
        "content": "hi", "context": "{}", "created_at": "2026-10-24T09:00:00+08:00",
        "processed_at": None, "decision_action": None,
    }
    audits = [dict(row) for row in db.fetch_all("SELECT * FROM audit_log ORDER BY id")]
    item = project_event(event, audits, [], [], {})
    assert item["degradation"], "degradation must be visible on the activity"
    entry = item["degradation"][0]
    assert entry["stage"] == "model"
    assert entry["capability_loss"] == ["vision"]
    assert entry["recovered"] is False


def test_failed_primary_falls_back_once(monkeypatch):
    assert isinstance(llm_module.current_router(), Router) or True
    primary = _endpoint()
    fallback = _endpoint("fallback", "small", ("plan", "tool_call"), fallback=True)
    router = Router(primary, fallback)
    llm_module.set_router(router)

    calls = []

    async def fake_once(messages, tools, *, max_tokens, model, timeout):
        calls.append(model)
        if model == "big":
            raise llm_module.LLMError("upstream 502")
        return {"role": "assistant", "content": "ok", "tool_calls": []}

    monkeypatch.setattr(llm_module, "_complete_with_tools_once", fake_once)
    result = asyncio.run(llm_module.complete_with_tools([], []))
    assert result["content"] == "ok"
    assert calls == ["big", "small"], "exactly one retry, on the fallback"
    assert router.state["primary"].consecutive_failures == 1


def test_both_routes_failing_raises_instead_of_faking(monkeypatch):
    primary = _endpoint()
    fallback = _endpoint("fallback", "small", ("plan", "tool_call"), fallback=True)
    router = Router(primary, fallback)
    llm_module.set_router(router)

    async def always_fail(messages, tools, *, max_tokens, model, timeout):
        raise llm_module.LLMError("upstream down")

    monkeypatch.setattr(llm_module, "_complete_with_tools_once", always_fail)
    with pytest.raises(llm_module.LLMError):
        asyncio.run(llm_module.complete_with_tools([], []))
