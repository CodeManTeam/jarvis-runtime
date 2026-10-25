"""项目治理：状态机、索引视图与 Session 绑定。

主 Agent 是所有项目的唯一治理者。项目会话只能通过 ``touch_activity``
上报进展，状态流转必须经主 Agent 决策后调用 ``update_status``。
这一约束保证状态机只有一个写入者。

本模块不执行具体工作，也不生成自然语言摘要。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .db import Database


PROJECT_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"active", "cancelled"},
    "active": {"paused", "blocked", "done", "failed", "cancelled"},
    "paused": {"active", "cancelled"},
    "blocked": {"active", "failed", "cancelled"},
    "done": set(),
    "failed": set(),
    "cancelled": set(),
}

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})

STATUS_ICONS = {
    "blocked": "⛔",
    "active": "▶",
    "proposed": "?",
    "paused": "⏸",
}


class InvalidProjectTransition(ValueError):
    """非法的项目状态转移。"""


class StaleProjectRevision(ValueError):
    """并发写入时提供了过期的 revision。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_transition(current: str, target: str) -> None:
    """校验一次状态转移是否合法；非法时抛出 InvalidProjectTransition。"""

    if current == target:
        return
    allowed = PROJECT_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidProjectTransition(
            f"项目状态不能从 {current} 变为 {target}；"
            f"合法目标：{sorted(allowed) if allowed else '无（终态）'}"
        )


