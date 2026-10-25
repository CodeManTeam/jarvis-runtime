"""ProjectService 状态机、索引视图与 Session 绑定的测试。"""

from __future__ import annotations

import pytest

from app.db import Database
from app.projects import (
    InvalidProjectTransition,
    ProjectService,
    StaleProjectRevision,
    check_transition,
)


@pytest.fixture
def service(tmp_path):
    database = Database(str(tmp_path / "projects.db"))
    database.init()
    return ProjectService(database)


def test_create_user_project_is_active(service):
    project = service.create("改提醒功能", "让提醒避开会议时段")

    assert project["status"] == "active"
    assert project["origin"] == "user"
    assert project["progress"] == 0
    assert project["revision"] == 0
    assert project["one_liner"]
    assert project["session_id"] is None


def test_create_auto_project_is_proposed(service):
    project = service.create("跟踪快递", "等快递到货", origin="auto")

    assert project["status"] == "proposed"
    assert project["origin"] == "auto"


def test_create_rejects_empty_name(service):
    with pytest.raises(ValueError):
        service.create("   ")


def test_context_snapshot_roundtrips(service):
    snapshot = {"device": {"id": "watch-01", "battery": 82}, "mood": "忙"}
    project = service.create("项目", "目标", context_snapshot=snapshot)

    assert project["context_snapshot"] == snapshot


def test_metadata_roundtrips(service):
    project = service.create("项目", metadata={"tag": "ops"})

    assert project["metadata"] == {"tag": "ops"}


def test_metadata_defaults_to_empty_dict(service):
    project = service.create("项目")

    assert project["metadata"] == {}
    assert project["context_snapshot"] == {}


def test_valid_transition_active_to_blocked(service):
    project = service.create("项目", "目标")
    updated = service.update_status(
        project["id"], "blocked", reason="等你确认改策略引擎"
    )

    assert updated["status"] == "blocked"
    assert updated["blocked_reason"] == "等你确认改策略引擎"
    assert updated["revision"] == 1


def test_blocked_requires_reason(service):
    project = service.create("项目")

    with pytest.raises(ValueError):
        service.update_status(project["id"], "blocked")

    with pytest.raises(ValueError):
        service.update_status(project["id"], "blocked", reason="   ")


def test_unblock_clears_blocked_reason(service):
    project = service.create("项目")
    service.update_status(project["id"], "blocked", reason="等确认")
    resumed = service.update_status(project["id"], "active")

    assert resumed["status"] == "active"
    assert resumed["blocked_reason"] is None


def test_invalid_transition_from_terminal_rejected(service):
    project = service.create("项目")
    service.update_status(project["id"], "done")

    with pytest.raises(InvalidProjectTransition):
        service.update_status(project["id"], "active")


def test_terminal_sets_closed_at(service):
    project = service.create("项目")
    done = service.update_status(project["id"], "done")

    assert done["closed_at"] is not None
    assert done["progress"] == 100


def test_proposed_cannot_go_straight_to_done(service):
    project = service.create("提案", origin="auto")

    with pytest.raises(InvalidProjectTransition):
        service.update_status(project["id"], "done")


def test_proposed_to_active_by_confirmation(service):
    project = service.create("提案", origin="auto")
    confirmed = service.update_status(project["id"], "active")

    assert confirmed["status"] == "active"


def test_check_transition_allows_same_status():
    # 幂等：重复设置同一状态不算非法转移
    check_transition("active", "active")


def test_check_transition_reports_allowed_targets():
    with pytest.raises(InvalidProjectTransition) as excinfo:
        check_transition("done", "active")

    assert "终态" in str(excinfo.value)


def test_stale_revision_rejected(service):
    project = service.create("项目")
    service.update_status(project["id"], "paused")

    with pytest.raises(StaleProjectRevision):
        service.update_status(
            project["id"], "active", expect_revision=project["revision"]
        )


def test_matching_revision_accepted(service):
    project = service.create("项目")
    updated = service.update_status(
        project["id"], "paused", expect_revision=0
    )

    assert updated["status"] == "paused"


def test_touch_activity_does_not_change_status(service):
    project = service.create("项目")
    touched = service.touch_activity(
        project["id"], one_liner="已完成分档规则", progress=60
    )

    assert touched["status"] == "active"
    assert touched["one_liner"] == "已完成分档规则"
    assert touched["progress"] == 60
    assert touched["revision"] == 1


def test_touch_activity_unknown_project_returns_none(service):
    assert service.touch_activity("missing") is None


def test_progress_is_clamped(service):
    project = service.create("项目")
    over = service.touch_activity(project["id"], progress=999)
    under = service.touch_activity(project["id"], progress=-5)

    assert over["progress"] == 100
    assert under["progress"] == 0


def test_index_view_excludes_terminal(service):
    active = service.create("进行中")
    done = service.create("已完成")
    service.update_status(done["id"], "done")

    ids = [item["project_id"] for item in service.index_view()]

    assert active["id"] in ids
    assert done["id"] not in ids


def test_index_view_orders_blocked_first(service):
    service.create("普通项目")
    blocked = service.create("阻塞项目")
    service.update_status(blocked["id"], "blocked", reason="等确认")
    proposed = service.create("提案", origin="auto")

    items = service.index_view()

    assert items[0]["project_id"] == blocked["id"]
    assert items[1]["status"] == "active"
    assert items[-1]["project_id"] == proposed["id"]


def test_index_view_respects_limit(service):
    for index in range(5):
        service.create(f"项目 {index}")

    assert len(service.index_view(limit=2)) == 2


def test_index_view_carries_blocked_reason(service):
    project = service.create("项目")
    service.update_status(project["id"], "blocked", reason="缺少权限")

    item = service.index_view()[0]

    assert item["blocked_reason"] == "缺少权限"


def test_render_index_text_empty(service):
    assert service.render_index_text() == "当前没有进行中的项目。"


def test_render_index_text_shows_blocked_reason(service):
    project = service.create("提醒联动")
    service.update_status(project["id"], "blocked", reason="等你确认")

    text = service.render_index_text()

    assert "⛔" in text
    assert "提醒联动" in text
    assert "阻塞：等你确认" in text


def test_render_index_text_tolerates_missing_one_liner(service):
    # 存量项目没有 one_liner 时只显示名称，不得报错
    project = service.create("老项目")
    service.database.execute(
        "UPDATE projects SET one_liner = '' WHERE id = ?", (project["id"],)
    )

    text = service.render_index_text()

    assert "老项目" in text


def test_bind_session_sets_id(service):
    project = service.create("项目")
    bound = service.bind_session(project["id"], "sess-123")

    assert bound["session_id"] == "sess-123"


def test_bind_session_is_idempotent(service):
    project = service.create("项目")
    service.bind_session(project["id"], "sess-123")
    again = service.bind_session(project["id"], "sess-999")

    assert again["session_id"] == "sess-123"


def test_bind_session_rejects_empty(service):
    project = service.create("项目")

    with pytest.raises(ValueError):
        service.bind_session(project["id"], "  ")


def test_bind_session_unknown_project(service):
    with pytest.raises(KeyError):
        service.bind_session("missing", "sess-1")


def test_get_unknown_project_returns_none(service):
    assert service.get("missing") is None


def test_list_filters_by_status(service):
    active = service.create("进行中")
    proposed = service.create("提案", origin="auto")

    active_ids = [item["id"] for item in service.list(status="active")]
    all_ids = [item["id"] for item in service.list()]

    assert active["id"] in active_ids
    assert proposed["id"] not in active_ids
    assert proposed["id"] in all_ids
