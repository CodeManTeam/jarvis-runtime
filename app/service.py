from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .agents import (
    AgentRegistry,
    CalendarAgent,
    DeviceAgent,
    LocationAgent,
    NotificationAgent,
    ReminderAgent,
)
from .config import settings
from .db import Database
from .domain import (
    DecisionAction,
    Event,
    EventSource,
    EventType,
    TaskItem,
    UserProfile,
)
from .memory import SQLiteMemoryStore
from .orchestrator import JarvisOrchestrator
from .policy import (
    CAPABILITY_MATRIX,
    LOCKED_CAPABILITIES,
    PermissionDenied,
    PermissionErrorBase,
    PermissionUnavailable,
)
from .projects import ProjectService
from .runtime import AgentRun, EventRuntime, RunResult
from .asr import ASRError, RealtimeASRSession
from .tts import TTSError, edge_audio
from .usage import (
    UsageLimitExceeded,
    normalize_datetime,
    record_usage,
    release_usage,
    reserve_usage,
    settle_usage,
    usage_snapshot,
)
from .llm import set_router, set_usage_budget, set_usage_recorder
from .routing import build_default_router, current_event
from . import escalate
from .artifacts import ArtifactError, ArtifactStore
from .uploads import UploadRefused, UploadStore
from .capabilities import capability_descriptions, capability_tools
from .tools import ToolDefinition, ToolRegistry


PushCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]
logger = logging.getLogger(__name__)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


CONVERSATION_TTL = timedelta(hours=2)
DEVICE_COMMAND_TTL = timedelta(minutes=2)


def split_tts_chunks(text: str, *, max_chars: int = 42) -> list[str]:
    """Split speech into short segments suitable for a low-power watch."""
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    sentences = re.split(r"(?<=[。！？；.!?;])\s*", normalized)
    chunks: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        while len(sentence) > max_chars:
            cut = max(
                sentence.rfind("，", 0, max_chars + 1),
                sentence.rfind(",", 0, max_chars + 1),
                sentence.rfind(" ", 0, max_chars + 1),
            )
            if cut < max_chars // 2:
                cut = max_chars
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            chunks.append(sentence)
    return chunks


def should_auto_speak(result: dict[str, Any]) -> bool:
    """Audio is opt-in: only an interruption-worthy event speaks on its own."""
    if result.get("speak") is True:
        return True
    try:
        return int(result.get("urgency") or 0) >= 4
    except (TypeError, ValueError):
        return False


