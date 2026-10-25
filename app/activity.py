"""Read-only, event-centred activity projection over existing persisted facts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .domain import AutonomyLevel
from .policy import CAPABILITY_MATRIX, LOCKED_CAPABILITIES


STATUSES = {
    "observed", "processing", "waiting_user", "accepted", "rejected",
    "completed", "cancelled", "failed", "expired", "ignored",
}


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def timestamp(value: Any) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return 0


def classify_attention(event, audits, pending) -> str:
    """Four mutually exclusive buckets, decided server-side.

    A plain reply is an automatic completion, not an ignored event: `ignored`
    is reserved for what the user or a rule actually dropped, otherwise the
    watch would label every answer "已忽略".
    """
    now = datetime.now(timezone.utc).timestamp()
    if any(timestamp(row.get("expires_at")) > now for row in pending):
        return "high_risk_waiting"
    context = json_object(event.get("context"))
    action_details = [json_object(row.get("detail")) for row in audits]
    if context.get("candidate_type") or context.get("source") == "ai_candidate":
        return "suggest_once"
    if event.get("decision_action") == "ignore" or any(
        detail.get("action") in {"ignore", "reject"}
        or detail.get("decision_action") == "ignore"
        for detail in action_details
    ):
        return "ignored"
    return "auto_done"


def encode_cursor(row: dict[str, Any]) -> str:
    data = json.dumps([1, row["sort_time"], row["id"]]).encode()
    return base64.urlsafe_b64encode(data).decode()


def decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        if len(cursor) > 2048:
            raise ValueError
        parts = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(parts, list) or len(parts) != 3 or parts[0] != 1
            or type(parts[1]) not in (int, float) or not math.isfinite(parts[1])
            or not isinstance(parts[2], str) or not parts[2]
        ):
            raise ValueError
        return parts[1], parts[2]
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("Invalid activity cursor") from exc


class ActivityFeed:
    def __init__(self, database: Database):
        self.database = database

    def list(
        self, *, limit: int = 50, cursor: str | None = None,
        source: str | None = None, status: str | None = None,
        device_id: str | None = None, project_id: str | None = None,
        query: str | None = None, tag: str | None = None,
        include_ignored: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if status is not None and status not in STATUSES:
            raise ValueError("Invalid activity status")
        boundary = decode_cursor(cursor) if cursor else None
        clauses, args = [], []
        if source is not None:
            clauses.append("source = ?")
            args.append(source)
        # Legacy context can be malformed; never expose or parse it as SQL.
        for key, value in (("device_id", device_id), ("project_id", project_id)):
            if value is not None:
                clauses.append(
                    f"json_extract(CASE WHEN json_valid(context) THEN context "
                    f"ELSE '{{}}' END, '$.{key}') = ?"
                )
                args.append(value)
        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            # Scan fixed-size batches so filters can find older results, not
            # only matches in an arbitrarily truncated recent-events window.
            while len(matches) <= limit:
                page_clauses, page_args = list(clauses), list(args)
                if boundary:
                    page_clauses.append(
                        "(COALESCE(julianday(created_at), 0), id) < (?, ?)"
                    )
                    page_args.extend(boundary)
                where = " WHERE " + " AND ".join(page_clauses) if page_clauses else ""
                rows = [
                    dict(row) for row in connection.execute(
                        "SELECT *, COALESCE(julianday(created_at), 0) AS sort_time "
                        "FROM events" + where
                        + " ORDER BY sort_time DESC, id DESC LIMIT 100",
                        page_args,
                    )
                ]
                if not rows:
                    break
                projected = self._project(connection, rows)
                for row, item in zip(rows, projected):
                    if not include_ignored and status is None and item["status"] == "ignored":
                        continue
                    haystack = " ".join([
                        str(item.get("content") or ""),
                        str(item.get("title") or ""),
                        str(item.get("detail", {}).get("reason") or ""),
                    ]).lower()
                    if query and query.lower() not in haystack:
                        continue
                    if tag and tag not in item.get("tags", []):
                        continue
                    if status is None or item["status"] == status:
                        matches.append((row, item))
                        if len(matches) > limit:
                            break
                boundary = rows[-1]["sort_time"], rows[-1]["id"]
                if len(rows) < 100:
                    break
        page = matches[:limit]
        for _, item in page:
            refs = item.get("evidence", {}).get("refs", {})
            for group in ("memories", "events", "tools"):
                refs[group] = [
                    {key: ref.get(key) for key in ("id", "title") if key in ref}
                    for ref in refs.get(group, [])
                ]
            item["etag"] = hashlib.sha256(
                json.dumps(item, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
        return {
            "items": [item for _, item in page],
            "next_cursor": encode_cursor(page[-1][0]) if len(matches) > limit else None,
        }

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            return self._project(connection, [dict(row)])[0] if row else None

    def _project(self, connection, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ids = [event["id"] for event in events]
        marks = ",".join("?" for _ in ids)
        audits: dict[str, list[dict[str, Any]]] = {key: [] for key in ids}
        pending: dict[str, list[dict[str, Any]]] = {key: [] for key in ids}
        memories: dict[str, list[dict[str, Any]]] = {key: [] for key in ids}
        grants: dict[str, list[dict[str, Any]]] = {key: [] for key in ids}
        for row in connection.execute(
            f"SELECT * FROM audit_log WHERE event_id IN ({marks}) ORDER BY id", ids
        ):
            audits[row["event_id"]].append(dict(row))
        for row in connection.execute(
            f"SELECT * FROM pending_actions WHERE event_id IN ({marks}) "
            "ORDER BY created_at, id", ids,
        ):
            pending[row["event_id"]].append(dict(row))
        for row in connection.execute(
            f"SELECT * FROM memories WHERE source_event_id IN ({marks}) "
            "ORDER BY created_at, id", ids,
        ):
            memories[row["source_event_id"]].append(dict(row))
        for row in connection.execute(
            f"SELECT * FROM permission_grants WHERE parent_grant_id IN "
            f"(SELECT id FROM pending_actions WHERE event_id IN ({marks}))",
            ids,
        ):
            for key in ids:
                if any(p.get("id") == row["parent_grant_id"] for p in pending[key]):
                    grants[key].append(dict(row))
        task_ids = {
            row["task_id"] for group in audits.values()
            for row in group if row.get("task_id")
        }
        tasks = {}
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            tasks = {
                row["id"]: dict(row) for row in connection.execute(
                    f"SELECT id, title, status, run_at, updated_at FROM tasks "
                    f"WHERE id IN ({placeholders})", tuple(task_ids),
                )
            }
        return [
            project_event(
                event, audits[event["id"]], pending[event["id"]],
                memories[event["id"]], tasks,
                grants[event["id"]],
            )
            for event in events
        ]


class ActivityActionError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class ActivityActions:
    """Apply user actions while keeping the original event as the stable card."""

    ACTIONS = {"accept", "reject", "ignore", "modify", "delete", "undo"}

    def __init__(self, database: Database, service, feed: ActivityFeed | None = None):
        self.database = database
        self.service = service
        self.feed = feed or ActivityFeed(database)

    def apply(self, event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = event_id.removeprefix("event:")
        action = str(payload.get("action") or "").strip().lower()
        if action not in self.ACTIONS:
            raise ActivityActionError(400, "Unsupported activity action")
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if not idempotency_key:
            raise ActivityActionError(400, "idempotency_key is required")

        current = self.feed.get(event_id)
        if current is None:
            raise ActivityActionError(404, "Activity not found")
        replay = self._find_replay(event_id, idempotency_key)
        if replay is not None:
            return {"activity": self.feed.get(event_id), "idempotent_replay": True}

        expected = payload.get("revision", payload.get("expected_revision"))
        if expected is not None:
            try:
                expected = int(expected)
            except (TypeError, ValueError) as exc:
                raise ActivityActionError(400, "revision must be an integer") from exc
            if expected != current["revision"]:
                raise ActivityActionError(409, "Activity revision has changed")

        before = self._snapshot(event_id, current)
        task_id = None
        if action == "accept":
            pending_id = current.get("pending_action_id")
            confirmed = payload.get("_confirmed_result")
            if confirmed is not None:
                if not isinstance(confirmed, dict):
                    raise ActivityActionError(409, "Invalid confirmed action result")
            else:
                if not pending_id:
                    raise ActivityActionError(409, "Activity has no pending action")
                task = self.service.confirm_pending_action(pending_id)
                if task is None:
                    raise ActivityActionError(409, "Pending action is missing or expired")
                task_id = task.get("id")
        elif action == "reject":
            self._require_pending(current)
            if not self.service.reject_pending_action(current["pending_action_id"]):
                raise ActivityActionError(409, "Pending action is missing")
        elif action == "ignore":
            self._ignore_event(event_id, current)
        elif action == "modify":
            content = str(payload.get("content") or "").strip()
            if not content:
                raise ActivityActionError(400, "content is required for modify")
            self.database.execute(
                "UPDATE events SET content = ? WHERE id = ?", (content, event_id)
            )
        elif action == "delete":
            self._ignore_event(event_id, current)
        elif action == "undo":
            self._undo(event_id)

        self.service.audit(
            event_id,
            task_id,
            "activity_action",
            {
                "action": action,
                "idempotency_key": idempotency_key,
                "before": before,
                "content": payload.get("content"),
                "confirmed_result": payload.get("_confirmed_result"),
            },
        )
        updated = self.feed.get(event_id)
        return {"activity": updated, "idempotent_replay": False}

    def _find_replay(self, event_id: str, key: str) -> dict[str, Any] | None:
        rows = self.database.fetch_all(
            "SELECT detail FROM audit_log "
            "WHERE event_id = ? AND action = 'activity_action' "
            "ORDER BY id DESC",
            (event_id,),
        )
        for row in rows:
            try:
                detail = json.loads(row.get("detail") or "{}")
            except (TypeError, ValueError):
                continue
            if detail.get("idempotency_key") == key:
                return detail
        return None

    def _snapshot(self, event_id: str, activity: dict[str, Any]) -> dict[str, Any]:
        event = self.database.fetch_one(
            "SELECT content, decision_action FROM events WHERE id = ?", (event_id,)
        ) or {}
        pending = None
        pending_id = activity.get("pending_action_id")
        if pending_id:
            pending = self.database.fetch_one(
                "SELECT * FROM pending_actions WHERE id = ?", (pending_id,)
            )
        return {
            "content": event.get("content"),
            "decision_action": event.get("decision_action"),
            "pending": dict(pending) if pending else None,
        }

    def _require_pending(self, activity: dict[str, Any]) -> None:
        if not activity.get("pending_action_id"):
            raise ActivityActionError(409, "Activity has no pending action")

    def _ignore_event(self, event_id: str, activity: dict[str, Any]) -> None:
        pending_id = activity.get("pending_action_id")
        if pending_id:
            pending = self.database.fetch_one(
                "SELECT * FROM pending_actions WHERE id = ?", (pending_id,)
            )
            self.database.execute(
                "DELETE FROM pending_actions WHERE id = ?", (pending_id,)
            )
            if pending:
                self.service._publish_pending_action("rejected", pending)
        self.database.execute(
            "UPDATE events SET decision_action = 'ignore' WHERE id = ?", (event_id,)
        )

    def _undo(self, event_id: str) -> None:
        rows = self.database.fetch_all(
            "SELECT id, task_id, detail FROM audit_log "
            "WHERE event_id = ? AND action = 'activity_action' "
            "ORDER BY id DESC",
            (event_id,),
        )
        target = None
        for row in rows:
            try:
                detail = json.loads(row.get("detail") or "{}")
            except (TypeError, ValueError):
                continue
            if detail.get("action") != "undo":
                target = (row, detail)
                break
        if target is None:
            raise ActivityActionError(409, "No reversible activity action")
        row, detail = target
        before = detail.get("before") or {}
        self.database.execute(
            "UPDATE events SET content = ?, decision_action = ? WHERE id = ?",
            (before.get("content"), before.get("decision_action"), event_id),
        )
        pending = before.get("pending")
        if pending:
            existing = self.database.fetch_one(
                "SELECT id FROM pending_actions WHERE event_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (event_id,),
            )
            if not existing:
                values = (
                    pending.get("id") or str(uuid.uuid4()),
                    pending["event_id"],
                    pending["action"],
                    pending.get("title"),
                    pending.get("run_at"),
                    pending.get("target_agent"),
                    pending.get("capability"),
                    pending.get("payload") or "{}",
                    pending.get("device_id"),
                    pending.get("project_id"),
                    pending["created_at"],
                    pending["expires_at"],
                )
                self.database.execute(
                    """
                    INSERT INTO pending_actions
                    (id, event_id, action, title, run_at, target_agent, capability,
                     payload, device_id, project_id, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                self.service._publish_pending_action("created", {
                    "id": values[0],
                    "capability": values[6],
                    "title": values[3],
                })
        task_id = row.get("task_id")
        if task_id:
            task = self.database.fetch_one(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            )
            if task and task["status"] == "pending":
                self.service.cancel_task(task_id)


def project_event(event, audits, pending, memories, tasks, grants=()) -> dict[str, Any]:
    context = json_object(event.get("context"))
    decision, result = {}, {}
    last_response = None
    for audit in audits:
        detail = json_object(audit.get("detail"))
        if audit["action"] == "policy_decision":
            decision = detail
        if audit["action"] == "event_result":
            result = detail
        elif audit["action"] == "agent_run_completed":
            result = detail.get("result") if isinstance(detail.get("result"), dict) else result
        if audit["action"] in {"action_rejected", "action_confirmed"}:
            last_response = audit["action"]
    action = result.get("action") or decision.get("action") or event.get("decision_action")
    attention = classify_attention(event, audits, pending)
    capability = decision.get("capability")
    configured = CAPABILITY_MATRIX.get(capability, {})
    autonomy = decision.get("autonomy_level", configured.get("autonomy_level", 0))
    try:
        autonomy = int(autonomy)
    except (TypeError, ValueError):
        autonomy = 0
    risk = "high" if autonomy >= 4 or capability in LOCKED_CAPABILITIES else "medium" if autonomy == 3 else "low"
    confidence = decision.get("confidence")
    confidence_basis = decision.get("confidence_basis") or ("model" if confidence is not None else "none")
    related_ids = list(dict.fromkeys(a["task_id"] for a in audits if a.get("task_id")))
    task = next((tasks[key] for key in reversed(related_ids) if key in tasks), None)
    current = pending[-1] if pending else None
    title, status = str(event.get("content") or "收到事件"), "observed"
    if result:
        title = str(result.get("reply") or title)
        status = {
            "reply": "completed",
            "suggest": "completed",
            "create_task": "accepted",
            "execute": "processing",
            "remember": "observed",
        }.get(action, "observed")
        execution = result.get("execution")
        if isinstance(execution, dict):
            if execution.get("completed") is False:
                status = "processing" if execution.get("success") else "failed"
            else:
                status = "completed" if execution.get("success") else "failed"
    elif action:
        # A policy decision without a result means the worker has not finished.
        # Do not present it as a passive observation.
        status = {
            "reply": "completed",
            "suggest": "completed",
            "create_task": "accepted",
            "execute": "processing",
            "remember": "observed",
        }.get(action, "observed")
    elif event.get("processed_at"):
        # The event was consumed, but no decision/result was persisted. This is
        # usually an agent or integration failure and should be visible as one.
        status = "failed"
        title = "处理失败：服务没有返回结果"
    if action in {"ask_user", "confirm"}:
        status = "waiting_user"
    if action == "ignore":
        status, title = "ignored", "已忽略这条信息"
    if memories:
        status, title = "completed", f"已记录「{memories[-1]['content']}」"
    elif any(a["action"] == "memory_saved" for a in audits):
        status, title = "observed", "记忆记录已变更"
    if task:
        status = {
            "done": "completed", "cancelled": "cancelled", "failed": "failed",
            "waiting_confirmation": "waiting_user", "waiting_user": "waiting_user",
        }.get(task["status"], "accepted")
        verb = {"completed": "已完成", "cancelled": "已取消", "failed": "处理失败"}.get(
            status, "已记录待办"
        )
        title = f"{verb}「{task['title']}」"
    if any(a["action"] == "agent_run_failed" for a in audits):
        status = "failed" if not result else status
    if current:
        status = "waiting_user" if timestamp(current["expires_at"]) > datetime.now(timezone.utc).timestamp() else "expired"
        title = str(current.get("title") or "需要你的决定")
    if last_response == "action_rejected":
        status = "rejected"
    elif last_response == "action_confirmed" and not task:
        status = "accepted"
    tags = []
    source = str(event.get("source") or "")
    event_type = str(event.get("event_type") or "")
    audit_actions = {str(a.get("action") or "") for a in audits}
    if source == "microphone":
        tags.append("麦克风")
    if event_type == "speech":
        tags.append("ASR")
    prefiltered = "speech_prefilter_ignored" in audit_actions
    if prefiltered:
        tags.append("预筛选忽略")
    if action == "ignore" and not prefiltered:
        tags.append("Agent忽略")
    elif action == "reply":
        tags.append("Agent回复")
    elif action in {"ask_user", "confirm"}:
        tags.append("等待输入")
    if "agent_run_failed" in audit_actions or (
        status == "failed" and not audit_actions
    ):
        tags.append("Agent错误")
    if "tool_call" in audit_actions:
        tags.append("调用工具")
    activity_audits = []
    for audit in audits:
        if audit["action"] != "activity_action":
            continue
        detail = json_object(audit.get("detail"))
        activity_audits.append(detail)
    last_activity_action = activity_audits[-1].get("action") if activity_audits else None
    if last_activity_action == "undo":
        if current:
            status = (
                "waiting_user"
                if timestamp(current["expires_at"]) > datetime.now(timezone.utc).timestamp()
                else "expired"
            )
        elif event.get("decision_action") == "ignore":
            status = "ignored"
        elif task:
            status = {
                "done": "completed",
                "cancelled": "cancelled",
                "failed": "failed",
                "waiting_confirmation": "waiting_user",
                "waiting_user": "waiting_user",
            }.get(task["status"], "accepted")
        else:
            status = "observed"
    available_actions = ["accept", "reject"] if current and status == "waiting_user" else []
    if not available_actions and status in {
        "observed", "processing", "accepted", "completed", "failed", "cancelled"
    }:
        available_actions = ["modify", "delete"]
    if last_activity_action and last_activity_action != "undo":
        available_actions.append("undo")
    if attention == "suggest_once":
        available_actions.append("apply_today")
    history_meta = {"created": event.get("created_at"), "modified": 0, "deleted": 0, "undone": 0}
    for entry in activity_audits:
        verb = entry.get("action")
        key = {"modify": "modified", "delete": "deleted", "undo": "undone"}.get(verb)
        if key:
            history_meta[key] += 1
    pending_row = next((p for p in reversed(pending) if timestamp(p.get("expires_at")) > datetime.now(timezone.utc).timestamp()), None)
    grant_row = next((g for g in grants if not g.get("revoked_at") and not g.get("consumed_at")), None)
    grant = None
    if attention == "high_risk_waiting":
        row = grant_row or pending_row
        if row:
            grant = {
                "id": row.get("id"), "capability": row.get("capability"), "level": row.get("level", "per_action"),
                "scope": {"kind": row.get("scope_kind") or "none", "value": row.get("scope_value")},
                "expires_at": row.get("expires_at"), "single_use": bool(row.get("single_use", True)),
                "revocable_after_send": False, "compensation_only": row.get("capability") in LOCKED_CAPABILITIES,
            }
    mem_refs = [{"id": m.get("id"), "title": m.get("content"), "source": m.get("source"), "available": True} for m in memories]
    for ref in context.get("memory_refs", []):
        if isinstance(ref, dict) and ref.get("id") not in {m.get("id") for m in memories}:
            mem_refs.append({"id": ref.get("id"), "title": ref.get("title"), "source": ref.get("source"), "available": False})
    tool_refs = []
    event_refs = []
    for audit in audits:
        d = json_object(audit.get("detail"))
        if audit.get("action") == "tool_call":
            tool_refs.append({"id": str(audit.get("id")), "name": d.get("tool") or d.get("name"), "result": d.get("result"), "idempotency_key": d.get("idempotency_key"), "available": True})
        for ref in d.get("event_refs", []):
            if isinstance(ref, dict):
                event_refs.append({**ref, "available": ref.get("available", True)})
    degradation = []
    for audit in audits:
        d = json_object(audit.get("detail"))
        if audit.get("action") == "degradation":
            degradation.append({
                "stage": d.get("stage"), "from": d.get("from"), "to": d.get("to"),
                "capability_loss": d.get("capability_loss") or [],
                "recovered": bool(d.get("recovered", False)),
                **({"attempt": d["attempt"]} if d.get("attempt") is not None else {}),
            })
    times = [event.get("created_at"), event.get("processed_at")]
    times.extend(a["created_at"] for a in audits)
    if task:
        times.append(task["updated_at"])
    if current:
        times.append(current["created_at"])
        if status == "expired":
            times.append(current["expires_at"])
    # A run that broke must not be projected as completed, whatever else the
    # audits say. Placed last so nothing above can overwrite it.
    if isinstance(result, dict) and result.get("failed"):
        status = "failed"
        title = str(result.get("reply") or "这次没有完成")

    item = {
        "id": f"event:{event['id']}",
        "event_id": event["id"],
        "task_id": task["id"] if task else None,
        "task_ids": related_ids,
        "pending_action_id": current["id"] if current and status == "waiting_user" else None,
        "kind": "event",
        "source": event["source"],
        "event_type": event["event_type"],
        "device_id": context.get("device_id"),
        "project_ids": [context["project_id"]] if context.get("project_id") else [],
        "title": title,
        "content": event["content"],
        "decision_action": action,
        "tags": tags,
        "status": status,
        "created_at": event["created_at"],
        "updated_at": max((t for t in times if t), key=timestamp),
        "revision": len(audits),
        "available_actions": available_actions,
        "attention": attention,
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "risk": risk,
        "grant": grant,
        "degradation": degradation,
        "evidence": {
            "conclusion": title,
            "judgment": {"text": result.get("reason") or decision.get("reason"), "confidence": confidence, "confidence_basis": confidence_basis, "risk": risk},
            "refs": {"memories": mem_refs, "events": event_refs, "tools": tool_refs},
            "raw": context.get("raw_refs", []) if isinstance(context.get("raw_refs", []), list) else [],
        },
        "detail": {
            "reason": result.get("reason") or decision.get("reason"),
            "reply": result.get("reply"),
            "waiting_for": (
                "input" if action == "ask_user"
                else "decision" if action == "confirm" or current
                else None
            ),
            "task": task,
            "memory_ids": [memory["id"] for memory in memories],
            # Keep raw tool payloads and credentials out of the small-device feed.
            "history": [
                {"audit_id": a["id"], "action": a["action"], "created_at": a["created_at"]}
                for a in audits
            ],
            "activity_actions": activity_audits,
            "history_meta": history_meta,
        },
    }
    item["etag"] = hashlib.sha256(
        json.dumps(item, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return item
