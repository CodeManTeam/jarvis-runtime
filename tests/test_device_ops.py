from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import Database
from app.service import JarvisService


@pytest.fixture
def client(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "ops.db"))
    database.init()
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "service", JarvisService(database))
    monkeypatch.setattr(main, "kill_switch", False)
    monkeypatch.setattr(main, "settings", main.settings.__class__(
        **{**main.settings.__dict__, "device_token": ""}
    ))
    with TestClient(main.app) as test_client:
        yield test_client, database


def test_metrics_and_insufficient_data(client):
    api, db = client
    api.post("/api/device/register", json={"device_id": "watch"})
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=12)).isoformat()
    end = now.isoformat()
    db.execute(
        "INSERT INTO device_sessions(id,device_id,started_at,ended_at,last_roundtrip_at) VALUES(?,?,?,?,?)",
        ("s1", "watch", start, end, end),
    )
    metrics = api.get("/api/devices/watch/metrics").json()
    assert metrics["online_ratio"] == pytest.approx(0.5, abs=0.002)
    assert metrics["offline_seconds"] == pytest.approx(43200, abs=180)
    api.post("/api/device/register", json={"device_id": "new"})
    missing = api.get("/api/devices/new/metrics").json()
    assert missing["online_ratio"] is None
    assert missing["source"] == "insufficient_data"


def test_sessions_timeline_states(client):
    api, db = client
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    minute = now - timedelta(minutes=2)
    db.execute("INSERT INTO device_sessions VALUES(?,?,?,?,?)",
               ("online", "watch", (minute+timedelta(seconds=5)).isoformat(), None, (minute+timedelta(minutes=1, seconds=1)).isoformat()))
    db.execute("INSERT INTO device_sessions VALUES(?,?,?,?,?)",
               ("degraded", "watch", (minute-timedelta(minutes=1, seconds=55)).isoformat(), minute.isoformat(), (minute-timedelta(minutes=1)).isoformat()))
    old = now-timedelta(minutes=20)
    db.execute("INSERT INTO device_sessions VALUES(?,?,?,?,?)",
               ("offline", "watch", old.isoformat(), (old+timedelta(minutes=1)).isoformat(), old.isoformat()))
    states = {item["minute"]: item["state"] for item in api.get("/api/devices/watch/sessions").json()}
    assert states[minute.strftime("%H:%M")] == "online"
    assert states[(minute-timedelta(minutes=1)).strftime("%H:%M")] == "degraded"
    assert states[(now-timedelta(minutes=10)).strftime("%H:%M")] == "offline"


def test_kill_switch_cancels_and_does_not_restore(client):
    api, db = client
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT INTO device_commands(id,device_id,action,status,created_at) VALUES('cmd','w','vibrate','pending',?)", (now,))
    db.execute("""INSERT INTO permission_grants(id,capability,level,scope_kind,subject,granted_at)
                  VALUES('grant','x','once','none','user',?)""", (now,))
    assert api.post("/api/runtime/kill-switch", json={"on": True}).json() == {"on": True}
    assert db.fetch_one("SELECT status FROM device_commands WHERE id='cmd'")["status"] == "expired"
    grant = db.fetch_one("SELECT revoked_reason FROM permission_grants WHERE id='grant'")
    assert grant["revoked_reason"] == "kill_switch"
    assert db.fetch_one("SELECT id FROM audit_log WHERE action='kill_switch_on'")
    api.post("/api/runtime/kill-switch", json={"on": False})
    assert db.fetch_one("SELECT status FROM device_commands WHERE id='cmd'")["status"] == "expired"
    assert db.fetch_one("SELECT revoked_at FROM permission_grants WHERE id='grant'")["revoked_at"]


@pytest.mark.parametrize("path", [
    "/api/devices/watch/upgrade",
    "/api/devices/watch/config",
    "/api/rce/request",
])
def test_unimplemented_endpoints_return_501(client, path):
    api, _ = client
    response = api.post(path, json={})
    assert response.status_code == 501
    assert "未接入" in response.json()["detail"]
