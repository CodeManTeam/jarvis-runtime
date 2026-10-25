from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

from .config import settings
from .db import Database


class UsageLimitExceeded(RuntimeError):
    """Raised before an upstream ASR/LLM request would exceed its daily budget."""


def _zone(timezone_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or "Asia/Shanghai")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def current_day(timezone_name: str | None = None) -> str:
    return datetime.now(_zone(timezone_name or settings.user_timezone)).date().isoformat()


def normalize_datetime(value: str | None, timezone_name: str | None = None) -> str | None:
    """Normalize user-entered timestamps to timezone-aware UTC ISO strings."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_zone(timezone_name or settings.user_timezone))
    return parsed.astimezone(ZoneInfo("UTC")).isoformat()


def record_usage(
    database: Database,
    *,
    asr_seconds: float = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    tts_characters: int = 0,
    timezone_name: str | None = None,
) -> None:
    day = current_day(timezone_name)
    total = max(0, input_tokens) + max(0, output_tokens)
    database.execute(
        """
        INSERT INTO usage_daily
        (day, asr_seconds, input_tokens, output_tokens, total_tokens,
         tts_characters, reserved_asr_seconds, reserved_tokens, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
        ON CONFLICT(day) DO UPDATE SET
            asr_seconds = asr_seconds + excluded.asr_seconds,
            input_tokens = input_tokens + excluded.input_tokens,
            output_tokens = output_tokens + excluded.output_tokens,
            total_tokens = total_tokens + excluded.total_tokens,
            tts_characters = tts_characters + excluded.tts_characters,
            updated_at = excluded.updated_at
        """,
        (
            day,
            max(0, asr_seconds),
            max(0, input_tokens),
            max(0, output_tokens),
            total,
            max(0, tts_characters),
            datetime.now(_zone(timezone_name or settings.user_timezone)).isoformat(),
        ),
    )


def reserve_usage(
    database: Database,
    *,
    asr_seconds: float = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    """Atomically reserve budget before an upstream request is submitted."""
    asr_seconds = max(0.0, float(asr_seconds))
    tokens = max(0, int(input_tokens)) + max(0, int(output_tokens))
    day = current_day(timezone_name)
    timestamp = datetime.now(_zone(timezone_name or settings.user_timezone)).isoformat()
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT asr_seconds, total_tokens, reserved_asr_seconds, reserved_tokens "
            "FROM usage_daily WHERE day = ?",
            (day,),
        ).fetchone()
        actual_asr = float(row[0]) if row else 0.0
        actual_tokens = int(row[1]) if row else 0
        reserved_asr = float(row[2]) if row and len(row) > 2 else 0.0
        reserved_tokens = int(row[3]) if row and len(row) > 3 else 0
        if actual_asr + reserved_asr + asr_seconds > settings.daily_asr_seconds_limit:
            raise UsageLimitExceeded("daily ASR quota exceeded")
        if actual_tokens + reserved_tokens + tokens > settings.daily_token_limit:
            raise UsageLimitExceeded("daily token quota exceeded")
        connection.execute(
            """
            INSERT INTO usage_daily
            (day, reserved_asr_seconds, reserved_tokens, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                reserved_asr_seconds = reserved_asr_seconds + excluded.reserved_asr_seconds,
                reserved_tokens = reserved_tokens + excluded.reserved_tokens,
                updated_at = excluded.updated_at
            """,
            (day, asr_seconds, tokens, timestamp),
        )
        connection.commit()
    return {
        "day": day,
        "asr_seconds": asr_seconds,
        "tokens": tokens,
        "timezone": timezone_name or settings.user_timezone,
    }


def settle_usage(
    database: Database,
    reservation: dict[str, Any] | None,
    *,
    asr_seconds: float = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    tts_characters: int = 0,
    timezone_name: str | None = None,
) -> None:
    """Convert a reservation into actual usage, releasing any unused amount."""
    if not reservation:
        record_usage(
            database,
            asr_seconds=asr_seconds,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tts_characters=tts_characters,
            timezone_name=timezone_name,
        )
        return
    reserved_asr = float(reservation.get("asr_seconds") or 0)
    reserved_tokens = int(reservation.get("tokens") or 0)
    actual_tokens = max(0, int(input_tokens)) + max(0, int(output_tokens))
    day = str(reservation["day"])
    timestamp = datetime.now(_zone(timezone_name or settings.user_timezone)).isoformat()
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO usage_daily
            (day, asr_seconds, input_tokens, output_tokens, total_tokens,
             tts_characters, reserved_asr_seconds, reserved_tokens, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
            ON CONFLICT(day) DO UPDATE SET
                asr_seconds = asr_seconds + excluded.asr_seconds,
                input_tokens = input_tokens + excluded.input_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                total_tokens = total_tokens + excluded.total_tokens,
                tts_characters = tts_characters + excluded.tts_characters,
                reserved_asr_seconds = MAX(0, reserved_asr_seconds - ?),
                reserved_tokens = MAX(0, reserved_tokens - ?),
                updated_at = excluded.updated_at
            """,
            (
                day,
                max(0, float(asr_seconds)),
                max(0, int(input_tokens)),
                max(0, int(output_tokens)),
                actual_tokens,
                max(0, int(tts_characters)),
                timestamp,
                reserved_asr,
                reserved_tokens,
            ),
        )
        connection.commit()


def release_usage(database: Database, reservation: dict[str, Any] | None) -> None:
    if not reservation:
        return
    day = str(reservation["day"])
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE usage_daily
            SET reserved_asr_seconds = MAX(0, reserved_asr_seconds - ?),
                reserved_tokens = MAX(0, reserved_tokens - ?),
                updated_at = ?
            WHERE day = ?
            """,
            (
                float(reservation.get("asr_seconds") or 0),
                int(reservation.get("tokens") or 0),
                datetime.now(_zone(reservation.get("timezone"))).isoformat(),
                day,
            ),
        )
        connection.commit()


def usage_snapshot(
    database: Database,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    day = current_day(timezone_name)
    row = database.fetch_one(
        "SELECT * FROM usage_daily WHERE day = ?", (day,)
    ) or {
        "day": day,
        "asr_seconds": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "tts_characters": 0,
        "reserved_asr_seconds": 0,
        "reserved_tokens": 0,
    }
    row["token_limit"] = settings.daily_token_limit
    row["asr_seconds_limit"] = settings.daily_asr_seconds_limit
    row["token_remaining"] = max(
        0,
        settings.daily_token_limit
        - int(row["total_tokens"])
        - int(row.get("reserved_tokens") or 0),
    )
    row["asr_seconds_remaining"] = max(
        0,
        settings.daily_asr_seconds_limit
        - float(row["asr_seconds"])
        - float(row.get("reserved_asr_seconds") or 0),
    )
    return row
