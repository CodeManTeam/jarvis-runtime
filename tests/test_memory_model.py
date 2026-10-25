import asyncio
import json

from fastapi.testclient import TestClient

from app.db import Database
from app.main import app
from app.memory import SQLiteMemoryStore
from app.service import JarvisService


def make_db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.init()
    return db


def test_forgotten_and_candidate_search_defaults(tmp_path):
    db = make_db(tmp_path)
    service = JarvisService(db)
    forgotten = service.create_memory("hidden forgotten")
    candidate = service.create_memory("hidden candidate", status="candidate")
    store = SQLiteMemoryStore(db)
    asyncio.run(store.forget(forgotten["id"]))
    assert asyncio.run(store.search("hidden")) == []
    assert [m.memory_id for m in asyncio.run(store.search("hidden", include_forgotten=True))] == [forgotten["id"]]
    assert asyncio.run(store.search("candidate")) == []


def test_revision_keeps_row_and_audits_before_value(tmp_path):
    db = make_db(tmp_path)
    service = JarvisService(db)
    item = service.create_memory("old text")
    updated = service.revise_memory(item["id"], "new text")
    assert updated["revision"] == 2
    record = db.fetch_one("SELECT detail FROM audit_log WHERE action='memory_revised'")
    assert json.loads(record["detail"])["before"] == "old text"


def test_migration_is_idempotent(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    with db.connect() as connection:
        connection.execute("""CREATE TABLE memories (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, memory_type TEXT NOT NULL DEFAULT 'episodic',
            project_id TEXT, source_event_id TEXT, importance REAL NOT NULL DEFAULT 0.5,
            created_at TEXT NOT NULL)""")
        connection.execute("INSERT INTO memories(id,content,memory_type,created_at) VALUES('m','x','fact','2026-01-01')")
    db.init()
    first = db.fetch_one("SELECT layer, status, revision FROM memories WHERE id='m'")
    count = db.fetch_one("SELECT COUNT(*) AS n FROM audit_log WHERE action='memory_migrated'")["n"]
    db.init()
    assert db.fetch_one("SELECT layer, status, revision FROM memories WHERE id='m'") == first
    assert db.fetch_one("SELECT COUNT(*) AS n FROM audit_log WHERE action='memory_migrated'")["n"] == count == 1
    assert first["layer"] == "long"


def test_apply_today_does_not_create_memory(tmp_path):
    db = make_db(tmp_path)
    service = JarvisService(db)
    item = service.create_memory("today")
    before = db.fetch_one("SELECT COUNT(*) AS n FROM memories")["n"]
    assert service.apply_memory_today(item["id"])["applied"]
    assert db.fetch_one("SELECT COUNT(*) AS n FROM memories")["n"] == before


def test_destroy_validation_and_ordered_receipt(tmp_path, monkeypatch):
    """Destruction must be exercised against a throwaway database.

    The routes close over the app-module `database` singleton, so without this
    patch the test would wipe the developer's real data directory.
    """
    db = make_db(tmp_path)
    service = JarvisService(db)
    service.create_memory("secret body")
    import app.main as main_module

    monkeypatch.setattr(main_module, "database", db)
    monkeypatch.setattr(main_module, "service", JarvisService(db))
    app_database = db
    # Fence: never let an irreversible operation run against the real data dir.
    assert str(tmp_path) in str(app_database.path), "destruction test must use a temp database"
    client = TestClient(app)
    from app.main import verify_token
    app.dependency_overrides[verify_token] = lambda: None
    app_database.init()
    app_database.execute("INSERT OR REPLACE INTO memories(id,content,memory_type,created_at) VALUES('secret','secret body','fact','2026-01-01')")
    phrase = "\u9500\u6bc1\u5168\u90e8"
    assert client.post("/api/data/destroy", json={"confirm_phrase": phrase + "!", "exported": True}).status_code == 400
    assert client.post("/api/data/destroy", json={"confirm_phrase": phrase, "exported": False}).status_code == 400
    # exported=True alone is not evidence: destruction needs a real export token.
    assert client.post("/api/data/destroy", json={"confirm_phrase": phrase, "exported": True}).status_code == 400
    assert client.post(
        "/api/data/destroy",
        json={"confirm_phrase": phrase, "exported": True, "export_token": "forged"},
    ).status_code == 400

    export = client.post("/api/memories/export")
    assert export.status_code == 200
    token = export.json()["token"]
    response = client.post(
        "/api/data/destroy",
        json={"confirm_phrase": phrase, "exported": True, "export_token": token},
    )
    assert response.status_code == 200
    detail = app_database.fetch_one("SELECT detail FROM audit_log WHERE action='data_destroyed'")["detail"]
    assert "secret body" not in detail
    assert json.loads(detail)["counts"]["memories"] >= 1
    # A second destruction with the same token must not be possible.
    assert client.post(
        "/api/data/destroy",
        json={"confirm_phrase": phrase, "exported": True, "export_token": token},
    ).status_code == 400
    app.dependency_overrides.clear()
