from __future__ import annotations

import sqlite3
import secrets
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    run_at TEXT,
                    source TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    last_error TEXT,
                    notified_at TEXT,
                    confirmed_at TEXT,
                    parent_task_id TEXT,
                    timeout_at TEXT,
                    next_attempt_at TEXT,
                    locked_at TEXT,
                    lock_token TEXT,
                    depends_on TEXT NOT NULL DEFAULT '[]',
                    dead_letter_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    memory_type TEXT NOT NULL DEFAULT 'episodic',
                    project_id TEXT,
                    source_event_id TEXT,
                    importance REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memory_daily_overrides (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    applied_date TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    context TEXT NOT NULL DEFAULT '{}',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    dedupe_key TEXT,
                    decision_action TEXT,
                    processed_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT,
                    task_id TEXT,
                    action TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS permission_grants (
                    id TEXT PRIMARY KEY,
                    capability TEXT NOT NULL,
                    level TEXT NOT NULL,
                    scope_kind TEXT NOT NULL,
                    scope_value TEXT,
                    subject TEXT NOT NULL DEFAULT 'user',
                    granted_at TEXT NOT NULL,
                    expires_at TEXT,
                    single_use INTEGER NOT NULL DEFAULT 1,
                    consumed_at TEXT,
                    consumed_by TEXT,
                    revoked_at TEXT,
                    revoked_reason TEXT,
                    parent_grant_id TEXT
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    content_type TEXT,
                    bytes INTEGER NOT NULL DEFAULT 0,
                    chars INTEGER NOT NULL DEFAULT 0,
                    note TEXT,
                    event_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    bytes INTEGER NOT NULL DEFAULT 0,
                    event_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS permission_matrix_overrides (
                    capability TEXT PRIMARY KEY,
                    level TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    battery_level INTEGER,
                    network_type TEXT,
                    app_version TEXT,
                    storage_free_mb INTEGER,
                    charging INTEGER,
                    status TEXT NOT NULL DEFAULT 'active',
                    tts_enabled INTEGER NOT NULL DEFAULT 1,
                    tts_ready INTEGER NOT NULL DEFAULT 0,
                    tts_error TEXT,
                    last_sequence INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    goal TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ignore_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    match_type TEXT NOT NULL DEFAULT 'contains',
                    source TEXT,
                    event_type TEXT,
                    device_id TEXT,
                    project_id TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_actions (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    title TEXT,
                    run_at TEXT,
                    target_agent TEXT,
                    capability TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    device_id TEXT,
                    project_id TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    device_id TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    pending_question TEXT,
                    pending_action_id TEXT,
                    context TEXT NOT NULL DEFAULT '{}',
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_commands (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    delivered_at TEXT,
                    acknowledged_at TEXT
                );
                CREATE TABLE IF NOT EXISTS usage_daily (
                    day TEXT PRIMARY KEY,
                    asr_seconds REAL NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    tts_characters INTEGER NOT NULL DEFAULT 0,
                    reserved_asr_seconds REAL NOT NULL DEFAULT 0,
                    reserved_tokens INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bucket TEXT NOT NULL,
                    path TEXT NOT NULL,
                    method TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    duration_ms REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_request_metrics_bucket
                ON request_metrics(bucket, path);
                """
            )
            self._add_column_if_missing(
                connection, "tasks", "retry_count", "INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column_if_missing(
                connection, "tasks", "max_retries", "INTEGER NOT NULL DEFAULT 3"
            )
            self._add_column_if_missing(
                connection, "memories", "importance", "REAL NOT NULL DEFAULT 0.5"
            )
            for column, definition in (
                ("layer", "TEXT NOT NULL DEFAULT 'short'"),
                ("status", "TEXT NOT NULL DEFAULT 'confirmed'"),
                ("confidence", "REAL"),
                ("valid_from", "TEXT"),
                ("valid_to", "TEXT"),
                ("superseded_by", "TEXT"),
                ("revision", "INTEGER NOT NULL DEFAULT 1"),
                ("raw_ref", "TEXT"),
                ("tags", "TEXT NOT NULL DEFAULT '[]'"),
                ("confirmed_by", "TEXT"),
                ("confirmed_at", "TEXT"),
            ):
                self._add_column_if_missing(connection, "memories", column, definition)
            migrated = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'memory_model_d1'"
            ).fetchone()
            if not migrated:
                connection.execute(
                    """UPDATE memories SET layer = CASE memory_type
                    WHEN 'episodic' THEN 'short' WHEN 'fact' THEN 'long'
                    WHEN 'preference' THEN 'long' ELSE 'short' END,
                    status = 'confirmed', revision = 1"""
                )
                counts = {
                    row["layer"]: row["count"]
                    for row in connection.execute(
                        "SELECT layer, COUNT(*) AS count FROM memories GROUP BY layer"
                    ).fetchall()
                }
                import json
                from datetime import datetime, timezone
                connection.execute(
                    "INSERT INTO audit_log(action, detail, created_at) VALUES (?, ?, ?)",
                    ("memory_migrated", json.dumps(counts), datetime.now(timezone.utc).isoformat()),
                )
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('memory_model_d1', '1')"
                )
            for column, definition in (
                ("last_error", "TEXT"),
                ("notified_at", "TEXT"),
                ("confirmed_at", "TEXT"),
                ("parent_task_id", "TEXT"),
                ("timeout_at", "TEXT"),
                ("next_attempt_at", "TEXT"),
                ("locked_at", "TEXT"),
                ("lock_token", "TEXT"),
                ("depends_on", "TEXT NOT NULL DEFAULT '[]'"),
                ("dead_letter_at", "TEXT"),
            ):
                self._add_column_if_missing(connection, "tasks", column, definition)
            for column, definition in (
                ("dedupe_key", "TEXT"),
                ("decision_action", "TEXT"),
                ("processed_at", "TEXT"),
            ):
                self._add_column_if_missing(connection, "events", column, definition)
            for column, definition in (
                ("reserved_asr_seconds", "REAL NOT NULL DEFAULT 0"),
                ("reserved_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ):
                self._add_column_if_missing(connection, "usage_daily", column, definition)
            for column, definition in (
                ("battery_level", "INTEGER"),
                ("network_type", "TEXT"),
                ("app_version", "TEXT"),
                ("storage_free_mb", "INTEGER"),
                ("charging", "INTEGER"),
                ("status", "TEXT NOT NULL DEFAULT 'active'"),
                ("tts_enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("tts_ready", "INTEGER NOT NULL DEFAULT 0"),
                ("tts_error", "TEXT"),
                ("last_sequence", "INTEGER NOT NULL DEFAULT 0"),
                ("model", "TEXT"),
                ("battery_mah", "INTEGER"),
                ("current_ma", "REAL"),
                ("cache_limit", "INTEGER"),
                ("input_caps", "TEXT"),
                ("sensing_policy", "TEXT"),
                ("cache_size", "INTEGER"),
                ("queue_size", "INTEGER"),
                ("sensing_state", "TEXT"),
            ):
                self._add_column_if_missing(connection, "devices", column, definition)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS device_sessions (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    last_roundtrip_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_device
                ON device_sessions(device_id, started_at);
                """
            )
            self._add_column_if_missing(
                connection, "device_commands", "expires_at", "TEXT"
            )
            self._add_column_if_missing(connection, "pending_actions", "scope_kind", "TEXT")
            self._add_column_if_missing(connection, "pending_actions", "scope_value", "TEXT")
            for column, definition in (
                ("session_id", "TEXT"),
                ("one_liner", "TEXT NOT NULL DEFAULT ''"),
                ("context_snapshot", "TEXT NOT NULL DEFAULT '{}'"),
                ("origin", "TEXT NOT NULL DEFAULT 'user'"),
                ("blocked_reason", "TEXT"),
                ("progress", "INTEGER NOT NULL DEFAULT 0"),
                ("revision", "INTEGER NOT NULL DEFAULT 0"),
                ("last_activity_at", "TEXT"),
                ("closed_at", "TEXT"),
            ):
                self._add_column_if_missing(connection, "projects", column, definition)
            self._add_column_if_missing(connection, "artifacts", "short_code", "TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_short_code ON artifacts(short_code)"
            )
            alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
            for row in connection.execute(
                "SELECT id FROM artifacts WHERE short_code IS NULL"
            ).fetchall():
                for _ in range(10):
                    code = "".join(secrets.choice(alphabet) for _ in range(4))
                    try:
                        connection.execute(
                            "UPDATE artifacts SET short_code=? WHERE id=? AND short_code IS NULL",
                            (code, row["id"]),
                        )
                        break
                    except sqlite3.IntegrityError:
                        continue
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_dedupe ON events(dedupe_key, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_event_order "
                "ON events(COALESCE(julianday(created_at), 0) DESC, id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event_id, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_grants_lookup "
                "ON permission_grants(capability, scope_kind, scope_value)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pending_event ON pending_actions(event_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_event ON memories(source_event_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_model "
                "ON memories(status, layer, project_id, importance DESC, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status_run_at ON tasks(status, run_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_attempt ON tasks(status, next_attempt_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_expiry ON conversations(status, expires_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_device_commands_pending ON device_commands(device_id, status, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status, updated_at DESC)"
            )

    @staticmethod
    def _add_column_if_missing(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def fetch_all(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, args).fetchall()]

    def fetch_one(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, args).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> None:
        with self.connect() as connection:
            connection.execute(sql, args)
            connection.commit()
