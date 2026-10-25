import asyncio
import json

import pytest
from fastapi import HTTPException

from app.activity import ActivityActionError, ActivityActions, ActivityFeed
from app.db import Database
from app.service import JarvisService
from app.domain import Event, EventSource, EventType, PolicyDecision, DecisionAction
from datetime import datetime, timezone


@pytest.fixture
def setup(tmp_path):
    db = Database(str(tmp_path / "activity.db"))
    db.init()
    return db, JarvisService(db), ActivityFeed(db)


def add_event(db, key="e1", action=None, context="{}", created="2026-10-03T00:00:00Z"):
    db.execute(
        "INSERT INTO events (id, source, event_type, content, context, "
        "decision_action, created_at) VALUES (?, 'user', 'text', ?, ?, ?, ?)",
        (key, "周五取快递", context, action, created),
    )


def add_pending(service):
    return service.save_pending_action(
        "e1", "create_task", "周五取快递", None, None, None, {}, None, None
    )


def test_decision_is_not_proof_of_saved_memory(setup):
    db, service, feed = setup
    add_event(db, action="remember")
    assert "已记录" not in feed.get("e1")["title"]
    service.create_memory("周五取快递", source_event_id="e1")
    item = feed.get("e1")
    assert item["title"] == "已记录「周五取快递」"
    assert item["status"] == "completed"


def test_processed_event_without_result_is_failed_not_observed(setup):
    db, service, feed = setup
    add_event(db)
    db.execute(
        "UPDATE events SET processed_at = ? WHERE id = ?",
        ("2026-10-03T00:01:00Z", "e1"),
    )
    item = feed.get("e1")
    assert item["status"] == "failed"
    assert item["title"] == "处理失败：服务没有返回结果"
    assert "Agent错误" in item["tags"]


def test_rejection_updates_same_card_with_history(setup):
    db, service, feed = setup
    add_event(db)
    pending = add_pending(service)
    before = feed.get("e1")
    assert before["available_actions"] == ["accept", "reject"]
    assert service.reject_pending_action(pending["id"])
    after = feed.get("e1")
    assert after["id"] == before["id"]
    assert after["etag"] != before["etag"]
    assert after["status"] == "rejected"
    assert after["available_actions"] == []
    assert len(feed.list()["items"]) == 1


def test_confirmation_links_real_task_and_does_not_claim_completion(setup):
    db, service, feed = setup
    add_event(db)
    pending = add_pending(service)
    task = service.confirm_pending_action(pending["id"])
    assert feed.get("e1")["task_id"] == task["id"]
    assert feed.get("e1")["status"] == "accepted"
    service.complete_task(task["id"])
    assert feed.get("e1")["status"] == "completed"


def test_stable_paging_equal_times_and_older_filtered_matches(setup):
    db, service, feed = setup
    for i in range(205):
        add_event(db, f"e{i:03}", action="ignore" if i > 2 else None)
    first = feed.list(limit=2)
    assert [x["event_id"] for x in first["items"]] == ["e002", "e001"]
    add_event(db, "new", created="2026-10-04T00:00:00Z")
    second = feed.list(limit=2, cursor=first["next_cursor"])
    assert [x["event_id"] for x in second["items"]] == ["e000"]
    assert second["next_cursor"] is None
    assert len(feed.list(status="ignored", limit=200)["items"]) == 200


def test_filters_json_and_expiration(setup):
    db, service, feed = setup
    add_event(db, context=json.dumps({"device_id": "watch", "project_id": "p1"}))
    add_event(db, "bad", context="[]")
    add_event(db, "malformed", context="{")
    assert len(feed.list(device_id="watch", project_id="p1")["items"]) == 1
    pending = add_pending(service)
    db.execute("UPDATE pending_actions SET expires_at='2000-01-01' WHERE id=?", (pending["id"],))
    assert feed.get("e1")["status"] == "expired"
    assert feed.get("e1")["available_actions"] == []
    assert len(feed.list()["items"]) == 3


@pytest.mark.parametrize("cursor", ["bad", "W10=", "bnVsbA==", "x" * 2049])
def test_invalid_cursor(setup, cursor):
    with pytest.raises(ValueError):
        setup[2].list(cursor=cursor)


def test_result_persisted_for_direct_orchestrator_path(setup):
    db, service, feed = setup
    add_event(db)
    event = Event("e1", EventSource.USER, EventType.TEXT, datetime.now(timezone.utc), "你好")
    service.orchestrator.result(event, PolicyDecision(DecisionAction.REPLY, reply="你好呀"))
    assert feed.get("e1")["title"] == "你好呀"
    assert feed.get("e1")["status"] == "completed"


def test_api_contract_errors_and_details(setup, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "database", setup[0])
    add_event(setup[0], "timer:test")
    assert asyncio.run(main.list_activities())["items"]
    assert asyncio.run(main.get_activity("timer:test"))["item"]["event_id"] == "timer:test"
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.list_activities(cursor="bad"))
    assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.get_activity("absent"))
    assert error.value.status_code == 404


