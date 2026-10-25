"""Interruption policy: permission requests wake the screen, the rest only nudge."""

from app.service import JarvisService


def envelope(**result):
    return JarvisService._attention_envelope(result)


def test_permission_request_is_high_and_opens_the_card():
    payload = envelope(action="confirm", confirmation_id="p-1", event_id="e1",
                       title="外发前，需要你许可")
    assert payload["type"] == "attention_required"
    assert payload["level"] == "high"
    assert payload["kind"] == "permission"
    assert payload["pending_action_id"] == "p-1"


def test_a_question_is_only_a_quiet_nudge():
    payload = envelope(action="ask_user", reply="你指的是哪一条提醒？", event_id="e2")
    assert payload["level"] == "low"
    assert payload["kind"] == "question"
    # A question must never take the screen: the wearer decides when to answer.
    assert "pending_action_id" not in payload


def test_urgent_reply_is_loud_but_not_a_permission():
    high = envelope(action="reply", reply="门没关", event_id="e3", urgency=5)
    assert high["level"] == "high"
    assert high["kind"] == "notice"
    normal = envelope(action="reply", reply="好的", event_id="e4", urgency=1)
    assert normal is None


def test_ordinary_actions_never_interrupt():
    for action in ("reply", "remember", "create_task", "suggest", "execute"):
        assert envelope(action=action, reply="x") is None


def test_confirm_without_a_pending_action_does_not_wake_the_screen():
    # Nothing to open means nothing worth waking someone for.
    assert envelope(action="confirm", confirmation_id=None) is None
