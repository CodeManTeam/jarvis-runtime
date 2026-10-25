from fastapi.testclient import TestClient
from dataclasses import replace

from app import main
from app.db import Database
from app.metrics import RequestMetricsMiddleware, request_stats
from fastapi import FastAPI


def test_request_middleware_records_status_templates_and_skips_console(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "metrics.db"))
    database.init()
    test_app = FastAPI()
    test_app.add_middleware(RequestMetricsMiddleware, database=database)

    @test_app.get("/items/{item_id}")
    async def item(item_id: str):
        return {"item_id": item_id}

    @test_app.get("/failure/{item_id}")
    async def failure(item_id: str):
        from fastapi import HTTPException
        raise HTTPException(500, "failed")

    client = TestClient(test_app)
    client.get("/items/one")
    client.get("/items/two")
    before = request_stats(database)
    assert before["samples"] == 2
    assert before["error_rate"] == 0
    paths = {row["path"] for row in database.fetch_all("SELECT path FROM request_metrics")}
    assert paths == {"/items/{item_id}"}

    database.execute("DELETE FROM request_metrics")
    client.get("/items/ok")
    client.get("/failure/one")
    measured = request_stats(database)
    assert measured["samples"] == 2
    assert measured["error_rate"] == 0.5
    database.execute("DELETE FROM request_metrics")
    client.get("/console/")
    assert request_stats(database)["samples"] == 0


def test_request_stats_empty_database_returns_insufficient_data(tmp_path):
    database = Database(str(tmp_path / "empty.db"))
    database.init()
    stats = request_stats(database)
    assert stats == {
        "p50_ms": None,
        "p95_ms": None,
        "error_rate": None,
        "samples": 0,
        "source": "insufficient_data",
    }


def test_device_metrics_preserves_fields_and_adds_request_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "database", Database(str(tmp_path / "device.db")))
    monkeypatch.setattr(main, "settings", replace(main.settings, device_token="", app_env="development"))
    main.database.init()
    with TestClient(main.app) as client:
        response = client.get("/api/devices/test-device/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert {"online_seconds", "offline_seconds", "online_ratio", "sessions",
            "degraded_minutes", "source", "p50_ms", "p95_ms", "error_rate"} <= payload.keys()
