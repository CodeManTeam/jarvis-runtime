"""The permission loop must actually close: request -> confirm -> spend once.

M0.1 built the grant table; M3 mapped real agent capabilities onto the matrix.
These tests cover the two links that made the loop deadlock before: the request
carrying a content hash, and the user's confirmation issuing the grant it spends.
"""

import pytest

from app.db import Database
from app.policy import PermissionUnavailable
from app.service import JarvisService


@pytest.fixture
def service(tmp_path):
    return JarvisService(Database(str(tmp_path / "loop.db")))


def scope_of(service, payload):
    return service.permission_scope_hash("message.send", payload)


def test_message_scope_hash_tracks_content(service):
    base = {"to": "林工", "subject": "提案", "body": "已备好", "attachments": ["a.pdf"]}
    same = dict(base)
    edited_body = {**base, "body": "已备好，另附预算表"}
    edited_to = {**base, "to": "林工（备用）"}

    assert scope_of(service, base) == scope_of(service, same)
    assert scope_of(service, base) != scope_of(service, edited_body)
    assert scope_of(service, base) != scope_of(service, edited_to)


def test_missing_grant_reports_the_content_it_needs(service):
    payload = {"to": "林工", "body": "已备好"}
    with pytest.raises(PermissionUnavailable) as caught:
        service.validate_permission("message.send", payload, "event-1")

    details = caught.value.details
    assert details["capability"] == "message.send"
    assert details["scope_kind"] == "message"
    # The requested scope is the content hash, so an edited draft asks again.
    assert details["scope_value"] == scope_of(service, payload)


def test_confirmation_issues_the_grant_it_spends(service):
    payload = {"to": "林工", "body": "已备好"}
    digest = scope_of(service, payload)

    source = service.authorise_and_consume(
        "message.send", "message", digest, "pending-1"
    )
    assert source == "issued_on_confirmation"

    grant = service.database.fetch_one(
        "SELECT * FROM permission_grants WHERE capability = 'message.send'"
    )
    assert grant["consumed_at"], "the grant issued on confirmation must be spent"
    assert grant["single_use"] == 1

    # The same action may not authorise itself a second time.
    with pytest.raises(PermissionUnavailable):
        service.authorise_and_consume("message.send", "message", digest, "pending-1")
    assert service.database.fetch_one(
        "SELECT 1 FROM audit_log WHERE action = 'permission_reuse_blocked'"
    )


def test_edited_content_cannot_reuse_the_old_grant(service):
    original = {"to": "林工", "body": "已备好"}
    service.authorise_and_consume("message.send", "message", scope_of(service, original), "pending-1")

    edited = {"to": "林工", "body": "已备好，另附预算表"}
    new_digest = scope_of(service, edited)
    assert new_digest != scope_of(service, original)
    with pytest.raises(PermissionUnavailable):
        service.consume_matching_permission("message.send", "message", new_digest, "pending-3")


def test_existing_grant_is_preferred_over_issuing_a_new_one(service):
    payload = {"to": "林工", "body": "已备好"}
    digest = scope_of(service, payload)
    service.create_permission_grant({
        "capability": "message.send",
        "level": "per_action",
        "scope_kind": "message",
        "scope_value": digest,
        "expires_at": "2099-01-01T00:00:00+00:00",
        "single_use": True,
    })
    assert service.authorise_and_consume("message.send", "message", digest, "pending-1") == "existing_grant"