def test_activity_actions_return_updated_card_and_are_idempotent(setup):
    db, service, feed = setup
    add_event(db)
    actions = ActivityActions(db, service, feed)
    first = actions.apply("e1", {
        "action": "modify",
        "content": "周五下午取快递",
        "revision": 0,
        "idempotency_key": "modify-1",
    })
    assert first["activity"]["content"] == "周五下午取快递"
    assert first["activity"]["revision"] == 1
    replay = actions.apply("e1", {
        "action": "modify",
        "content": "另一条内容",
        "revision": 0,
        "idempotency_key": "modify-1",
    })
    assert replay["idempotent_replay"] is True
    assert replay["activity"]["content"] == "周五下午取快递"
    assert len(db.fetch_all(
        "SELECT * FROM audit_log WHERE event_id = 'e1' "
        "AND action = 'activity_action'"
    )) == 1


def test_activity_actions_reject_updates_same_card_and_undo_restores_pending(setup):
    db, service, feed = setup
    add_event(db)
    pending = add_pending(service)
    actions = ActivityActions(db, service, feed)
    rejected = actions.apply("e1", {
        "action": "reject",
        "revision": feed.get("e1")["revision"],
        "idempotency_key": "reject-1",
    })
    assert rejected["activity"]["status"] == "rejected"
    assert rejected["activity"]["event_id"] == "e1"
    restored = actions.apply("e1", {
        "action": "undo",
        "revision": rejected["activity"]["revision"],
        "idempotency_key": "undo-1",
    })
    assert restored["activity"]["status"] == "waiting_user"
    assert restored["activity"]["pending_action_id"] == pending["id"]


def test_activity_accept_returns_updated_activity_with_task(setup):
    db, service, feed = setup
    add_event(db)
    add_pending(service)
    actions = ActivityActions(db, service, feed)
    result = actions.apply("e1", {
        "action": "accept",
        "revision": feed.get("e1")["revision"],
        "idempotency_key": "accept-1",
    })
    assert result["activity"]["status"] == "accepted"
    assert result["activity"]["task_id"]
    assert result["activity"]["available_actions"] == ["modify", "delete", "undo"]


def test_activity_ignore_keeps_same_event_out_of_default_feed(setup):
    db, service, feed = setup
    add_event(db)
    actions = ActivityActions(db, service, feed)
    result = actions.apply("e1", {
        "action": "ignore",
        "revision": feed.get("e1")["revision"],
        "idempotency_key": "ignore-1",
    })
    assert result["activity"]["status"] == "ignored"
    assert result["activity"]["event_id"] == "e1"
    assert feed.list()["items"] == []
    visible = feed.list(include_ignored=True, tag="Agent忽略")
    assert visible["items"][0]["tags"] == ["Agent忽略"]


def test_activity_tags_and_query_find_prefiltered_microphone_events(setup):
    db, service, feed = setup
    add_event(
        db,
        "mic-1",
        action="ignore",
        context=json.dumps({"device_id": "watch-1"}),
    )
    db.execute(
        "INSERT INTO audit_log (event_id, action, detail, created_at) "
        "VALUES (?, 'speech_prefilter_ignored', ?, ?)",
        ("mic-1", '{"reason":"ambient_or_incomplete"}', "2026-10-03T00:01:00Z"),
    )
    db.execute("UPDATE events SET source='microphone', event_type='speech' WHERE id='mic-1'")
    result = feed.list(
        include_ignored=True,
        query="周五取快递",
        tag="预筛选忽略",
    )
    assert result["items"][0]["event_id"] == "mic-1"
    assert set(result["items"][0]["tags"]) == {"麦克风", "ASR", "预筛选忽略"}


def test_prefiltered_microphone_is_persisted_without_agent_call(setup):
    db, service, feed = setup
    event_id = service.record_prefiltered_microphone(
        "watch-1", "什么情况啊", "speech-1"
    )
    service.audit(event_id, None, "speech_prefilter_ignored", {
        "reason": "ambient_or_incomplete",
    })
    item = feed.list(include_ignored=True, tag="预筛选忽略")["items"][0]
    assert item["event_id"] == event_id
    assert item["status"] == "ignored"
    assert item["content"] == "什么情况啊"


def test_activity_delete_is_tombstone_and_undo_restores_original_event(setup):
    db, service, feed = setup
    add_event(db)
    actions = ActivityActions(db, service, feed)
    deleted = actions.apply("e1", {
        "action": "delete",
        "revision": 0,
        "idempotency_key": "delete-1",
    })
    assert deleted["activity"]["status"] == "ignored"
    assert len(feed.list()["items"]) == 0
    restored = actions.apply("e1", {
        "action": "undo",
        "revision": deleted["activity"]["revision"],
        "idempotency_key": "undo-delete-1",
    })
    assert restored["activity"]["status"] == "observed"
    assert len(feed.list()["items"]) == 1


def test_activity_action_revision_conflict_and_api_route(setup, monkeypatch):
    db, service, feed = setup
    add_event(db)
    actions = ActivityActions(db, service, feed)
    actions.apply("e1", {
        "action": "modify",
        "content": "新内容",
        "idempotency_key": "modify-2",
    })
    with pytest.raises(ActivityActionError) as error:
        actions.apply("e1", {
            "action": "delete",
            "revision": 0,
            "idempotency_key": "delete-2",
        })
    assert error.value.status_code == 409

    from app import main
    monkeypatch.setattr(main, "database", db)
    # The route builds ActivityActions(database, service); without patching the
    # service too, its audit() would write into the real data directory.
    monkeypatch.setattr(main, "service", service)
    result = asyncio.run(main.apply_activity_action("e1", {
        "action": "delete",
        "revision": 1,
        "idempotency_key": "delete-route-1",
    }))
    assert result["activity"]["status"] == "ignored"
