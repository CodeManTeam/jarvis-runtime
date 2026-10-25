from datetime import datetime, timedelta, timezone

import pytest

from app.db import Database
from app.domain import PermissionLevel
from app.policy import (
    CAPABILITY_MATRIX,
    LOCKED_CAPABILITIES,
    permission_level_allows,
    permission_level_stricter,
)
from app.service import JarvisService, PermissionDenied, PermissionUnavailable


@pytest.fixture
def service(tmp_path):
    return JarvisService(Database(str(tmp_path / "permissions.db")))


def make_grant(service, **overrides):
    data = {
        "capability": "memory.write_long",
        "level": "once",
        "scope_kind": "project",
        "scope_value": "p1",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    data.update(overrides)
    return service.create_permission_grant(data)


def test_second_consumption_is_audited_and_rejected(service):
    grant = make_grant(service)
    service.consume_permission_grant(grant["id"], "event-1")
    with pytest.raises(PermissionUnavailable):
        service.consume_permission_grant(grant["id"], "event-2")
    assert service.database.fetch_one(
        "SELECT action FROM audit_log WHERE action='permission_reuse_blocked'"
    )


def test_sequential_race_simulation_allows_only_first_consumer(service):
    grant = make_grant(service)
    service.consume_permission_grant(grant["id"], "worker-1")
    with pytest.raises(PermissionUnavailable):
        service.consume_permission_grant(grant["id"], "worker-2")


def test_expired_grant_cannot_be_consumed(service):
    grant = make_grant(
        service, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    )
    with pytest.raises(PermissionUnavailable):
        service.consume_permission_grant(grant["id"], "event")


def test_revoked_grant_cannot_be_consumed(service):
    grant = make_grant(service)
    service.revoke_permission_grant(grant["id"])
    with pytest.raises(PermissionUnavailable):
        service.consume_permission_grant(grant["id"], "event")


@pytest.mark.parametrize("capability", sorted(LOCKED_CAPABILITIES))
def test_locked_capabilities_cannot_be_changed(service, capability):
    with pytest.raises(PermissionDenied):
        service.set_permission_level(capability, "deny")


def test_all_permission_level_pairs_have_fixed_order():
    rank = {"auto": 0, "once": 1, "per_action": 2, "deny": 3}
    levels = list(PermissionLevel)
    for left in levels:
        for right in levels:
            expected = left if rank[left.value] >= rank[right.value] else right
            assert permission_level_stricter(left, right) is expected
            # `deny` is a prohibition, not a strong grant: it authorises nothing.
            if "deny" in (left.value, right.value):
                assert permission_level_allows(left, right) is False
            else:
                assert permission_level_allows(left, right) is (
                    rank[left.value] >= rank[right.value]
                )


def test_auto_grant_never_satisfies_a_stricter_requirement():
    assert not permission_level_allows("auto", "per_action")
    assert not permission_level_allows("auto", "once")
    assert permission_level_allows("per_action", "auto")
    assert permission_level_allows("once", "auto")
    assert not permission_level_allows("deny", "auto")
    assert not permission_level_allows("per_action", "deny")


def test_matrix_override_survives_restart(tmp_path):
    path = str(tmp_path / "restart.db")
    first = JarvisService(Database(path))
    first.set_permission_level("reminder.write", "once")
    # A fresh service stands in for a process restart against the same file.
    second = JarvisService(Database(path))
    assert second.permission_matrix()["capabilities"]["reminder.write"]["level"] == "once"


def test_trusted_mode_creates_legacy_grant_with_audit(service):
    service.validate_permission(
        "reminder.write", {"project_id": "p1"}, "event-1",
        trusted_mode=True, subject="agent:god",
    )
    grant = service.database.fetch_one(
        "SELECT * FROM permission_grants WHERE capability='reminder.write'"
    )
    assert grant and grant["level"] == "auto"
    records = service.database.fetch_all(
        "SELECT detail FROM audit_log WHERE action='permission_granted'"
    )
    assert any("trusted_mode_legacy" in row["detail"] for row in records)


def test_matrix_contains_the_spec_capabilities():
    """Capability packs may add entries, so this is a superset check.

    What must stay true is that every hand-written capability from the spec is
    still present with its documented default; a pack can only *add*.
    """
    core = {
        "reminder.write", "memory.organize_short", "memory.write_long",
        "reminder.adjust_today", "file.read", "message.send", "payment.execute",
        "device.configure", "device.self_iterate", "device.remote_exec",
        "escalate.read", "escalate.write",
    }
    assert core <= set(CAPABILITY_MATRIX)
    assert CAPABILITY_MATRIX["message.send"]["default"] == "per_action"
