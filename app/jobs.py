from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .db import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, database: Database, root: str | Path | None = None,
                 event_publisher: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.database = database
        self.event_publisher = event_publisher
        self.root = Path(root or Path("data") / "jobs")
        self.root.mkdir(parents=True, exist_ok=True)
        self.init()

    def init(self) -> None:
        with self.database.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL,
                    workspace TEXT NOT NULL, steps INTEGER NOT NULL DEFAULT 0,
                    max_steps INTEGER NOT NULL DEFAULT 60, error TEXT, result TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    seq INTEGER NOT NULL, kind TEXT NOT NULL, tool TEXT,
                    detail TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, task_id TEXT,
                    action TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL
                );
                """
            )

    def create(self, goal: str, max_steps: int = 60) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        workspace = self.root / job_id / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        now = _now()
        self.database.execute(
            "INSERT INTO jobs(id,goal,status,workspace,max_steps,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, goal, "queued", str(workspace), max(1, min(int(max_steps), 200)), now, now),
        )
        job = self.get(job_id)
        self._publish_status(job)
        return job  # type: ignore[return-value]

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row:
            row["steps_detail"] = self.steps(job_id)
            row["trajectory"] = self.load_trajectory(job_id)
        return row

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self.get(row["id"]) for row in self.database.fetch_all(
            "SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)
        ) if self.get(row["id"])]

    def approve(self, job_id: str) -> dict[str, Any] | None:
        """Mark a queued job as approved. Nothing runs before this."""
        job = self.get(job_id)
        if job is None:
            return None
        if job["status"] not in {"queued", "failed"}:
            return job
        self.database.execute(
            "UPDATE jobs SET status = 'queued', error = NULL, updated_at = ? WHERE id = ?",
            (_now(), job_id),
        )
        self._publish_status(self.get(job_id))
        return self.get(job_id)

    def _publish_status(self, job: dict[str, Any] | None) -> None:
        if job and self.event_publisher:
            self.event_publisher({"event": "job_status", "job_id": job["id"],
                "goal": job.get("goal"), "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"), "status": job["status"],
                "steps": job.get("steps", 0),
                "max_steps": job.get("max_steps", 0), "error": job.get("error")})
    def set_status(self, job_id: str, status: str, *, error: str | None = None,
                   result: Any = None) -> None:
        self.database.execute(
            "UPDATE jobs SET status=?, error=?, result=?, updated_at=? WHERE id=?",
            (status, error, json.dumps(result, ensure_ascii=False, default=str) if result is not None else None, _now(), job_id),
        )
        self._publish_status(self.get(job_id))

    def audit(self, job_id: str, action: str, detail: Any) -> None:
        self.database.execute(
            "INSERT INTO audit_log(action, detail, created_at) VALUES (?, ?, ?)",
            (action, json.dumps({"job_id": job_id, **(detail if isinstance(detail, dict) else {"detail": detail})}, ensure_ascii=False, default=str), _now()),
        )
        if action in {"job_approved", "job_cancelled"}:
            self._publish_status(self.get(job_id))

    def step(self, job_id: str, seq: int, kind: str, detail: Any,
             status: str = "ok", tool: str | None = None) -> None:
        self.database.execute(
            "INSERT INTO job_steps(job_id,seq,kind,tool,detail,status,created_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, seq, kind, tool, json.dumps(detail, ensure_ascii=False, default=str) if not isinstance(detail, str) else detail, status, _now()),
        )
        self.database.execute("UPDATE jobs SET steps=?, updated_at=? WHERE id=?", (seq, _now(), job_id))

    def steps(self, job_id: str) -> list[dict[str, Any]]:
        return self.database.fetch_all("SELECT * FROM job_steps WHERE job_id=? ORDER BY seq,id", (job_id,))

    def trajectory_path(self, job_id: str) -> Path:
        row = self.database.fetch_one("SELECT workspace FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise KeyError(job_id)
        return Path(row["workspace"]).parent / "trajectory.json"

    def save_trajectory(self, job_id: str, messages: list[dict[str, Any]]) -> None:
        path = self.trajectory_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(messages, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def load_trajectory(self, job_id: str) -> list[dict[str, Any]]:
        path = self.trajectory_path(job_id)
        if not path.exists():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def recover(self) -> list[dict[str, Any]]:
        return [self.get(row["id"]) for row in self.database.fetch_all(
            "SELECT id FROM jobs WHERE status='running' ORDER BY created_at"
        ) if self.get(row["id"])]
