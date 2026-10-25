"""Model settings must be changeable from the console and apply without a restart."""

import pytest

from app.db import Database
from app.service import JarvisService


@pytest.fixture
def service(tmp_path):
    return JarvisService(
        Database(str(tmp_path / "model.db")), artifacts_root=tmp_path / "artifacts"
    )


def test_defaults_come_from_the_active_router(service):
    settings = service.model_settings()
    assert settings["primary"]
    assert settings["fallback"] == "" or isinstance(settings["fallback"], str)
    assert settings["timeout_seconds"] > 0


def test_settings_persist_across_a_restart(tmp_path):
    db = Database(str(tmp_path / "model2.db"))
    first = JarvisService(db, artifacts_root=tmp_path / "a1")
    first.update_model_settings({"primary.model": "custom-model-a"})
    again = JarvisService(db, artifacts_root=tmp_path / "a2")
    assert again.model_settings()["primary"] == "custom-model-a"


def test_a_change_applies_immediately_without_rebuilding(service):
    before = service.router.choose("plan").model
    assert before
    service.update_model_settings({"primary.model": "switched-model"})
    assert service.router.choose("plan").model == "switched-model"


def test_empty_fallback_means_no_degradation(service):
    service.update_model_settings({"fallback.model": ""})
    assert service.router.fallback is None
    assert service.model_settings()["fallback"] == ""


def test_a_configured_fallback_appears(service):
    service.update_model_settings({"fallback.model": "backup-model"})
    assert service.router.fallback is not None
    assert service.model_settings()["fallback"] == "backup-model"


def test_timeout_is_applied(service):
    service.update_model_settings({"timeout_seconds": "12"})
    assert service.model_settings()["timeout_seconds"] == 12.0


def test_an_override_does_not_leak_the_key(service):
    payload = service.model_settings()
    assert "api_key" not in payload
    assert "key" not in payload