class JarvisService:
    """应用服务层。

    HTTP、ASR 和 WebSocket 都从这里进入同一个 Orchestrator。
    这里不理解自然语言，只提供持久化和兼容 API。
    """

    def __init__(self, database: Database, *, artifacts_root: str | Path | None = None) -> None:
        self.database = database
        self.database.init()
        set_usage_recorder(
            lambda input_tokens, output_tokens: record_usage(
                self.database,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
        self.push_callback: PushCallback | None = None
        self.event_publisher: Callable[[dict[str, Any]], None] | None = None
        self._event_worker_active = False
        self._speech_sessions: dict[str, RealtimeASRSession] = {}
        self._speech_partial_tasks: dict[str, asyncio.Task[None]] = {}
        self._tts_locks: dict[str, asyncio.Lock] = {}
        trusted_mode = settings.trust_mode in {"personal", "trusted", "full"}
        self.user = UserProfile(
            user_id="default",
            timezone=settings.user_timezone,
            permissions=(
                {
                    "tasks.write",
                    "devices.write",
                    "calendar.read",
                    "location.read",
                }
                if settings.personal_mode
                else set()
            ),
            preferences={"trusted_mode": trusted_mode},
            interruption_policy={"do_not_disturb": False},
        )
        set_usage_budget(
            lambda input_tokens, output_tokens: reserve_usage(
                self.database,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                timezone_name=self.user.timezone,
            ),
            lambda reservation, input_tokens, output_tokens: settle_usage(
                self.database,
                reservation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                timezone_name=self.user.timezone,
            ),
            lambda reservation: release_usage(self.database, reservation),
        )
        self.memory = SQLiteMemoryStore(database)
        self.projects = ProjectService(database)
        self.agents = AgentRegistry()
        self.agents.register(
            ReminderAgent(
                create_task=self.create_agent_task,
                complete_task=self.complete_task,
                cancel_task=self.cancel_task,
            )
        )
        self.agents.register(
            DeviceAgent(
                get_device=lambda device_id: self.database.fetch_one(
                    "SELECT * FROM devices WHERE id = ?", (device_id,)
                ),
                send_command=self.queue_device_command,
            )
        )
        self.agents.register(NotificationAgent())
        self.agents.register(CalendarAgent())
        self.agents.register(LocationAgent())
        self.tools = self._build_tool_registry()
        self.orchestrator = JarvisOrchestrator(
            database=database,
            memory=self.memory,
            agents=self.agents,
            user=self.user,
            tools=self.tools,
        )
        self.orchestrator.policy.permission_service = self
        self.orchestrator.classifier.permission_service = self
        # Degradation must reach the audit trail so the UI can show what was
        # lost. The router only ever narrows capability, never permission.
        self.artifacts = ArtifactStore(
            database,
            Path(artifacts_root) if artifacts_root is not None
            else Path(settings.database_path).resolve().parent / "artifacts",
        )
        self.uploads = UploadStore(
            database,
            (Path(settings.database_path).resolve().parent / "uploads"),
        )
        self.router = build_default_router()
        self.router.load_overrides({
            row["key"]: row["value"]
            for row in self.database.fetch_all("SELECT key, value FROM model_settings")
        })
        self.router.on_degradation = (
            lambda event: self.audit(current_event.get(), None, "degradation", event)
        )
        set_router(self.router)
        self._permission_levels: dict[str, str] = self._load_permission_levels()
        self.event_runtime = EventRuntime(self._handle_runtime_event)

    def model_settings(self) -> dict[str, Any]:
        values = {row["key"]: row["value"] for row in self.database.fetch_all("SELECT key, value FROM model_settings")}
        return {
            "primary": values.get("primary.model", self.router.primary.model),
            "fallback": values.get("fallback.model", self.router.fallback.model if self.router.fallback else ""),
            "timeout_seconds": float(values.get("timeout_seconds", self.router.primary.timeout_seconds)),
            "provider_base_url": values.get("provider_base_url", self.router.primary.base_url),
        }

    def update_model_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        for key, value in values.items():
            self.database.execute(
                "INSERT INTO model_settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), now()),
            )
        self.router.load_overrides({
            row["key"]: row["value"] for row in self.database.fetch_all("SELECT key, value FROM model_settings")
        })
        return self.model_settings()

    def _load_permission_levels(self) -> dict[str, str]:
        """Matrix edits must survive a restart; only locked capabilities are ignored."""
        rows = self.database.fetch_all(
            "SELECT capability, level FROM permission_matrix_overrides"
        )
        loaded = {}
        for row in rows:
            capability = row["capability"]
            spec = CAPABILITY_MATRIX.get(capability)
            if spec is None or capability in LOCKED_CAPABILITIES:
                continue
            if row["level"] in spec["levels"]:
                loaded[capability] = row["level"]
        return loaded

    def permission_matrix(self) -> dict[str, Any]:
        return {"capabilities": {
            name: {**spec, "levels": list(spec["levels"]),
                   "adjustable": name not in LOCKED_CAPABILITIES,
                   "level": self._permission_levels.get(name, spec["default"])}
            for name, spec in CAPABILITY_MATRIX.items()
        }}

    def set_permission_level(self, capability: str, level: str) -> dict[str, Any]:
        spec = CAPABILITY_MATRIX.get(capability)
        if spec is None:
            raise KeyError(capability)
        if capability in LOCKED_CAPABILITIES:
            raise PermissionDenied("Capability level is locked")
        if level not in spec["levels"]:
            raise ValueError("Unsupported level for capability")
        self._permission_levels[capability] = level
        self.database.execute(
            "INSERT INTO permission_matrix_overrides (capability, level, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(capability) DO UPDATE SET "
            "level = excluded.level, updated_at = excluded.updated_at",
            (capability, level, now()),
        )
        self.audit(None, None, "permission_granted", {
            "capability": capability, "level": level, "kind": "matrix_revision",
        })
        return self.permission_matrix()["capabilities"][capability]

    def create_permission_grant(self, data: dict[str, Any]) -> dict[str, Any]:
        grant_id = str(data.get("id") or uuid.uuid4())
        capability = str(data["capability"])
        if capability not in CAPABILITY_MATRIX:
            raise ValueError("Unknown capability")
        level = str(data["level"])
        scope_kind = str(data.get("scope_kind") or "none")
        expires_at = data.get("expires_at")
        if level == "once" and not expires_at:
            raise ValueError("once grants require expires_at")
        timestamp = now()
        self.database.execute(
            """INSERT INTO permission_grants
            (id, capability, level, scope_kind, scope_value, subject, granted_at,
             expires_at, single_use, parent_grant_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (grant_id, capability, level, scope_kind, data.get("scope_value"),
             str(data.get("subject") or "user"), timestamp, expires_at,
             int(data.get("single_use", level in {"once", "per_action"})),
             data.get("parent_grant_id")),
        )
        self.audit(None, None, "permission_granted", {
            "grant_id": grant_id, "capability": capability, "level": level,
            "granted_by": data.get("granted_by", "user"),
        })
        return self.database.fetch_one(
            "SELECT * FROM permission_grants WHERE id = ?", (grant_id,)
        ) or {}

    def consume_permission_grant(self, grant_id: str, consumed_by: str) -> dict[str, Any]:
        timestamp = now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """UPDATE permission_grants SET consumed_at = ?, consumed_by = ?
                WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL
                AND (expires_at IS NULL OR expires_at > ?)""",
                (timestamp, consumed_by, grant_id, timestamp),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT expires_at, revoked_at, consumed_at FROM permission_grants WHERE id = ?",
                    (grant_id,),
                ).fetchone()
                reason = "permission_reuse_blocked" if row and row["consumed_at"] else (
                    "permission_expired" if row and row["expires_at"] else "permission_denied"
                )
                connection.execute(
                    "INSERT INTO audit_log (action, detail, created_at) VALUES (?, ?, ?)",
                    (reason, json.dumps({"grant_id": grant_id}, ensure_ascii=False), timestamp),
                )
                connection.commit()
                raise PermissionUnavailable(reason)
            connection.execute(
                "INSERT INTO audit_log (action, detail, created_at) VALUES (?, ?, ?)",
                ("permission_consumed", json.dumps({"grant_id": grant_id, "consumed_by": consumed_by}), timestamp),
            )
            row = connection.execute(
                "SELECT * FROM permission_grants WHERE id = ?", (grant_id,)
            ).fetchone()
            connection.commit()
            return dict(row)

    def revoke_permission_grant(self, grant_id: str, reason: str = "user_revoked") -> dict[str, Any] | None:
        timestamp = now()
        self.database.execute(
            "UPDATE permission_grants SET revoked_at = ?, revoked_reason = ? WHERE id = ? AND revoked_at IS NULL",
            (timestamp, reason, grant_id),
        )
        row = self.database.fetch_one("SELECT * FROM permission_grants WHERE id = ?", (grant_id,))
        if row and row.get("revoked_at"):
            self.audit(None, None, "permission_revoked", {"grant_id": grant_id, "reason": reason})
        return row

    def validate_permission(
        self, capability: str, payload: dict[str, Any], event_id: str,
        *, trusted_mode: bool = False, subject: str = "user",
    ) -> None:
        spec = CAPABILITY_MATRIX.get(capability)
        if spec is None:
            return
        configured = self._permission_levels.get(capability, spec["default"])
        if capability in LOCKED_CAPABILITIES:
            configured = spec["default"]
        if configured == "deny":
            self.audit(event_id, None, "permission_denied", {"capability": capability})
            raise PermissionDenied("Permission denied")
        scope_kind = str(payload.get("scope_kind") or spec["scope"])
        scope_value = payload.get("scope_value") or payload.get("project_id")
        grants = self.database.fetch_all(
            """SELECT * FROM permission_grants WHERE capability = ? AND subject IN (?, 'user')
            AND scope_kind = ? AND (scope_value IS ? OR scope_value = ?)
            AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)
            AND (single_use = 0 OR consumed_at IS NULL) ORDER BY granted_at DESC""",
            (capability, subject, scope_kind, scope_value, scope_value, now()),
        )
        precedence = {"auto": 0, "once": 1, "per_action": 2, "deny": 3}
        effective = max(
            [configured, *(grant["level"] for grant in grants)],
            key=lambda level: precedence[level],
        )
        if effective == "deny":
            self.audit(event_id, None, "permission_denied", {"capability": capability})
            raise PermissionDenied("Permission denied")
        matching_grant = next(
            (grant for grant in grants if grant["level"] == effective), None
        )
        if matching_grant:
            if matching_grant["single_use"]:
                self.consume_permission_grant(matching_grant["id"], event_id)
            return
        if trusted_mode and capability not in LOCKED_CAPABILITIES and configured == "auto":
            self.create_permission_grant({
                "capability": capability, "level": "auto", "scope_kind": scope_kind,
                "scope_value": scope_value, "subject": subject,
                "granted_by": "trusted_mode_legacy", "single_use": False,
            })
            return
        if effective in {"once", "per_action"}:
            self.audit(event_id, None, "permission_requested", {"capability": capability})
            raise PermissionUnavailable(
                "Permission grant required",
                capability=capability,
                scope_kind="message" if capability == "message.send" else scope_kind,
                scope_value=self.permission_scope_hash(capability, payload),
            )

    @staticmethod
    def permission_scope_hash(capability: str, payload: dict[str, Any]) -> str:
        if capability == "message.send":
            data = {key: payload.get(key) for key in ("to", "subject", "body", "attachments")}
        else:
            data = payload
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def tool_scope_hash(name: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps([name, payload], sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def has_live_grant(self, capability: str, scope_kind: str, scope_value: str) -> bool:
        """Read-only check: is there a grant that still applies right now?"""
        row = self.database.fetch_one(
            "SELECT 1 FROM permission_grants WHERE capability = ? AND scope_kind = ? "
            "AND scope_value = ? AND consumed_at IS NULL AND (expires_at IS NULL OR expires_at > ?) "
            "LIMIT 1",
            (capability, scope_kind, scope_value, now()),
        )
        return row is not None

    def consume_matching_permission(self, capability: str, scope_kind: str, scope_value: str, consumed_by: str) -> None:
        grant = self.database.fetch_one(
            """SELECT id FROM permission_grants WHERE capability=? AND scope_kind=?
            AND scope_value=? AND consumed_at IS NULL AND revoked_at IS NULL
            AND expires_at > ? ORDER BY granted_at DESC LIMIT 1""",
            (capability, scope_kind, scope_value, now()),
        )
        if not grant:
            raise PermissionUnavailable("Permission grant unavailable")
        self.consume_permission_grant(grant["id"], consumed_by)

    def authorise_and_consume(
        self, capability: str, scope_kind: str, scope_value: str, consumed_by: str
    ) -> str:
        """The user's confirmation *is* the grant: issue it, then spend it once.

        Without this a per_action capability could never be confirmed — the
        request is what creates the demand, so waiting for a pre-existing grant
        would deadlock every send.
        """
        try:
            self.consume_matching_permission(capability, scope_kind, scope_value, consumed_by)
            return "existing_grant"
        except PermissionUnavailable:
            pass
        # Defence in depth: the same action must not authorise itself twice.
        # A *new* action with identical content is still allowed, because the
        # fence is the consuming action, not the content.
        already = self.database.fetch_one(
            "SELECT id FROM permission_grants WHERE capability = ? AND scope_kind = ? "
            "AND scope_value = ? AND consumed_by = ?",
            (capability, scope_kind, scope_value, consumed_by),
        )
        if already:
            self.audit(None, None, "permission_reuse_blocked", {
                "capability": capability, "consumed_by": consumed_by,
                "grant_id": already["id"], "reason": "action_confirmed_twice",
            })
            raise PermissionUnavailable("permission_reuse_blocked")
        grant = self.create_permission_grant({
            "capability": capability,
            "level": "per_action",
            "scope_kind": scope_kind,
            "scope_value": scope_value,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "single_use": True,
            "granted_by": "user_confirmation",
        })
        self.consume_permission_grant(grant["id"], consumed_by)
        return "issued_on_confirmation"

    def _build_tool_registry(self) -> ToolRegistry:
        registry = ToolRegistry()

        async def escalate_runner(args: dict[str, Any]) -> dict[str, Any]:
            """Run a small script via the Codex CLI when no local tool fits.

            Read-only by default. The write path needs a consumed
            `escalate.write` grant, so it fails closed rather than escalating
            into a mutation nobody approved.
            """
            task = str(args.get("task") or "")
            write = bool(args.get("write"))
            if write:
                self.consume_matching_permission(
                    "escalate.write",
                    "task",
                    hashlib.sha256(task.encode("utf-8")).hexdigest(),
                    "tool:escalate",
                )
            result = await escalate.run(task, write=write)
            self.audit(None, None, "escalation_completed", {
                "write": write,
                "duration_ms": result.duration_ms,
                "chars": len(result.output),
            })
            return result.as_dict()

        registry.register(
            ToolDefinition(
                name="system.escalate",
                description=(
                    "当现有工具无法完成任务时，写一小段脚本并执行它（例如抓取数据、"
                    "转换格式、做一次性计算）。默认只读沙箱；write=true 需要事先获得许可。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "minLength": 1, "maxLength": 2000},
                        "write": {"type": "boolean"},
                    },
                    "required": ["task"],
                    "additionalProperties": False,
                },
                handler=escalate_runner,
                timeout_seconds=150.0,
            )
        )

        # Everything above is core plumbing. Everything below is a capability
        # pack: dropping a module into app/capabilities/ is the whole install.
        for tool in capability_tools(self):
            registry.register(tool)
        registry.register(
            ToolDefinition(
                name="device.get_status",
                description="查询设备在线、电量、网络、存储和充电状态。",
                input_schema={
                    "type": "object",
                    "properties": {"device_id": {"type": "string", "minLength": 1}},
                    "required": ["device_id"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.database.fetch_one(
                    "SELECT * FROM devices WHERE id = ?",
                    (args["device_id"],),
                ),
            )
        )
        registry.register(
            ToolDefinition(
                name="device.list",
                description="列出已经注册的个人设备。",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=lambda _: self.database.fetch_all(
                    "SELECT * FROM devices ORDER BY last_seen DESC"
                ),
            )
        )
        registry.register(
            ToolDefinition(
                name="task.list",
                description="查询当前提醒和任务，可按状态过滤。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "additionalProperties": False,
                },
                handler=lambda args: self.list_tasks(args.get("status"))[
                    : int(args.get("limit", 20))
                ],
            )
        )
        registry.register(
            ToolDefinition(
                name="memory.search",
                description="搜索 Jarvis 已保存的长期记忆。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=self._tool_search_memory,
            )
        )
        registry.register(
            ToolDefinition(
                name="location.get_latest",
                description="查询最近一次设备位置事件。",
                input_schema={
                    "type": "object",
                    "properties": {"device_id": {"type": "string"}},
                    "additionalProperties": False,
                },
                handler=lambda args: self._latest_event(
                    "location_change", args.get("device_id")
                ),
            )
        )
        registry.register(
            ToolDefinition(
                name="calendar.list_recent",
                description="查询设备最近同步的日历事件。",
                input_schema={
                    "type": "object",
                    "properties": {"device_id": {"type": "string"}},
                    "additionalProperties": False,
                },
                handler=lambda args: self._recent_events(
                    "calendar_change", device_id=args.get("device_id")
                ),
            )
        )
        registry.register(
            ToolDefinition(
                name="event.list_recent",
                description="查询最近进入 Jarvis 的外部事件。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "additionalProperties": False,
                },
                handler=lambda args: self._recent_events(
                    None,
                    source=args.get("source"),
                    limit=int(args.get("limit", 20)),
                ),
            )
        )
        return registry

    async def _tool_search_memory(self, args: dict[str, Any]) -> list[Any]:
        rows = await self.memory.search(
            str(args["query"]),
            limit=int(args.get("limit", 10)),
        )
        return [row.content for row in rows]

    def _latest_event(
        self,
        event_type: str,
        device_id: str | None = None,
    ) -> dict[str, Any] | None:
        rows = self._recent_events(event_type, device_id=device_id, limit=1)
        return rows[0] if rows else None

    def _recent_events(
        self,
        event_type: str | None,
        *,
        source: str | None = None,
        device_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if event_type:
            clauses.append("event_type = ?")
            args.append(event_type)
        if source:
            clauses.append("source = ?")
            args.append(source)
        sql = "SELECT * FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(limit, 50)))
        rows = self.database.fetch_all(sql, tuple(args))
        if not device_id:
            return rows
        filtered: list[dict[str, Any]] = []
        for row in rows:
            try:
                context = json.loads(row.get("context") or "{}")
            except json.JSONDecodeError:
                context = {}
            if context.get("device_id") == device_id:
                filtered.append(row)
        return filtered

    def set_push_callback(self, callback: PushCallback) -> None:
        self.push_callback = callback
        self.orchestrator.push = callback

    async def ingest_audio_message(
        self,
        device_id: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        """Receive realtime watch audio and emit one final speech event."""
        message_type = str(message.get("type") or "")
        session_id = str(message.get("session_id") or device_id)
        if message_type == "audio_start":
            self._cancel_speech_partial(session_id)
            self._speech_sessions[session_id] = RealtimeASRSession(
                sample_rate=int(message.get("sample_rate") or 16000),
                channels=int(message.get("channels") or 1),
                sample_width=int(message.get("sample_width") or 2),
            )
            self.audit(None, None, "speech_stream_started", {
                "device_id": device_id,
                "session_id": session_id,
            })
            return {"type": "audio_started", "session_id": session_id}

        session = self._speech_sessions.get(session_id)
        if session is None:
            return {
                "type": "speech_error",
                "session_id": session_id,
                "error": "audio_session_not_started",
            }

        if message_type == "audio_chunk":
            encoded = str(message.get("data") or "")
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                return {
                    "type": "speech_error",
                    "session_id": session_id,
                    "error": "invalid_audio_chunk",
                }
            if not chunk:
                return {"type": "audio_ack", "session_id": session_id}
            session.append(chunk)
            if session.pcm_bytes >= 16000:
                self._schedule_speech_partial(device_id, session_id)
            return {
                "type": "audio_ack",
                "session_id": session_id,
                "sequence": message.get("sequence"),
                "bytes": session.pcm_bytes,
            }

        if message_type == "audio_end":
            self._cancel_speech_partial(session_id)
            self._speech_sessions.pop(session_id, None)
            asr_seconds = session.pcm_bytes / (
                session.sample_rate * session.channels * session.sample_width
            )
            reservation = None
            try:
                reservation = self.reserve_asr_seconds(asr_seconds)
                transcript = await session.final()
                # Keep the time axis so the watch can show and replay the
                # transcript instead of racing to speak when recording stops.
                utterances = list(getattr(session, "utterances", []) or [])
                self.settle_asr_seconds(reservation, asr_seconds)
                reservation = None
            except UsageLimitExceeded as exc:
                return {
                    "type": "speech_error",
                    "session_id": session_id,
                    "error": "今日 ASR 配额已用尽",
                }
            except ASRError as exc:
                self.audit(None, None, "speech_stream_failed", {
                    "device_id": device_id,
                    "session_id": session_id,
                    "error": str(exc),
                })
                return {
                    "type": "speech_error",
                    "session_id": session_id,
                    "error": str(exc),
                }
            finally:
                if reservation is not None:
                    self.release_usage(reservation)
            if not transcript:
                return {
                    "type": "speech_final",
                    "session_id": session_id,
                    "text": "",
                    "utterances": utterances,
                    "action": "ignore",
                }
            event = Event(
                event_id=str(uuid.uuid4()),
                source=EventSource.MICROPHONE,
                event_type=EventType.SPEECH,
                timestamp=datetime.now(timezone.utc),
                content=transcript,
                context={
                    "device_id": device_id,
                    "conversation_id": device_id,
                    "speech_session_id": session_id,
                    "input_mode": "realtime_audio",
                },
            )
            try:
                result = await self.publish_event(event)
            except UsageLimitExceeded:
                return {
                    "type": "speech_error",
                    "session_id": session_id,
                    "error": "今日 AI 配额已用尽",
                }
            await self.push_agent_result(
                device_id,
                result,
                event_id=event.event_id,
                run_id=f"speech-run:{event.event_id}",
            )
            return {
                "type": "speech_final",
                "session_id": session_id,
                "event_id": event.event_id,
                "text": transcript,
                "utterances": utterances,
                "result": result,
            }
        return {"type": "speech_error", "error": "unknown_audio_message"}

    def _cancel_speech_partial(self, session_id: str) -> None:
        task = self._speech_partial_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()

    def record_prefiltered_microphone(
        self,
        device_id: str,
        transcript: str,
        session_id: str | None = None,
        reason: str = "ambient_or_incomplete",
    ) -> str:
        event_id = str(uuid.uuid4())
        event = Event(
            event_id=event_id,
            source=EventSource.MICROPHONE,
            event_type=EventType.SPEECH,
            timestamp=datetime.now(timezone.utc),
            content=transcript,
            context={
                "device_id": device_id,
                "conversation_id": device_id,
                "speech_session_id": session_id,
                "input_mode": "ambient_microphone",
                "prefilter_reason": reason,
            },
        )
        self.orchestrator.record_event(
            event, decision_action=DecisionAction.IGNORE.value
        )
        return event_id

    def _schedule_speech_partial(self, device_id: str, session_id: str) -> None:
        self._cancel_speech_partial(session_id)
        self._speech_partial_tasks[session_id] = asyncio.create_task(
            self._emit_speech_partial(device_id, session_id)
        )

    async def _emit_speech_partial(self, device_id: str, session_id: str) -> None:
        try:
            await asyncio.sleep(0.8)
            session = self._speech_sessions.get(session_id)
            if session is None or session.pcm_bytes < 16000:
                return
            text = await session.partial()
            if text and self.push_callback:
                await self.push_callback(device_id, {
                    "type": "speech_partial",
                    "session_id": session_id,
                    "text": text,
                })
            if text and self.event_publisher is not None:
                # The console shows what is being heard while it is being said, so
                # it belongs on the event bus too, not only on the device socket.
                self.event_publisher({
                    "event": "asr_partial",
                    "session_id": session_id,
                    "text": text,
                    "final": False,
                })
        except (ASRError, asyncio.CancelledError):
            return

    def create_agent_task(
        self,
        title: str,
        run_at: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.create_task(
            title=title,
            run_at=run_at,
            source="agent",
            payload=payload,
        )

    async def handle_message(
        self,
        text: str,
        device_id: str | None = None,
        image_data: str | None = None,
        project_id: str | None = None,
        conversation_id: str | None = None,
        event_source: EventSource = EventSource.USER,
    ) -> dict[str, Any]:
        conversation_id = conversation_id or device_id or "default"
        conversation = self.get_conversation(conversation_id, device_id)
        confirmation_result = (
            await self._handle_natural_language_confirmation(text, conversation)
            if event_source is EventSource.USER
            else None
        )
        if confirmation_result is not None:
            self.update_conversation_from_result(
                conversation_id,
                device_id,
                confirmation_result,
            )
            await self.push_agent_result(
                device_id,
                confirmation_result,
                event_id=confirmation_result.get("event_id"),
                run_id=f"confirmation-run:{uuid.uuid4()}",
            )
            return {**confirmation_result, "conversation_id": conversation_id}
        content: Any = text
        event_type = (
            EventType.SPEECH
            if event_source is EventSource.MICROPHONE
            else EventType.TEXT
        )
        if image_data:
            event_type = EventType.IMAGE
            content = [
                {"type": "text", "text": text or "请理解这张图片。"},
                {"type": "image_url", "image_url": {"url": image_data}},
            ]
        event = Event(
            event_id=str(uuid.uuid4()),
            source=event_source,
            event_type=event_type,
            timestamp=datetime.now(timezone.utc),
            content=content,
            context={
                key: value
                for key, value in {
                    "device_id": device_id,
                    "project_id": project_id,
                    "conversation_id": conversation_id,
                    "conversation": conversation,
                    "input_mode": (
                        "ambient_microphone"
                        if event_source is EventSource.MICROPHONE
                        else None
                    ),
                }.items()
                if value is not None
            },
        )
        result = await self.orchestrator.submit(event)
        self.update_conversation_from_result(
            conversation_id,
            device_id,
            result,
        )
        await self.push_agent_result(
            device_id,
            result,
            event_id=result.get("event_id"),
            run_id=f"message-run:{result.get('event_id', uuid.uuid4())}",
        )
        return {**result, "conversation_id": conversation_id}

    async def _handle_natural_language_confirmation(
        self,
        text: str,
        conversation: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not conversation or not conversation.get("pending_action_id"):
            return None
        normalized = "".join(str(text or "").lower().split())
        if not normalized:
            return None
        reject_words = ("取消", "不要", "拒绝", "算了", "不用了", "否")
        confirm_words = ("确认", "允许", "执行", "继续", "好的", "可以", "同意")
        if any(word in normalized for word in reject_words):
            action_id = str(conversation["pending_action_id"])
            rejected = self.reject_pending_action(action_id)
            if not rejected:
                return None
            return {
                "action": "reject",
                "reply": "已取消这次操作。",
                "reason": "natural_language_rejection",
            }
        if any(word in normalized for word in confirm_words):
            action_id = str(conversation["pending_action_id"])
            task = await self.async_confirm_pending_action(action_id)
            if task is None:
                return None
            return {
                "action": "confirmed",
                "reply": "已确认，我继续执行。",
                "reason": "natural_language_confirmation",
                "task": task,
            }
        return None

    @staticmethod
    def _attention_envelope(result: dict[str, Any]) -> dict[str, Any] | None:
        """Decide whether the watch should be interrupted, and how loudly.

        A permission request wakes the screen; anything else is a quiet nudge.
        Nothing here decides *whether* the action is allowed — Policy already did.
        """
        action = str(result.get("action") or "")
        if action == "confirm" and result.get("confirmation_id"):
            return {
                "type": "attention_required",
                "level": "high",
                "kind": "permission",
                "pending_action_id": result.get("confirmation_id"),
                "event_id": result.get("event_id"),
                "title": result.get("title") or result.get("reply") or "有件事需要你许可",
            }
        if action == "ask_user":
            return {
                "type": "attention_required",
                "level": "low",
                "kind": "question",
                "event_id": result.get("event_id"),
                "title": result.get("reply") or "需要你补充一句",
            }
        urgency = int(result.get("urgency") or 0)
        if action == "reply" and urgency >= 4:
            return {
                "type": "attention_required",
                "level": "high",
                "kind": "notice",
                "event_id": result.get("event_id"),
                "title": result.get("reply") or "有件事需要你看一眼",
            }
        return None

    async def push_agent_result(
        self,
        device_id: str | None,
        result: dict[str, Any],
        *,
        event_id: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        if not self.push_callback or not device_id:
            return False
        run_id = run_id or f"agent-run:{event_id or uuid.uuid4()}"
        logger.info(
            "agent result ready device_id=%s event_id=%s action=%s reply_chars=%d",
            device_id or "-",
            event_id or "-",
            result.get("action"),
            len(str(result.get("reply") or "")),
        )
        delivered = await self.push_callback(
            device_id,
            {
                "type": "agent_event",
                "event_id": event_id,
                "run_id": run_id,
                "text": result.get("reply") or result.get("reason") or "",
                "result": result,
            },
        )
        attention = self._attention_envelope(result)
        if attention is not None:
            await self.push_callback(device_id, attention)
            self.audit(event_id, None, "attention_pushed", {
                "level": attention["level"],
                "kind": attention["kind"],
            })
        if not delivered:
            self.audit(event_id, None, "device_push_failed", {
                "device_id": device_id,
                "run_id": run_id,
                "reason": "not_connected_or_send_failed",
            })
            logger.warning(
                "agent result not delivered device_id=%s event_id=%s run_id=%s",
                device_id or "-",
                event_id or "-",
                run_id,
            )
        else:
            self.audit(event_id, None, "device_push_delivered", {
                "device_id": device_id,
                "run_id": run_id,
            })
        reply = str(result.get("reply") or "").strip()
        if not delivered or not reply:
            return delivered
        # Speaking every reply made people brace for the audio and rush the
        # interaction. Text arrives first; audio is on demand, except when the
        # event genuinely must interrupt (urgency >= 4).
        if not should_auto_speak(result):
            self.audit(event_id, None, "tts_skipped", {
                "reason": "text_first",
                "urgent": False,
            })
            return delivered
        device = self.database.fetch_one(
            "SELECT tts_enabled, tts_ready, tts_error FROM devices WHERE id = ?",
            (device_id,),
        )
        if device is not None and not bool(device.get("tts_enabled", 1)):
            await self.push_callback(device_id, {
                "type": "tts_unavailable",
                "run_id": run_id,
                "reason": "disabled_on_device",
                "text": "TTS 已关闭，仅保留文字回复",
            })
            return delivered
        chunks = split_tts_chunks(reply)
        tts_lock = self._tts_locks.setdefault(device_id, asyncio.Lock())
        async with tts_lock:
            return await self._stream_tts(
                device_id, event_id, run_id, chunks, delivered
            )

    async def _stream_tts(
        self,
        device_id: str,
        event_id: str | None,
        run_id: str,
        chunks: list[str],
        delivered: bool,
    ) -> bool:
        await self.push_callback(
            device_id,
            {
                "type": "tts_start",
                "run_id": run_id,
                "event_id": event_id,
                "text": "",
                "chunk_count": len(chunks),
                "audio_format": "audio/mpeg",
                "audio_provider": settings.tts_provider,
                "interruptible": True,
            },
        )
        use_edge_audio = settings.tts_provider == "edge"
        for sequence, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "type": "tts_chunk",
                "run_id": run_id,
                "event_id": event_id,
                "sequence": sequence,
                "text": chunk,
            }
            if use_edge_audio:
                try:
                    import base64

                    payload["audio_base64"] = base64.b64encode(
                        await edge_audio(chunk)
                    ).decode("ascii")
                except (TTSError, OSError):
                    use_edge_audio = False
            await self.push_callback(
                device_id,
                payload,
            )
            await asyncio.sleep(0.02)
        await self.push_callback(
            device_id,
            {
                "type": "tts_end",
                "run_id": run_id,
                "event_id": event_id,
                "chunk_count": len(chunks),
            },
        )
        return delivered

    async def submit_external_event(
        self,
        source: EventSource,
        event_type: EventType,
        content: Any,
        context: dict[str, Any] | None = None,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        event = Event(
            event_id=str(uuid.uuid4()),
            source=source,
            event_type=event_type,
            timestamp=datetime.now(timezone.utc),
            content=content,
            context=context or {},
            confidence=confidence,
        )
        return await self.orchestrator.submit(event)

    def get_conversation(
        self,
        conversation_id: str,
        device_id: str | None = None,
    ) -> dict[str, Any] | None:
        conversation = self.database.fetch_one(
            """
            SELECT * FROM conversations
            WHERE id = ? AND status = 'active' AND expires_at > ?
            """,
            (conversation_id, now()),
        )
        if not conversation:
            return None
        if device_id and conversation.get("device_id") not in {None, device_id}:
            return None
        try:
            conversation["context"] = json.loads(conversation.get("context") or "{}")
        except json.JSONDecodeError:
            conversation["context"] = {}
        return conversation

    def update_conversation_from_result(
        self,
        conversation_id: str,
        device_id: str | None,
        result: dict[str, Any],
    ) -> None:
        timestamp = now()
        expires_at = (
            datetime.now(timezone.utc) + CONVERSATION_TTL
        ).isoformat()
        if result.get("failed"):
            # A failure is not a question. Record it as a closed thread so it
            # stays auditable, but never leave a question nobody can answer
            # (the v1 database had one parked for 17 days exactly this way).
            self.database.execute(
                """
                INSERT INTO conversations
                (id, device_id, status, pending_question, pending_action_id,
                 context, expires_at, created_at, updated_at)
                VALUES (?, ?, 'closed', NULL, NULL, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = 'closed',
                    pending_question = NULL,
                    pending_action_id = NULL,
                    context = excluded.context,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    device_id,
                    json.dumps(
                        {"last_action": "failed", "last_event_id": result.get("event_id")},
                        ensure_ascii=False,
                    ),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            return
        pending_question = None
        pending_action_id = result.get("confirmation_id")
        if result.get("action") in {"ask_user", "confirm"}:
            pending_question = result.get("reply") or result.get("reason") or "请补充信息。"
        if pending_question or pending_action_id:
            self.database.execute(
                """
                INSERT INTO conversations
                (id, device_id, status, pending_question, pending_action_id,
                 context, expires_at, created_at, updated_at)
                VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    device_id = excluded.device_id,
                    status = 'active',
                    pending_question = excluded.pending_question,
                    pending_action_id = excluded.pending_action_id,
                    context = excluded.context,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    device_id,
                    pending_question,
                    pending_action_id,
                    json.dumps(
                        {
                            "last_action": result.get("action"),
                            "last_event_id": result.get("event_id"),
                        },
                        ensure_ascii=False,
                    ),
                    expires_at,
                    timestamp,
                    timestamp,
                ),
            )
            return
        self.database.execute(
            """
            UPDATE conversations
            SET status = 'closed', pending_question = NULL,
                pending_action_id = NULL, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, conversation_id),
        )

    def list_conversations(self) -> list[dict[str, Any]]:
        self.database.execute(
            "UPDATE conversations SET status = 'expired' WHERE status = 'active' AND expires_at <= ?",
            (now(),),
        )
        return self.database.fetch_all(
            "SELECT * FROM conversations ORDER BY updated_at DESC"
        )

    async def run_event_worker(self) -> None:
        self._event_worker_active = True
        try:
            await self.orchestrator.run_worker()
        finally:
            self._event_worker_active = False

    async def _handle_runtime_event(self, envelope, run: AgentRun) -> RunResult:
        self.audit(
            envelope.event_id,
            None,
            "agent_run_started",
            run.to_dict(),
        )
        try:
            result = await self.orchestrator.handle(envelope.to_event())
            run.current_step = "completed"
            self.audit(
                envelope.event_id,
                None,
                "agent_run_completed",
                {"run": run.to_dict(), "result": result},
            )
            return RunResult.ok(
                result,
                metadata={"action": result.get("action")},
            )
        except Exception as exc:
            self.audit(
                envelope.event_id,
                None,
                "agent_run_failed",
                {"error": str(exc) or exc.__class__.__name__},
            )
            raise

    async def publish_event(self, event: Event) -> dict[str, Any]:
        """Publish an event, waiting for the runtime only when a worker exists."""
        if (
            event.event_type is EventType.TIMER
            and self._event_worker_active
        ):
            dispatch = await self.event_runtime.dispatch(event)
            if dispatch.run.result and dispatch.run.result.output:
                return dict(dispatch.run.result.output)
            return {
                "event_id": event.event_id,
                "action": "failed",
                "reply": "",
                "reason": dispatch.run.error.message
                if dispatch.run.error
                else dispatch.run.status.value,
            }
        if self._event_worker_active:
            return await self.orchestrator.submit(event)
        await self.orchestrator.bus.publish(event)
        return {
            "event_id": event.event_id,
            "action": "published",
            "reply": "",
            "reason": "event_worker_not_running",
        }

    def create_task(
        self,
        title: str,
        run_at: str | None = None,
        source: str = "api",
        payload: dict[str, Any] | None = None,
        timeout_at: str | None = None,
        max_retries: int = 3,
        depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        task_id = str(uuid.uuid4())
        run_at = normalize_datetime(run_at, self.user.timezone)
        timeout_at = normalize_datetime(timeout_at, self.user.timezone)
        timestamp = now()
        self.database.execute(
            """
            INSERT INTO tasks
            (id, title, status, run_at, source, payload, max_retries,
             timeout_at, next_attempt_at, depends_on, created_at, updated_at)
            VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                title,
                run_at,
                source,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
                max(0, min(max_retries, 20)),
                timeout_at,
                run_at,
                json.dumps(depends_on or [], ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
        return self.database.fetch_one(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) or {}

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self.database.fetch_all(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
        return self.database.fetch_all(
            "SELECT * FROM tasks ORDER BY created_at DESC"
        )

    def transition_task(self, task_id: str, target: str) -> dict[str, Any] | None:
        allowed = {
            "pending": {"paused", "cancelled", "waiting_confirmation"},
            "waiting_confirmation": {"pending", "cancelled"},
            "waiting_user": {"pending", "paused", "cancelled"},
            "waiting": {"done", "pending", "cancelled"},
            "paused": {"pending", "cancelled"},
            "failed": {"pending", "cancelled"},
        }
        task = self.database.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not task or target not in allowed.get(task["status"], set()):
            return None
        self.database.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (target, now(), task_id),
        )
        return self.database.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def get_due_tasks(self) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT * FROM tasks
            WHERE status = 'pending'
              AND run_at IS NOT NULL
              AND datetime(run_at) <= datetime(?)
              AND (
                    next_attempt_at IS NULL
                    OR datetime(next_attempt_at) <= datetime(?)
                  )
            ORDER BY run_at ASC
            """,
            (now(), now()),
        )

    def _dependencies_ready(self, task: dict[str, Any]) -> bool:
        try:
            dependencies = json.loads(task.get("depends_on") or "[]")
        except json.JSONDecodeError:
            dependencies = []
        if not dependencies:
            return True
        placeholders = ",".join("?" for _ in dependencies)
        rows = self.database.fetch_all(
            f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",
            tuple(str(item) for item in dependencies),
        )
        return len(rows) == len(dependencies) and all(
            row["status"] == "done" for row in rows
        )

    def claim_task(self, task_id: str) -> tuple[dict[str, Any] | None, str | None]:
        token = str(uuid.uuid4())
        timestamp = now()
        task = self.database.fetch_one(
            """
            SELECT * FROM tasks
            WHERE id = ? AND status = 'pending'
              AND (locked_at IS NULL OR locked_at < ?)
            """,
            (task_id, (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()),
        )
        if not task or not self._dependencies_ready(task):
            return None, None
        self.database.execute(
            """
            UPDATE tasks
            SET status = 'running', locked_at = ?, lock_token = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (timestamp, token, timestamp, task_id),
        )
        claimed = self.database.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND lock_token = ?",
            (task_id, token),
        )
        return claimed, token if claimed else None

    def release_task_lock(self, task_id: str, token: str) -> None:
        self.database.execute(
            "UPDATE tasks SET locked_at = NULL, lock_token = NULL WHERE id = ? AND lock_token = ?",
            (task_id, token),
        )

    def mark_task_notified(self, task_id: str) -> dict[str, Any] | None:
        self.database.execute(
            """
            UPDATE tasks SET status = 'waiting', locked_at = NULL, lock_token = NULL,
                next_attempt_at = NULL, updated_at = ? WHERE id = ?
            """,
            (now(), task_id),
        )
        return self.database.fetch_one(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )

    def mark_delivery_failed(
        self,
        task_id: str,
        retry_count: int,
        max_retries: int,
        error: str = "设备推送失败",
    ) -> dict[str, Any] | None:
        exhausted = retry_count >= max_retries
        status = "failed" if exhausted else "pending"
        retry_delay = min(3600, 5 * (2 ** max(0, retry_count - 1)))
        next_attempt = (
            None
            if exhausted
            else (datetime.now(timezone.utc) + timedelta(seconds=retry_delay)).isoformat()
        )
        self.database.execute(
            """
            UPDATE tasks
            SET status = ?, retry_count = ?, last_error = ?, next_attempt_at = ?,
                locked_at = NULL, lock_token = NULL,
                dead_letter_at = CASE WHEN ? THEN ? ELSE dead_letter_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (status, retry_count, error, next_attempt, exhausted, now() if exhausted else None, now(), task_id),
        )
        return self.database.fetch_one(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )

    def complete_task(self, task_id: str) -> dict[str, Any] | None:
        task = self.database.fetch_one(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )
        if task is None:
            return None
        self.database.execute(
            """
            UPDATE tasks SET status = 'done', locked_at = NULL, lock_token = NULL,
                next_attempt_at = NULL, updated_at = ? WHERE id = ?
            """,
            (now(), task_id),
        )
        return self.database.fetch_one(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )

    def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        return self.transition_task(task_id, "cancelled")

    def save_pending_action(
        self,
        event_id: str,
        action: str,
        title: str,
        run_at: str | None,
        target_agent: str | None,
        capability: str | None,
        payload: dict[str, Any],
        device_id: str | None,
        project_id: str | None,
    ) -> dict[str, Any]:
        action_id = str(uuid.uuid4())
        created = datetime.now(timezone.utc)
        self.database.execute(
            """
            INSERT INTO pending_actions
            (id, event_id, action, title, run_at, target_agent, capability, payload,
             device_id, project_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id, event_id, action, title, run_at, target_agent, capability,
                json.dumps(payload, ensure_ascii=False, default=str), device_id,
                project_id, created.isoformat(),
                (created + timedelta(minutes=15)).isoformat(),
            ),
        )
        pending = self.database.fetch_one(
            "SELECT * FROM pending_actions WHERE id = ?", (action_id,)
        ) or {}
        self._publish_pending_action("created", pending)
        return pending

    def _publish_pending_action(self, action: str, pending: dict[str, Any]) -> None:
        if self.event_publisher:
            self.event_publisher({
                "event": "pending_action",
                "action": action,
                "id": pending.get("id"),
                "capability": pending.get("capability"),
                "title": pending.get("title"),
            })

    def expire_pending_actions(self) -> int:
        rows = self.database.fetch_all(
            "SELECT * FROM pending_actions WHERE expires_at <= ?", (now(),)
        )
        for pending in rows:
            self.database.execute("DELETE FROM pending_actions WHERE id = ?", (pending["id"],))
            self._publish_pending_action("expired", pending)
        return len(rows)

    def confirm_pending_action(self, action_id: str) -> dict[str, Any] | None:
        pending = self.database.fetch_one(
            "SELECT * FROM pending_actions WHERE id = ? AND expires_at > ?",
            (action_id, now()),
        )
        if not pending:
            return None
        payload = json.loads(pending["payload"] or "{}")
        if pending["capability"] == "cancel":
            task = self.cancel_task(str(payload.get("task_id") or ""))
        elif pending["capability"] == "complete":
            task = self.complete_task(str(payload.get("task_id") or ""))
        elif pending["target_agent"] or pending["capability"]:
            # Capability-backed actions must go through the async confirmation path.
            return None
        else:
            task = self.create_task(
                pending["title"] or "Jarvis task",
                pending["run_at"],
                "confirmed",
                {
                    **payload,
                    "device_id": pending["device_id"],
                    "project_id": pending["project_id"],
                    "confirmed_action_id": action_id,
                },
            )
        if task is None:
            return None
        self.database.execute("DELETE FROM pending_actions WHERE id = ?", (action_id,))
        self._publish_pending_action("confirmed", pending)
        self.audit(pending["event_id"], task.get("id"), "action_confirmed", task)
        return task

    async def async_confirm_pending_action(
        self,
        action_id: str,
    ) -> dict[str, Any] | None:
        pending = self.database.fetch_one(
            "SELECT * FROM pending_actions WHERE id = ? AND expires_at > ?",
            (action_id, now()),
        )
        if not pending:
            return None
        payload = json.loads(pending["payload"] or "{}")
        if pending["capability"] in {"cancel", "complete"}:
            return self.confirm_pending_action(action_id)
        if pending["target_agent"] == "tool":
            try:
                self.authorise_and_consume(
                    str(pending["capability"]), str(payload["scope_kind"]),
                    str(payload["scope_value"]), action_id,
                )
                result = await self.orchestrator.classifier.tool_runtime.execute(
                    str(payload["tool_name"]), payload.get("tool_arguments") or {},
                    permissions=self.user.permissions,
                )
            except PermissionUnavailable as exc:
                return {"status": 409, "error": str(exc)}
            self.database.execute("DELETE FROM pending_actions WHERE id = ?", (action_id,))
            self._publish_pending_action("confirmed", pending)
            value = result.to_dict()
            self.audit(pending["event_id"], None, "action_confirmed", value)
            return value
        if not pending["target_agent"] and not pending["capability"]:
            return self.confirm_pending_action(action_id)
        if not pending["target_agent"] or not pending["capability"]:
            self.audit(
                pending["event_id"],
                None,
                "action_confirmation_failed",
                {"id": action_id, "reason": "incomplete_capability_binding"},
            )
            return None

        matrix_name = payload.get("capability")
        if matrix_name and payload.get("scope_value"):
            try:
                grant_source = self.authorise_and_consume(
                    str(matrix_name), str(payload["scope_kind"]),
                    str(payload["scope_value"]), action_id,
                )
                self.audit(
                    pending["event_id"], None, "permission_consumed",
                    {"pending_action_id": action_id, "source": grant_source,
                     "capability": str(matrix_name)},
                )
            except PermissionUnavailable as exc:
                self.audit(
                    pending["event_id"], None,
                    "permission_reuse_blocked" if "reuse" in str(exc) else "permission_denied",
                    {"pending_action_id": action_id, "reason": str(exc)},
                )
                return {"status": 409, "error": str(exc)}

        task = TaskItem(
            task_id=f"confirmed:{action_id}",
            title=str(pending["title"] or "Confirmed action"),
            input_data=payload,
            target_agent=str(pending["target_agent"]),
            capability=str(pending["capability"]),
        )
        execution = await self.orchestrator.executor.execute(task, self.user)
        dispatched = isinstance(execution.output, dict) and bool(
            execution.output.get("accepted")
        )
        if not execution.success and not dispatched:
            self.audit(
                pending["event_id"],
                None,
                "action_confirmation_failed",
                {
                    "id": action_id,
                    "target_agent": pending["target_agent"],
                    "capability": pending["capability"],
                    "error": execution.error,
                },
            )
            return None
        self.database.execute("DELETE FROM pending_actions WHERE id = ?", (action_id,))
        self._publish_pending_action("confirmed", pending)
        result = {
            "execution": execution.__dict__,
            "target_agent": pending["target_agent"],
            "capability": pending["capability"],
            "dispatched": dispatched,
        }
        self.audit(pending["event_id"], None, "action_confirmed", result)
        return result

    def reject_pending_action(self, action_id: str) -> bool:
        pending = self.database.fetch_one(
            "SELECT * FROM pending_actions WHERE id = ?", (action_id,)
        )
        if not pending:
            return False
        self.database.execute("DELETE FROM pending_actions WHERE id = ?", (action_id,))
        self._publish_pending_action("rejected", pending)
        self.audit(pending["event_id"], None, "action_rejected", {"id": action_id})
        return True

    def create_memory(
        self,
        content: str,
        memory_type: str = "episodic",
        project_id: str | None = None,
        source_event_id: str | None = None,
        importance: float = 0.5,
        layer: str | None = None,
        status: str = "confirmed",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        memory_id = str(uuid.uuid4())
        timestamp = now()
        self.database.execute(
            """
            INSERT INTO memories
            (id, content, memory_type, project_id, source_event_id, importance, created_at, layer, status, tags, confirmed_by, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                content,
                memory_type,
                project_id,
                source_event_id,
                importance,
                timestamp,
                layer or ("long" if memory_type in {"fact", "preference"} else "short"),
                status,
                json.dumps(tags or [], ensure_ascii=False),
                "user" if status == "confirmed" else None,
                timestamp if status == "confirmed" else None,
            ),
        )
        return self.database.fetch_one(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ) or {}

    def list_memories(self, **filters: Any) -> list[dict[str, Any]]:
        clauses, args = [], []
        for key in ("layer", "status", "project_id"):
            if filters.get(key) is not None:
                clauses.append(f"{key} = ?"); args.append(filters[key])
        if filters.get("q"):
            clauses.append("content LIKE ?"); args.append(f"%{filters['q']}%")
        sql = "SELECT * FROM memories" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY importance DESC, created_at DESC"
        rows = self.database.fetch_all(sql, tuple(args))
        if filters.get("tag"):
            rows = [r for r in rows if filters["tag"] in json.loads(r.get("tags") or "[]")]
        return rows

    def revise_memory(self, memory_id: str, content: str, **fields: Any) -> dict[str, Any] | None:
        old = self.database.fetch_one("SELECT * FROM memories WHERE id=?", (memory_id,))
        if not old: return None
        values = {"content": content}
        for key in ("layer", "status", "project_id", "importance", "tags"):
            if key in fields and fields[key] is not None:
                values[key] = json.dumps(fields[key], ensure_ascii=False) if key == "tags" else fields[key]
        assignments = ", ".join(f"{key}=?" for key in values)
        self.database.execute(f"UPDATE memories SET {assignments}, revision=revision+1 WHERE id=?", (*values.values(), memory_id))
        self.audit(None, None, "memory_revised", {"memory_id": memory_id, "before": old["content"], "revision": int(old.get("revision") or 1)+1})
        return self.database.fetch_one("SELECT * FROM memories WHERE id=?", (memory_id,))

    def apply_memory_today(self, memory_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT id, project_id FROM memories WHERE id=?", (memory_id,))
        if not row: return None
        from datetime import date
        override_id = str(uuid.uuid4())
        applied_date = date.today().isoformat()
        self.database.execute(
            "INSERT INTO memory_daily_overrides(id, memory_id, applied_date, created_at) VALUES (?, ?, ?, ?)",
            (override_id, memory_id, applied_date, now()),
        )
        self.audit(None, None, "memory_apply_today", {"memory_id": memory_id, "override_id": override_id, "date": applied_date})
        return {"memory_id": memory_id, "override_id": override_id, "applied": True, "scope": "today"}

    def create_project(
        self,
        name: str,
        goal: str = "",
        metadata: dict[str, Any] | None = None,
        *,
        origin: str = "user",
        context_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建项目；治理逻辑在 ProjectService，这里只做入口。"""

        return self.projects.create(
            name,
            goal,
            origin=origin,
            metadata=metadata,
            context_snapshot=context_snapshot,
        )

    def list_projects(
        self,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.projects.list(status=status)

    def update_project_status(
        self,
        project_id: str,
        target: str,
        *,
        reason: str | None = None,
        one_liner: str | None = None,
        progress: int | None = None,
        expect_revision: int | None = None,
    ) -> dict[str, Any]:
        """状态流转入口；非法转移和版本冲突都以异常上报。"""

        return self.projects.update_status(
            project_id,
            target,
            reason=reason,
            one_liner=one_liner,
            progress=progress,
            expect_revision=expect_revision,
        )

    def project_index_text(self, *, limit: int = 20) -> str:
        """主 Agent 每轮注入用的项目索引文本。"""

        return self.projects.render_index_text(limit=limit)

    def register_device(
        self,
        device_id: str,
        name: str,
        platform: str,
    ) -> dict[str, Any]:
        timestamp = now()
        self.database.execute(
            """
            INSERT INTO devices (id, name, platform, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                platform = excluded.platform,
                last_seen = excluded.last_seen
            """,
            (device_id, name, platform, timestamp),
        )
        return self.database.fetch_one(
            "SELECT * FROM devices WHERE id = ?", (device_id,)
        ) or {}

    def touch_device(self, device_id: str) -> None:
        self.database.execute(
            "UPDATE devices SET last_seen = ? WHERE id = ?",
            (now(), device_id),
        )

    def update_device_status(
        self,
        device_id: str,
        battery_level: int | None = None,
        network_type: str | None = None,
        app_version: str | None = None,
        storage_free_mb: int | None = None,
        charging: bool | None = None,
        sequence: int | None = None,
        tts_enabled: bool | None = None,
        tts_ready: bool | None = None,
        tts_error: str | None = None,
        current_ma: float | None = None,
        cache_size: int | None = None,
        queue_size: int | None = None,
        sensing_state: str | None = None,
        model: str | None = None,
        battery_mah: int | None = None,
        cache_limit: int | None = None,
        input_caps: str | None = None,
        sensing_policy: str | None = None,
    ) -> dict[str, Any] | None:
        device = self.database.fetch_one(
            "SELECT * FROM devices WHERE id = ?", (device_id,)
        )
        if not device:
            return None
        if sequence is not None and sequence <= int(device.get("last_sequence") or 0):
            return device
        self.database.execute(
            """
            UPDATE devices
            SET last_seen = ?,
                battery_level = COALESCE(?, battery_level),
                network_type = COALESCE(?, network_type),
                app_version = COALESCE(?, app_version),
                storage_free_mb = COALESCE(?, storage_free_mb),
                charging = COALESCE(?, charging),
                tts_enabled = COALESCE(?, tts_enabled),
                tts_ready = COALESCE(?, tts_ready),
                tts_error = COALESCE(?, tts_error),
                current_ma = COALESCE(?, current_ma),
                cache_size = COALESCE(?, cache_size),
                queue_size = COALESCE(?, queue_size),
                sensing_state = COALESCE(?, sensing_state),
                model = COALESCE(?, model),
                battery_mah = COALESCE(?, battery_mah),
                cache_limit = COALESCE(?, cache_limit),
                input_caps = COALESCE(?, input_caps),
                sensing_policy = COALESCE(?, sensing_policy),
                last_sequence = COALESCE(?, last_sequence)
            WHERE id = ?
            """,
            (
                now(), battery_level, network_type, app_version,
                storage_free_mb, int(charging) if charging is not None else None,
                tts_enabled,
                tts_ready,
                tts_error,
                current_ma, cache_size, queue_size, sensing_state,
                model, battery_mah, cache_limit, input_caps, sensing_policy,
                sequence, device_id,
            ),
        )
        return self.database.fetch_one(
            "SELECT * FROM devices WHERE id = ?", (device_id,)
        )

    def record_device_roundtrip(self, device_id: str, timestamp: str | None = None) -> None:
        self.database.execute(
            """UPDATE device_sessions SET last_roundtrip_at = ?
               WHERE device_id = ? AND ended_at IS NULL""",
            (timestamp or now(), device_id),
        )

    def usage_snapshot(self) -> dict[str, Any]:
        return usage_snapshot(self.database)

    def record_asr_seconds(self, seconds: float) -> None:
        record_usage(self.database, asr_seconds=seconds)

    def reserve_asr_seconds(self, seconds: float) -> dict[str, Any]:
        return reserve_usage(
            self.database,
            asr_seconds=seconds,
            timezone_name=self.user.timezone,
        )

    def settle_asr_seconds(
        self,
        reservation: dict[str, Any] | None,
        seconds: float,
    ) -> None:
        settle_usage(
            self.database,
            reservation,
            asr_seconds=seconds,
            timezone_name=self.user.timezone,
        )

    def release_usage(self, reservation: dict[str, Any] | None) -> None:
        release_usage(self.database, reservation)

    async def queue_device_command(
        self,
        device_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        command_id = str(uuid.uuid4())
        created_dt = datetime.now(timezone.utc)
        created = created_dt.isoformat()
        expires_at = (created_dt + DEVICE_COMMAND_TTL).isoformat()
        self.database.execute(
            """
            INSERT INTO device_commands
            (id, device_id, action, payload, status, created_at, expires_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                command_id,
                device_id,
                action,
                json.dumps(payload or {}, ensure_ascii=False),
                created,
                expires_at,
            ),
        )
        delivered = await self.deliver_pending_commands(device_id)
        return {
            "command_id": command_id,
            "accepted": True,
            "delivered": delivered > 0,
            "status": "delivered" if delivered > 0 else "queued",
            "expires_at": expires_at,
        }

    async def send_device_command(
        self,
        device_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Compatibility wrapper; transport delivery is not device execution."""
        result = await self.queue_device_command(device_id, action, payload)
        return bool(result["delivered"])

    async def run_device_script(
        self, device_id: str, script_id: str, script: str
    ) -> dict[str, Any]:
        del device_id, script_id, script
        return {
            "success": False,
            "error": "设备脚本执行能力已禁用；请使用类型化、最小权限的设备能力",
        }

    async def deliver_pending_commands(self, device_id: str) -> int:
        commands = self.database.fetch_all(
            """
            SELECT * FROM device_commands
            WHERE device_id = ? AND status = 'pending'
            ORDER BY created_at ASC LIMIT 50
            """,
            (device_id,),
        )
        delivered = 0
        for command in commands:
            expires_at = command.get("expires_at")
            if not expires_at:
                try:
                    expires_at = (
                        datetime.fromisoformat(str(command["created_at"]))
                        + DEVICE_COMMAND_TTL
                    ).isoformat()
                except (KeyError, TypeError, ValueError):
                    expires_at = None
            if expires_at:
                try:
                    if datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
                        self.database.execute(
                            "UPDATE device_commands SET status = 'expired' WHERE id = ? AND status = 'pending'",
                            (command["id"],),
                        )
                        continue
                except ValueError:
                    self.database.execute(
                        "UPDATE device_commands SET status = 'expired' WHERE id = ? AND status = 'pending'",
                        (command["id"],),
                    )
                    continue
            if not self.push_callback:
                continue
            try:
                payload = json.loads(command["payload"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            ok = await self.push_callback(
                device_id,
                {
                    "type": "command",
                    "command_id": command["id"],
                    "action": command["action"],
                    "payload": payload,
                    "expires_at": expires_at,
                },
            )
            if not ok:
                break
            self.database.execute(
                """
                UPDATE device_commands
                SET status = 'delivered', delivered_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now(), command["id"]),
            )
            delivered += 1
        return delivered

    def acknowledge_device_command(
        self,
        device_id: str,
        command_id: str,
        success: bool,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> bool:
        command = self.database.fetch_one(
            "SELECT id, status, expires_at FROM device_commands WHERE id = ? AND device_id = ?",
            (command_id, device_id),
        )
        if not command:
            return False
        if command["status"] in {"acknowledged", "failed", "expired"}:
            return False
        if command.get("expires_at"):
            try:
                if datetime.fromisoformat(command["expires_at"]) <= datetime.now(timezone.utc):
                    self.database.execute(
                        "UPDATE device_commands SET status = 'expired' WHERE id = ?",
                        (command_id,),
                    )
                    return False
            except ValueError:
                return False
        self.database.execute(
            """
            UPDATE device_commands
            SET status = ?, acknowledged_at = ?
            WHERE id = ?
            """,
            ("acknowledged" if success else "failed", now(), command_id),
        )
        self.audit(
            None,
            None,
            "device_command_ack",
            {
                "device_id": device_id,
                "command_id": command_id,
                "success": success,
                "error": error,
                "result": result or {},
            },
        )
        return True

    def record_event(
        self,
        event_id: str,
        source: str,
        event_type: str,
        content: Any,
        context: dict[str, Any] | None = None,
        confidence: float = 1.0,
    ) -> None:
        self.database.execute(
            """
            INSERT OR REPLACE INTO events
            (id, source, event_type, content, context, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                source,
                event_type,
                str(content),
                json.dumps(context or {}, ensure_ascii=False, default=str),
                confidence,
                now(),
            ),
        )

    def audit(
        self,
        event_id: str | None,
        task_id: str | None,
        action: str,
        detail: Any,
    ) -> None:
        self.database.execute(
            """
            INSERT INTO audit_log (event_id, task_id, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_id,
                task_id,
                action,
                json.dumps(detail, ensure_ascii=False, default=str),
                now(),
            ),
        )
        if action == "event_result" and self.event_publisher:
            payload = detail if isinstance(detail, dict) else {}
            self.event_publisher({"event": "activity",
                "event_id": event_id or payload.get("event_id"),
                "attention": payload.get("attention"),
                "title": payload.get("title") or payload.get("action"),
                "reply": payload.get("reply")})

    async def dispatch_due_tasks(self) -> int:
        due_tasks = self.get_due_tasks()
        for task in due_tasks:
            claimed, lock_token = self.claim_task(task["id"])
            if not claimed or not lock_token:
                continue
            try:
                payload = json.loads(task["payload"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            device_id = payload.get("device_id") or task.get("device_id") or ""
            scheduled_at = task.get("run_at") or now()
            timer_event = Event(
                event_id=f"timer:{task['id']}",
                source=EventSource.SYSTEM,
                event_type=EventType.TIMER,
                timestamp=datetime.now(timezone.utc),
                content={
                    "task_id": task["id"],
                    "title": task["title"],
                    "payload": payload,
                    "scheduled_at": scheduled_at,
                },
                context={
                    "task_id": task["id"],
                    "device_id": device_id,
                    "conversation_id": device_id or task["id"],
                    "scheduled_at": scheduled_at,
                    "correlation_id": task["id"],
                    "dedupe_key": f"timer:{task['id']}",
                },
            )
            event_result = await self.publish_event(timer_event)
            self.audit(
                event_result.get("event_id"),
                task["id"],
                "timer_fired",
                event_result,
            )
            delivered = False
            if self.push_callback and device_id:
                run_id = f"timer-run:{task['id']}"
                if event_result.get("action") == "published":
                    delivered = await self.push_callback(
                        device_id,
                        {
                            "type": "timer.fired",
                            "event_type": "timer.fired",
                            "event_id": timer_event.event_id,
                            "dedupe_key": timer_event.event_id,
                            "task_id": task["id"],
                            "title": task["title"],
                            "payload": payload,
                            "device_id": device_id,
                            "scheduled_at": scheduled_at,
                        },
                    )
                else:
                    delivered = await self.push_agent_result(
                        device_id,
                        event_result,
                        event_id=event_result.get("event_id"),
                        run_id=run_id,
                    )
            if delivered or not self.push_callback or not device_id:
                updated = self.mark_task_notified(task["id"])
                self.audit(
                    event_result.get("event_id"),
                    task["id"],
                    "task_agent_completed",
                    updated,
                )
            else:
                retry_count = int(task.get("retry_count") or 0) + 1
                updated = self.mark_delivery_failed(
                    task["id"],
                    retry_count,
                    int(task.get("max_retries") or 3),
                    "设备推送失败",
                )
                self.audit(
                    event_result.get("event_id"),
                    task["id"],
                    "task_dead_lettered" if updated and updated["status"] == "failed"
                    else "task_delivery_failed",
                    updated,
                )
        return len(due_tasks)
