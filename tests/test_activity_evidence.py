from datetime import datetime, timedelta, timezone

from app.activity import classify_attention, project_event


def _event(**context):
    return {
        "id": "e1", "source": "microphone", "event_type": "speech",
        "content": "event", "context": context, "created_at": "2026-10-24T09:00:00+08:00",
        "processed_at": None, "decision_action": None,
    }


def _audit(action, detail, ident=1):
    return {"id": ident, "action": action, "detail": detail, "created_at": "2026-10-24T09:01:00+08:00"}


def test_attention_four_classes_and_pending_precedence():
    # A plain answer is an automatic completion, never "ignored".
    assert classify_attention(_event(), [], []) == "auto_done"
    assert classify_attention(
        _event(), [_audit("event_result", {"action": "reply", "reply": "好的"})], []
    ) == "auto_done"
    assert classify_attention(_event(candidate_type="memory"), [], []) == "suggest_once"
    assert classify_attention(_event(), [_audit("policy_decision", {"capability": "reminder.write"})], [_pending()]) == "high_risk_waiting"
    assert classify_attention(_event(), [_audit("policy_decision", {"capability": "reminder.write"}), _audit("tool_call", {})], []) == "auto_done"
    assert classify_attention(_event(), [_audit("activity_action", {"action": "ignore"})], []) == "ignored"


def test_ignored_requires_explicit_evidence():
    # Nothing may be filed as "ignored" merely because it matched no other rule.
    assert classify_attention(_event(), [_audit("agent_run_failed", {})], []) == "auto_done"
    ignored_event = dict(_event())
    ignored_event["decision_action"] = "ignore"
    assert classify_attention(ignored_event, [], []) == "ignored"


def _pending():
    return {"id": "p1", "action": "confirm", "created_at": "2026-10-24T09:00:00+08:00", "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), "capability": "message.send"}


def test_missing_confidence_and_deleted_memory_reference_serialize():
    item = project_event(_event(memory_refs=[{"id": "gone", "title": "Removed"}]), [], [], [], {})
    assert item["confidence"] is None
    assert item["confidence_basis"] == "none"
    assert item["evidence"]["refs"]["memories"][0]["available"] is False
    assert isinstance(item["etag"], str)


def test_degradation_aggregation_preserves_recovery():
    audits = [
        _audit("degradation", {"stage": "model", "from": "J-Pro", "to": "J-Lite", "capability_loss": ["attachment"], "recovered": False}),
        _audit("degradation", {"stage": "tool", "from": "file.preview", "to": "retry", "capability_loss": [], "recovered": True, "attempt": "1/2"}, 2),
    ]
    result = project_event(_event(), audits, [], [], {})
    assert len(result["degradation"]) == 2
    assert result["degradation"][1]["recovered"] is True
    assert result["degradation"][0]["capability_loss"] == ["attachment"]


def test_grant_only_for_high_risk_waiting():
    pending = [_pending()]
    waiting = project_event(_event(), [], pending, [], {})
    normal = project_event(_event(), [], [], [], {})
    assert waiting["grant"]["id"] == "p1"
    assert normal["grant"] is None


def test_history_meta_counts_activity_actions():
    audits = [
        _audit("activity_action", {"action": "modify"}),
        _audit("activity_action", {"action": "delete"}, 2),
        _audit("activity_action", {"action": "undo"}, 3),
    ]
    meta = project_event(_event(), audits, [], [], {})["detail"]["history_meta"]
    assert meta["modified"] == meta["deleted"] == meta["undone"] == 1
    assert meta["created"] == "2026-10-24T09:00:00+08:00"
