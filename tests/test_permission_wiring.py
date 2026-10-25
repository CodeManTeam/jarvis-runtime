from datetime import datetime, timedelta, timezone

import pytest

from app.db import Database
from app.policy import matrix_capability
from app.service import JarvisService, PermissionUnavailable


@pytest.fixture
def service(tmp_path):
    return JarvisService(Database(str(tmp_path / "wiring.db")))


def test_aliases_are_narrow():
    assert matrix_capability("reminder", "create") == "reminder.write"
    assert matrix_capability("reminder", "complete") == "reminder.write"
    assert matrix_capability("device", "review.device_script") is None
    assert matrix_capability("reminder", "list") is None


def test_unmapped_capability_is_not_in_matrix(service):
    service.validate_permission("reminder.create", {}, "event")


def test_payload_hash_changes_with_content(service):
    service.set_permission_level("reminder.write", "once")
    first = service.permission_scope_hash("reminder.write", {"text": "A"})
    changed = service.permission_scope_hash("reminder.write", {"text": "B"})
    assert first != changed
    with pytest.raises(PermissionUnavailable):
        service.validate_permission("reminder.write", {"text": "B", "scope_value": first}, "event")


def test_consumption_is_atomic_and_expiry_is_enforced(service):
    scope = "f" * 64
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    grant = service.create_permission_grant({
        "capability": "reminder.write", "level": "per_action",
        "scope_kind": "project", "scope_value": scope, "expires_at": future,
    })
    service.consume_permission_grant(grant["id"], "event-1")
    with pytest.raises(PermissionUnavailable):
        service.consume_permission_grant(grant["id"], "event-2")
    assert service.database.fetch_one(
        "SELECT 1 FROM audit_log WHERE action='permission_reuse_blocked'"
    )

    expired = service.create_permission_grant({
        "capability": "reminder.write", "level": "per_action",
        "scope_kind": "project", "scope_value": "e" * 64,
        "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    })
    with pytest.raises(PermissionUnavailable):
        service.consume_permission_grant(expired["id"], "event-3")