class ProjectService:
    """项目生命周期管理。

    职责边界：
      - 只管项目的状态、摘要、Session 绑定
      - 不执行具体工作（那是项目会话的事）
      - 不生成自然语言摘要（由调用方或模型提供）
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    # ---------- 创建 ----------

    def create(
        self,
        name: str,
        goal: str = "",
        *,
        origin: str = "user",
        metadata: dict[str, Any] | None = None,
        context_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建项目。

        ``origin="user"`` 直接进入 active；其余来源进入 proposed，
        等待用户确认后才开始工作。
        """

        if not str(name or "").strip():
            raise ValueError("项目名称不能为空")
        project_id = str(uuid.uuid4())
        timestamp = utc_now()
        status = "active" if origin == "user" else "proposed"
        one_liner = self._initial_one_liner(name, goal, status)

        self.database.execute(
            """
            INSERT INTO projects
            (id, name, goal, status, metadata, context_snapshot,
             origin, one_liner, progress, revision, last_activity_at,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
            """,
            (
                project_id,
                name,
                goal,
                status,
                json.dumps(metadata or {}, ensure_ascii=False),
                json.dumps(context_snapshot or {}, ensure_ascii=False),
                origin,
                one_liner,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        return self.get(project_id) or {}

    # ---------- 读取 ----------

    def get(self, project_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        return _decode(row) if row else None

    def list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if status and status != "all":
            rows = self.database.fetch_all(
                """
                SELECT * FROM projects
                WHERE status = ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (status, limit),
            )
        else:
            rows = self.database.fetch_all(
                "SELECT * FROM projects ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        return [_decode(row) for row in rows]

    # ---------- 状态流转 ----------

    def update_status(
        self,
        project_id: str,
        target: str,
        *,
        reason: str | None = None,
        one_liner: str | None = None,
        progress: int | None = None,
        expect_revision: int | None = None,
    ) -> dict[str, Any]:
        project = self.get(project_id)
        if project is None:
            raise KeyError(f"unknown project: {project_id}")

        if expect_revision is not None and project["revision"] != expect_revision:
            raise StaleProjectRevision(
                f"项目已被更新（当前 revision={project['revision']}，"
                f"你的 revision={expect_revision}）"
            )

        check_transition(project["status"], target)

        if target == "blocked" and not str(reason or "").strip():
            raise ValueError("转为 blocked 必须提供原因")

        timestamp = utc_now()
        fields: dict[str, Any] = {
            "status": target,
            "revision": int(project["revision"]) + 1,
            "updated_at": timestamp,
            "last_activity_at": timestamp,
        }
        if target == "blocked":
            fields["blocked_reason"] = str(reason).strip()
        elif target in {"active", "done"}:
            # 离开阻塞态或完成时清空旧的阻塞原因，避免索引显示过期信息。
            fields["blocked_reason"] = None
        if one_liner is not None:
            fields["one_liner"] = str(one_liner)[:60]
        if progress is not None:
            fields["progress"] = max(0, min(100, int(progress)))
        if target in TERMINAL_STATUSES:
            fields["closed_at"] = timestamp
            fields["progress"] = 100 if target == "done" else fields.get(
                "progress", project.get("progress", 0)
            )

        self._update_fields(project_id, fields)
        return self.get(project_id) or {}

    # ---------- 进展上报 ----------

    def touch_activity(
        self,
        project_id: str,
        *,
        one_liner: str | None = None,
        progress: int | None = None,
    ) -> dict[str, Any] | None:
        """项目有进展时刷新摘要，不改变状态。

        项目会话只能调用这个方法，不能直接改状态。
        """

        project = self.get(project_id)
        if project is None:
            return None
        timestamp = utc_now()
        fields: dict[str, Any] = {
            "last_activity_at": timestamp,
            "updated_at": timestamp,
            "revision": int(project["revision"]) + 1,
        }
        if one_liner is not None:
            fields["one_liner"] = str(one_liner)[:60]
        if progress is not None:
            fields["progress"] = max(0, min(100, int(progress)))
        self._update_fields(project_id, fields)
        return self.get(project_id)

    # ---------- Session 绑定 ----------

    def bind_session(self, project_id: str, session_id: str) -> dict[str, Any]:
        """把项目绑定到一个 DSH Session；已绑定则原样返回。"""

        project = self.get(project_id)
        if project is None:
            raise KeyError(f"unknown project: {project_id}")
        if project.get("session_id"):
            return project
        if not str(session_id or "").strip():
            raise ValueError("session_id 不能为空")
        self._update_fields(
            project_id,
            {"session_id": session_id, "updated_at": utc_now()},
        )
        return self.get(project_id) or {}

    # ---------- 主 Agent 视图 ----------

    def index_view(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """返回给主 Agent 的紧凑索引，每个项目一行。

        刻意只取摘要字段：主会话上下文必须保持小。
        """

        rows = self.database.fetch_all(
            """
            SELECT id, name, status, one_liner, progress,
                   blocked_reason, last_activity_at
            FROM projects
            WHERE status NOT IN ('done', 'cancelled')
            ORDER BY
                CASE status
                    WHEN 'blocked'  THEN 0
                    WHEN 'active'   THEN 1
                    WHEN 'proposed' THEN 2
                    WHEN 'paused'   THEN 3
                    ELSE 4
                END,
                updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "project_id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "one_liner": row.get("one_liner") or "",
                "progress": int(row.get("progress") or 0),
                "blocked_reason": row.get("blocked_reason"),
                "last_activity_at": row.get("last_activity_at"),
            }
            for row in rows
        ]

    def render_index_text(self, *, limit: int = 20) -> str:
        """渲染成可直接注入模型上下文的文本。

        每行约 50 token，20 个项目约 1000 token。
        存量项目没有 one_liner 时只显示名称，不报错。
        """

        items = self.index_view(limit=limit)
        if not items:
            return "当前没有进行中的项目。"
        lines: list[str] = []
        for item in items:
            icon = STATUS_ICONS.get(item["status"], "·")
            line = f"{icon} [{item['project_id'][:8]}] {item['name']}"
            if item["status"] == "blocked" and item["blocked_reason"]:
                line += f" — 阻塞：{item['blocked_reason']}"
            elif item["one_liner"]:
                line += f" — {item['one_liner']}"
            if item["progress"]:
                line += f" ({item['progress']}%)"
            lines.append(line)
        return "进行中的项目：\n" + "\n".join(lines)

    # ---------- 内部 ----------

    def _update_fields(self, project_id: str, fields: dict[str, Any]) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self.database.execute(
            f"UPDATE projects SET {assignments} WHERE id = ?",
            (*fields.values(), project_id),
        )

    @staticmethod
    def _initial_one_liner(name: str, goal: str, status: str) -> str:
        prefix = "待确认" if status == "proposed" else "刚启动"
        detail = str(goal or name).strip()
        return f"{prefix}：{detail[:30]}"


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("metadata", "context_snapshot"):
        if isinstance(row.get(key), str):
            try:
                row[key] = json.loads(row[key] or "{}")
            except json.JSONDecodeError:
                row[key] = {}
    return row


__all__ = [
    "InvalidProjectTransition",
    "PROJECT_TRANSITIONS",
    "ProjectService",
    "STATUS_ICONS",
    "StaleProjectRevision",
    "TERMINAL_STATUSES",
    "check_transition",
]
