from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
import json
from typing import Any

from .db import Database
from .domain import MemoryItem


class MemoryStore:
    async def save(self, item: MemoryItem) -> None:
        raise NotImplementedError

    async def search(
        self,
        query: str,
        project_id: str | None = None,
        limit: int = 20,
        *,
        include_candidates: bool = False,
        include_forgotten: bool = False,
        layer: str | None = None,
        tags: list[str] | None = None,
    ) -> list[MemoryItem]:
        raise NotImplementedError

    async def forget(self, memory_id: str) -> None:
        raise NotImplementedError


class SQLiteMemoryStore(MemoryStore):
    """原型阶段的持久记忆；检索先用 SQLite 文本匹配，后续可换向量检索。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def save(self, item: MemoryItem) -> None:
        self.database.execute(
            """
            INSERT OR REPLACE INTO memories
            (id, content, memory_type, project_id, source_event_id, importance, created_at, layer, status, revision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.memory_id,
                str(item.content),
                item.memory_type,
                item.project_id,
                item.source_event_id,
                item.importance,
                item.created_at.isoformat(),
                item.layer, item.status, item.revision,
            ),
        )

    async def search(
        self,
        query: str,
        project_id: str | None = None,
        limit: int = 20,
        *,
        include_candidates: bool = False,
        include_forgotten: bool = False,
        layer: str | None = None,
        tags: list[str] | None = None,
    ) -> list[MemoryItem]:
        rows = self.database.fetch_all(
            """
            SELECT * FROM memories
            WHERE (? = '' OR content LIKE ?)
              AND (? IS NULL OR project_id = ?)
              AND (? IS NULL OR layer = ?)
              AND (? OR status != 'candidate')
              AND (? OR status != 'forgotten')
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
            """,
            (query, f"%{query}%", project_id, project_id, layer, layer,
             include_candidates, include_forgotten, limit),
        )
        if tags:
            rows = [r for r in rows if set(tags).issubset(set(json.loads(r.get("tags") or "[]")))]
        return [
            MemoryItem(
                memory_id=row["id"],
                memory_type=row["memory_type"],
                content=row["content"],
                created_at=datetime.fromisoformat(row["created_at"]),
                importance=float(row.get("importance") or 0.5),
                project_id=row.get("project_id"),
                source_event_id=row.get("source_event_id"),
                layer=row.get("layer", "short"),
                status=row.get("status", "confirmed"),
                confidence=row.get("confidence"),
                tags=json.loads(row.get("tags") or "[]"),
                revision=int(row.get("revision") or 1),
            )
            for row in rows
        ]

    async def forget(self, memory_id: str) -> None:
        self.database.execute("UPDATE memories SET status = 'forgotten' WHERE id = ?", (memory_id,))

    def accept_candidate(self, memory_id: str) -> bool:
        self.database.execute("UPDATE memories SET status='confirmed', confirmed_by='user', confirmed_at=? WHERE id=? AND status='candidate'",
                              (datetime.now(timezone.utc).isoformat(), memory_id))
        return bool(self.database.fetch_one("SELECT id FROM memories WHERE id=? AND status='confirmed'", (memory_id,)))

    def ignore_candidate(self, memory_id: str) -> bool:
        self.database.execute("UPDATE memories SET status='forgotten' WHERE id=? AND status='candidate'", (memory_id,))
        return bool(self.database.fetch_one("SELECT id FROM memories WHERE id=? AND status='forgotten'", (memory_id,)))

    async def recent(self, limit: int = 20) -> list[MemoryItem]:
        return await self.search("", limit=limit)
