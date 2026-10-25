from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .db import Database

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT_MS = 30_000.0
SKIPPED_PATHS = ("/console", "/docs", "/redoc", "/openapi.json", "/api/metrics/requests")


class RequestMetricsMiddleware:
    def __init__(self, app: ASGIApp, database: Database) -> None:
        self.app = app
        self.database = database

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_path = scope.get("path", "")
        if any(raw_path == path or raw_path.startswith(path + "/") for path in SKIPPED_PATHS):
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500

        async def capture_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            route = scope.get("route")
            path = getattr(route, "path", None) or raw_path
            now = datetime.now(timezone.utc)
            bucket = now.replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
            try:
                self.database.execute(
                    """INSERT INTO request_metrics
                    (bucket, path, method, status, duration_ms, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (bucket, path, scope.get("method", "GET"), status, duration_ms, now.isoformat()),
                )
            except Exception:
                logger.exception("request metrics write failed")


def request_stats(database: Database, window_hours: int = 24) -> dict[str, Any]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, window_hours))).isoformat()
    rows = database.fetch_all(
        "SELECT status, duration_ms FROM request_metrics WHERE created_at >= ?",
        (cutoff,),
    )
    if not rows:
        return {
            "p50_ms": None,
            "p95_ms": None,
            "error_rate": None,
            "samples": 0,
            "source": "insufficient_data",
        }
    durations = sorted(float(row["duration_ms"]) for row in rows)

    def percentile(fraction: float) -> float:
        index = (len(durations) - 1) * fraction
        lower = int(index)
        upper = min(lower + 1, len(durations) - 1)
        return round(durations[lower] + (durations[upper] - durations[lower]) * (index - lower), 1)

    errors = sum(
        int(row["status"]) >= 500 or float(row["duration_ms"]) >= REQUEST_TIMEOUT_MS
        for row in rows
    )
    return {
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "error_rate": round(errors / len(rows), 3),
        "samples": len(rows),
        "source": "request_metrics",
    }
