import asyncio
from types import SimpleNamespace

import pytest

import app.usage as usage_module
from app.db import Database
from app.service import JarvisService
from app.usage import UsageLimitExceeded, normalize_datetime, reserve_usage


def test_normalize_datetime_uses_user_timezone() -> None:
    assert normalize_datetime(
        "2026-10-06T09:00:00",
        "Asia/Shanghai",
    ) == "2026-10-06T01:00:00+00:00"


def test_usage_reservation_is_rejected_before_limit(tmp_path, monkeypatch) -> None:
    database = Database(str(tmp_path / "usage.db"))
    database.init()
    monkeypatch.setattr(
        usage_module,
        "settings",
        SimpleNamespace(
            user_timezone="Asia/Shanghai",
            daily_token_limit=10,
            daily_asr_seconds_limit=5,
        ),
    )
    reservation = reserve_usage(database, input_tokens=6)
    with pytest.raises(UsageLimitExceeded):
        reserve_usage(database, input_tokens=5)
    usage = usage_module.usage_snapshot(database)
    assert usage["token_remaining"] == 4
    usage_module.release_usage(database, reservation)
    assert usage_module.usage_snapshot(database)["token_remaining"] == 10


def test_confirmed_capability_executes_original_action(tmp_path) -> None:
    database = Database(str(tmp_path / "confirmation.db"))
    database.init()
    service = JarvisService(database)
    service.user.permissions.add("devices.write")
    service.register_device("watch-1", "Watch", "android")
    pushed = []

    async def push(device_id, payload):
        pushed.append((device_id, payload))
        return True

    service.set_push_callback(push)
    pending = service.save_pending_action(
        "event-1",
        "execute",
        "让手表震动",
        None,
        "device",
        "vibrate",
        {"device_id": "watch-1"},
        "watch-1",
        None,
    )

    result = asyncio.run(service.async_confirm_pending_action(pending["id"]))

    assert result is not None
    assert result["dispatched"] is True
    assert result["capability"] == "vibrate"
    assert database.fetch_one(
        "SELECT id FROM pending_actions WHERE id = ?", (pending["id"],)
    ) is None
    assert pushed[0][1]["action"] == "vibrate"

