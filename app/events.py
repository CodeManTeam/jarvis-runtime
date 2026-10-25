from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from .domain import Event


class EventBus:
    """进程内事件总线；后续可替换为 Redis/NATS 而不改变上层接口。"""

    def __init__(self, max_size: int = 1000) -> None:
        self.queue: asyncio.Queue[tuple[Event, asyncio.Future[Any] | None]] = asyncio.Queue(
            maxsize=max_size
        )

    async def publish(
        self,
        event: Event,
        result: asyncio.Future[Any] | None = None,
    ) -> None:
        await self.queue.put((event, result))

    async def subscribe(self) -> AsyncIterator[tuple[Event, asyncio.Future[Any] | None]]:
        while True:
            yield await self.queue.get()
            self.queue.task_done()


class EventNormalizer:
    async def process(self, event: Event) -> Event:
        if isinstance(event.content, str):
            event.content = re.sub(r"\s+", " ", event.content).strip()
        if event.timestamp.tzinfo is None:
            event.timestamp = event.timestamp.replace(tzinfo=timezone.utc)
        event.context = dict(event.context)
        event.context.setdefault("content_hash", content_hash(event.content))
        event.context.setdefault("normalized_at", datetime.now(timezone.utc).isoformat())
        return event


class RelevanceFilter:
    def __init__(self, database: Any | None = None, min_confidence: float = 0.2) -> None:
        self.database = database
        self.min_confidence = min_confidence

    async def should_process(self, event: Event) -> bool:
        if event.confidence < self.min_confidence:
            return False
        if event.content is None:
            return False
        if isinstance(event.content, str) and not event.content.strip():
            return False
        if self.database and self._is_duplicate(event):
            event.context["ignored_reason"] = "duplicate_event"
            return False
        return True

    def _is_duplicate(self, event: Event) -> bool:
        # Direct user messages are conversational turns, not external signals.
        # Repeating "你好" should create another turn instead of being dropped
        # by the event dedupe window.
        if event.source.value == "user":
            return False
        key = event.context.get("content_hash")
        if not key:
            return False
        row = self.database.fetch_one(
            """
            SELECT id FROM events
            WHERE dedupe_key = ? AND source = ? AND event_type = ?
              AND created_at >= datetime('now', '-45 seconds')
            ORDER BY created_at DESC LIMIT 1
            """,
            (key, event.source.value, event.event_type.value),
        )
        return row is not None


def content_hash(content: Any) -> str:
    if isinstance(content, str):
        normalized = re.sub(r"\s+", " ", content).strip().lower()
    else:
        normalized = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class IgnoreRuleEngine:
    """Persistent, user-controlled event suppression rules."""

    def __init__(self, database: Any) -> None:
        self.database = database

    def list_rules(self, enabled: bool | None = None) -> list[dict[str, Any]]:
        if enabled is None:
            return self.database.fetch_all(
                "SELECT * FROM ignore_rules ORDER BY enabled DESC, updated_at DESC"
            )
        return self.database.fetch_all(
            "SELECT * FROM ignore_rules WHERE enabled = ? ORDER BY updated_at DESC",
            (1 if enabled else 0,),
        )

    def add_rule(
        self,
        name: str,
        pattern: str,
        match_type: str = "contains",
        source: str | None = None,
        event_type: str | None = None,
        device_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        from uuid import uuid4

        timestamp = datetime.now(timezone.utc).isoformat()
        rule_id = str(uuid4())
        self.database.execute(
            """
            INSERT INTO ignore_rules
            (id, name, pattern, match_type, source, event_type, device_id, project_id,
             enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                rule_id, name, pattern, match_type, source, event_type,
                device_id, project_id, timestamp, timestamp,
            ),
        )
        return self.database.fetch_one(
            "SELECT * FROM ignore_rules WHERE id = ?", (rule_id,)
        ) or {}

    def remove_rule(self, rule_id: str) -> bool:
        before = self.database.fetch_one(
            "SELECT id FROM ignore_rules WHERE id = ?", (rule_id,)
        )
        self.database.execute("DELETE FROM ignore_rules WHERE id = ?", (rule_id,))
        return before is not None

    def set_enabled(self, rule_id: str, enabled: bool) -> dict[str, Any] | None:
        self.database.execute(
            "UPDATE ignore_rules SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, datetime.now(timezone.utc).isoformat(), rule_id),
        )
        return self.database.fetch_one(
            "SELECT * FROM ignore_rules WHERE id = ?", (rule_id,)
        )

    def match(self, event: Event) -> dict[str, Any] | None:
        rules = self.list_rules(enabled=True)
        text = str(event.content).lower()
        for rule in rules:
            if rule.get("source") and rule["source"] != event.source.value:
                continue
            if rule.get("event_type") and rule["event_type"] != event.event_type.value:
                continue
            if rule.get("device_id") and rule["device_id"] != event.context.get("device_id"):
                continue
            if rule.get("project_id") and rule["project_id"] != event.context.get("project_id"):
                continue
            pattern = str(rule["pattern"]).lower()
            matched = (
                pattern in text
                if rule["match_type"] == "contains"
                else re.search(pattern, text) is not None
            )
            if matched:
                self.database.execute(
                    """
                    UPDATE ignore_rules
                    SET hit_count = hit_count + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (datetime.now(timezone.utc).isoformat(), rule["id"]),
                )
                return rule
        return None
