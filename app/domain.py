"""Jarvis 运行时领域对象。

这些对象对应 structure.py 中的核心协议，尽量不依赖 FastAPI 或数据库，
方便以后替换存储、输入源和 Agent。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EventSource(str, Enum):
    USER = "user"
    MICROPHONE = "microphone"
    CALENDAR = "calendar"
    SCREEN = "screen"
    NOTIFICATION = "notification"
    LOCATION = "location"
    SYSTEM = "system"


class EventType(str, Enum):
    SPEECH = "speech"
    TEXT = "text"
    IMAGE = "image"
    CALENDAR_CHANGE = "calendar_change"
    SCREEN_CHANGE = "screen_change"
    NOTIFICATION = "notification"
    TIMER = "timer"
    LOCATION_CHANGE = "location_change"
    DEVICE_HEARTBEAT = "device_heartbeat"


class DecisionAction(str, Enum):
    IGNORE = "ignore"
    REPLY = "reply"
    REMEMBER = "remember"
    ASK_USER = "ask_user"
    SUGGEST = "suggest"
    CREATE_TASK = "create_task"
    EXECUTE = "execute"
    CONFIRM = "confirm"


class AutonomyLevel(int, Enum):
    OBSERVE = 0
    REMIND = 1
    PROPOSE = 2
    LOW_RISK_AUTO = 3
    HIGH_RISK_AUTO = 4


class PermissionLevel(str, Enum):
    AUTO = "auto"
    ONCE = "once"
    PER_ACTION = "per_action"
    DENY = "deny"


@dataclass
class PermissionGrant:
    id: str
    capability: str
    level: PermissionLevel
    scope_kind: str
    scope_value: str | None
    subject: str
    granted_at: str
    expires_at: str | None
    single_use: bool
    consumed_at: str | None = None
    consumed_by: str | None = None
    revoked_at: str | None = None
    revoked_reason: str | None = None
    parent_grant_id: str | None = None


class TaskStatus(str, Enum):
    PENDING = "pending"
    WAITING_CONFIRMATION = "waiting_confirmation"
    RUNNING = "running"
    WAITING = "waiting"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Event:
    event_id: str
    source: EventSource
    event_type: EventType
    timestamp: datetime
    content: Any
    context: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0


@dataclass
class UserProfile:
    user_id: str
    name: str = ""
    timezone: str = "Asia/Shanghai"
    preferences: dict[str, Any] = field(default_factory=dict)
    permissions: set[str] = field(default_factory=set)
    interruption_policy: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectContext:
    project_id: str
    name: str
    goal: str = ""
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryItem:
    memory_id: str
    memory_type: str
    content: Any
    created_at: datetime
    importance: float = 0.5
    project_id: str | None = None
    source_event_id: str | None = None
    layer: str = "short"
    status: str = "confirmed"
    confidence: float | None = None
    tags: list[str] = field(default_factory=list)
    revision: int = 1


@dataclass
class TaskItem:
    task_id: str
    title: str
    task_type: str = "one_off"
    input_data: dict[str, Any] = field(default_factory=dict)
    target_agent: str | None = None
    capability: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    run_at: datetime | None = None
    retry_count: int = 0
    max_retries: int = 3
    project_id: str | None = None
    result: Any = None
    error: str | None = None


@dataclass
class Context:
    event: Event
    user: UserProfile
    project: ProjectContext | None = None
    recent_events: list[Event] = field(default_factory=list)
    memories: list[MemoryItem] = field(default_factory=list)
    active_tasks: list[TaskItem] = field(default_factory=list)
    intent: dict[str, Any] | None = None
    conversation: dict[str, Any] | None = None


@dataclass
class PolicyDecision:
    action: DecisionAction
    reason: str = ""
    urgency: int = 0
    interruption_allowed: bool = False
    needs_confirmation: bool = False
    target_agent: str | None = None
    capability: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    reply: str = ""
    title: str = ""
    run_at: str | None = None
    memory_content: str = ""
    memory_type: str = "episodic"
    project_id: str | None = None
    confirmation_token: str | None = None
    # A broken run is a first-class outcome, not a question to the user.
    failed: bool = False
    confidence: float | None = None
    confidence_basis: str | None = None


@dataclass
class Plan:
    plan_id: str
    goal: str
    tasks: list[TaskItem]
    requires_confirmation: bool = False


@dataclass
class ExecutionResult:
    success: bool
    output: Any = None
    error: str | None = None
    should_retry: bool = False
    follow_up_task: TaskItem | None = None


@dataclass
class AgentCapability:
    name: str
    description: str
    input_schema: dict[str, str] = field(default_factory=dict)
    output_schema: dict[str, str] = field(default_factory=dict)
    required_permissions: set[str] = field(default_factory=set)
    autonomy_level: AutonomyLevel = AutonomyLevel.PROPOSE
    can_interrupt: bool = False
