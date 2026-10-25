"""Failure is an outcome, not a question — and content is never silently dropped.

Both rules come from the v1 production database: a broken run was reported as
"我暂时无法完成这次信息查询，请稍后再试。" and parked in conversations as a
pending question that nobody could answer, and spoken homework was filed as
ignored.
"""

import json

import pytest

from app.activity import project_event
from app.db import Database
from app.domain import (
    Context,
    DecisionAction,
    Event,
    EventSource,
    EventType,
    PolicyDecision,
    UserProfile,
)
from app.agents import AgentRegistry
from app.orchestrator import _failure_reply, coerce_ignored_decision
from app.service import JarvisService


def _context(text="今天的作业是同步导练 38 页"):
    event = Event(
        event_id="ev-1",
        source=EventSource.MICROPHONE,
        event_type=EventType.SPEECH,
        timestamp=None,
        content=text,
    )
    return Context(event=event, user=UserProfile(user_id="u1"))


def test_failure_reply_names_the_cause():
    assert "额度" in _failure_reply("LLM tool call failed with 402: {\"error\"")
    assert "鉴权" in _failure_reply("401 Unauthorized")
    assert "超时" in _failure_reply("timeout while calling model")
    assert "连接" in _failure_reply("connection reset by peer")
    unknown = _failure_reply("weird internal error xyz")
    assert "没有完成" in unknown
    # The old canned phrasing must never come back.
    for cause in ("402", "timeout", "unknown"):
        assert "请稍后再试" not in _failure_reply(cause)


def test_model_cannot_drop_content():
    """A model that answers 'ignore' gets captured instead of discarded."""
    coerced = coerce_ignored_decision(
        {"action": "ignore", "reply": "", "reason": "ambient_or_incomplete"},
        "今天的作业是同步导练 38 页",
    )
    assert coerced["action"] == "remember"
    assert "同步导练" in coerced["memory_content"]
    assert coerced["reason"].startswith("coerced_from_ignore:")

    # Already-actionable decisions are left alone.
    untouched = {"action": "reply", "reply": "好的"}
    assert coerce_ignored_decision(untouched, "hi") == untouched


def test_prompt_no_longer_offers_ignore():
    source = open("app/orchestrator.py", encoding="utf-8").read()
    assert "action=ignore" not in source, "the prompt must not teach the model to drop content"
    assert "ignore、reply" not in source, "ignore must not be listed as an allowed action"


def test_failed_decision_projects_as_failed():
    event = {
        "id": "e1", "source": "microphone", "event_type": "speech",
        "content": "帮我查一下天气", "context": "{}",
        "created_at": "2026-10-24T09:00:00+08:00",
        "processed_at": "2026-10-24T09:00:05+08:00", "decision_action": "reply",
    }
    audits = [{
        "id": 1, "action": "event_result", "created_at": "2026-10-24T09:00:05+08:00",
        "detail": json.dumps({
            "action": "reply",
            "reply": "上游模型额度不足，这次没有完成。",
            "failed": True,
        }, ensure_ascii=False),
    }]
    item = project_event(event, audits, [], [], {})
    assert item["status"] == "failed", "a broken run must never read as completed"
    assert "没有完成" in item["title"]


def test_failure_does_not_park_a_pending_question(tmp_path):
    db = Database(str(tmp_path / "conv.db"))
    db.init()
    service = JarvisService(db)

    service.update_conversation_from_result(
        "watch-1", "watch-1",
        {"action": "reply", "reply": "上游模型额度不足，这次没有完成。", "failed": True},
    )
    row = db.fetch_one("SELECT status, pending_question FROM conversations WHERE id='watch-1'")
    assert row is not None
    assert row["status"] == "closed"
    assert row["pending_question"] is None


def test_ordinary_question_still_parks(tmp_path):
    db = Database(str(tmp_path / "conv2.db"))
    db.init()
    service = JarvisService(db)

    service.update_conversation_from_result(
        "watch-2", "watch-2",
        {"action": "ask_user", "reply": "你指的是哪一条提醒？"},
    )
    row = db.fetch_one("SELECT status, pending_question FROM conversations WHERE id='watch-2'")
    assert row["status"] == "active"
    assert row["pending_question"] == "你指的是哪一条提醒？"
