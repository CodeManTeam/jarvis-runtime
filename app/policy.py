from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

from .domain import (
    AgentCapability,
    AutonomyLevel,
    Context,
    DecisionAction,
    PolicyDecision,
    PermissionLevel,
)

class PermissionErrorBase(Exception):
    """Raised when a capability cannot proceed under the current permission model."""

    status_code = 409


class PermissionDenied(PermissionErrorBase):
    status_code = 403


class PermissionUnavailable(PermissionErrorBase):
    status_code = 409

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


PERMISSION_PRECEDENCE = {
    PermissionLevel.AUTO: 0,
    PermissionLevel.ONCE: 1,
    PermissionLevel.PER_ACTION: 2,
    PermissionLevel.DENY: 3,
}

def register_capability(
    name: str, *, default: str, levels: tuple[str, ...], scope: str = "none"
) -> dict:
    """Let a capability pack add its permission entry instead of editing the table.

    Locked capabilities stay hand-written on purpose: a pack must never be able
    to mark its own high-risk action as locked or unlocked.
    """
    if name in CAPABILITY_MATRIX:
        return CAPABILITY_MATRIX[name]
    spec = {"default": default, "levels": tuple(levels), "scope": scope}
    CAPABILITY_MATRIX[name] = spec
    return spec


LOCKED_CAPABILITIES = frozenset({
    "message.send",
    "escalate.write",
    "payment.execute",
    "device.configure",
    "device.self_iterate",
    "device.remote_exec",
    "device.open_url",
})

TOOL_CAPABILITY = {
    "device.get_status": None,
    "device.list": None,
    "task.list": None,
    "memory.search": None,
    "event.list_recent": None,
    "calendar.list_recent": None,
    "location.get_latest": None,
    "device.open_url": "device.open_url",
    "device.vibrate": "device.vibrate",
    "device.notify": "device.notify",
    "system.escalate": "escalate.read",
}

# Filled by capability packs when they register, so a pack-declared capability
# is gated by the same map as the hand-written ones.
PACK_TOOL_CAPABILITY: dict[str, str] = {}


def register_tool_capability(tool_name: str, capability: str) -> None:
    PACK_TOOL_CAPABILITY[tool_name] = capability


def capability_for_tool(name: str) -> str | None:
    if name in TOOL_CAPABILITY:
        return TOOL_CAPABILITY[name]
    return PACK_TOOL_CAPABILITY.get(name)

CAPABILITY_ALIASES: dict[tuple[str, str], str] = {
    ("reminder", "create"): "reminder.write",
    ("reminder", "complete"): "reminder.write",
}


def matrix_capability(agent: str | None, capability: str | None) -> str | None:
    if agent is None or capability is None:
        return None
    return CAPABILITY_ALIASES.get((agent, capability))

CAPABILITY_MATRIX = {
    "reminder.write": {"default": "auto", "levels": ("auto", "once", "deny"), "scope": "project"},
    "memory.organize_short": {"default": "auto", "levels": ("auto", "once", "deny"), "scope": "none"},
    "memory.write_long": {"default": "once", "levels": ("once", "per_action", "deny"), "scope": "project"},
    "reminder.adjust_today": {"default": "once", "levels": ("once", "per_action", "deny"), "scope": "day"},
    "file.read": {"default": "deny", "levels": ("deny", "once"), "scope": "path"},
    "message.send": {"default": "per_action", "levels": ("per_action",), "scope": "message"},
    "payment.execute": {"default": "per_action", "levels": ("per_action",), "scope": "none"},
    "device.configure": {"default": "per_action", "levels": ("per_action",), "scope": "device"},
    "device.self_iterate": {"default": "per_action", "levels": ("per_action",), "scope": "device"},
    "device.remote_exec": {"default": "deny", "levels": ("deny", "per_action"), "scope": "none"},
    # Escalation: write a small script and run it when no tool exists.
    "escalate.read": {"default": "auto", "levels": ("auto", "once", "deny"), "scope": "none"},
    "escalate.write": {"default": "per_action", "levels": ("per_action",), "scope": "task"},
    "device.open_url": {"default": "per_action", "levels": ("per_action",), "scope": "tool"},
    "device.vibrate": {"default": "auto", "levels": ("auto", "once", "deny"), "scope": "tool"},
    "device.notify": {"default": "auto", "levels": ("auto", "once", "deny"), "scope": "tool"},
}


def permission_level_stricter(left: PermissionLevel | str, right: PermissionLevel | str) -> PermissionLevel:
    left_level, right_level = PermissionLevel(left), PermissionLevel(right)
    return left_level if PERMISSION_PRECEDENCE[left_level] >= PERMISSION_PRECEDENCE[right_level] else right_level


def permission_level_allows(grant: PermissionLevel | str, required: PermissionLevel | str) -> bool:
    """A grant authorises an action only when it is at least as strong as required.

    `deny` is a prohibition rather than a very strong grant, so it never
    authorises anything — including another `deny`. Fails closed on both sides.
    """
    granted, needed = PermissionLevel(grant), PermissionLevel(required)
    if PermissionLevel.DENY in (granted, needed):
        return False
    return PERMISSION_PRECEDENCE[granted] >= PERMISSION_PRECEDENCE[needed]


class PolicyEngine:
    """AI 负责理解；PolicyEngine 负责把 AI 决定限制在安全边界内。"""

    def __init__(self, agent_lookup: Any, *, trusted_mode: bool = False) -> None:
        self.agent_lookup = agent_lookup
        self.trusted_mode = trusted_mode
        self.permission_service: Any = None

    def validate(self, decision: PolicyDecision, context: Context) -> PolicyDecision:
        if decision.action == DecisionAction.EXECUTE:
            if not decision.target_agent or not decision.capability:
                return PolicyDecision(
                    action=DecisionAction.ASK_USER,
                    reply="我还无法确定要调用哪个能力。",
                    reason="missing_agent_capability",
                )
            agent = self.agent_lookup(decision.target_agent, decision.capability)
            if agent is None:
                return PolicyDecision(
                    action=DecisionAction.ASK_USER,
                    reply="这个能力还没有安装到 Jarvis。",
                    reason="capability_unavailable",
                )
            capability: AgentCapability = agent.capabilities[decision.capability]
            matrix_name = matrix_capability(decision.target_agent, decision.capability)
            if self.permission_service is not None and matrix_name is not None:
                self.permission_service.validate_permission(
                    matrix_name,
                    decision.payload,
                    context.event.event_id,
                    trusted_mode=self.trusted_mode,
                    subject=f"agent:{decision.target_agent}",
                )
            missing = capability.required_permissions - context.user.permissions
            if missing:
                if (
                    decision.target_agent == "device"
                    and decision.capability == "status"
                ):
                    return PolicyDecision(
                        action=DecisionAction.ASK_USER,
                        reply="我需要知道要查询哪台设备。",
                        reason="missing_device_id",
                    )
                return PolicyDecision(
                    action=DecisionAction.CONFIRM,
                    reply="这个操作需要额外权限确认。",
                    reason=f"missing_permissions:{','.join(sorted(missing))}",
                    needs_confirmation=True,
                    target_agent=decision.target_agent,
                    capability=decision.capability,
                    payload={
                        **decision.payload,
                        "_requested_action": decision.action.value,
                    },
                )
            explicit_confirmation = (
                capability.name in {"cancel", "open_url"}
                or capability.autonomy_level >= AutonomyLevel.HIGH_RISK_AUTO
            )
            if self.trusted_mode and not explicit_confirmation:
                decision.needs_confirmation = False
            if (
                (not self.trusted_mode or explicit_confirmation)
                and capability.autonomy_level > AutonomyLevel.LOW_RISK_AUTO
                and not decision.needs_confirmation
            ):
                decision.needs_confirmation = True
                decision.payload["_requested_action"] = decision.action.value
        if decision.action == DecisionAction.CREATE_TASK and not decision.title:
            return PolicyDecision(
                action=DecisionAction.ASK_USER,
                reply="你希望我创建什么任务？",
                reason="missing_task_title",
            )
        return decision


class InterruptionManager:
    def __init__(self, quiet_hours: tuple[int, int] = (23, 7)) -> None:
        self.quiet_start, self.quiet_end = quiet_hours

    def can_interrupt(
        self,
        decision: PolicyDecision,
        context: Context,
    ) -> bool:
        if decision.urgency >= 5:
            return True
        try:
            user_zone = ZoneInfo(context.user.timezone)
        except ZoneInfoNotFoundError:
            user_zone = timezone.utc
        current_hour = datetime.now(user_zone).hour
        in_quiet = (
            current_hour >= self.quiet_start
            or current_hour < self.quiet_end
        )
        if in_quiet and not context.user.interruption_policy.get("allow_quiet", False):
            return False
        if context.user.interruption_policy.get("do_not_disturb", False):
            return False
        return decision.interruption_allowed or decision.action == DecisionAction.CONFIRM
