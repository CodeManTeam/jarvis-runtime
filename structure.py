"""Jarvis 系统结构伪代码。

核心闭环：
    感知 -> 事件 -> 上下文 -> 判断 -> 记忆/追问/计划 -> 执行 -> 反馈

这是一份用于稳定产品边界的架构草图，不是可直接上线的业务代码。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator


# ---------------------------------------------------------------------------
# 1. 基础对象：用户、项目和事件
# ---------------------------------------------------------------------------


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
    CALENDAR_CHANGE = "calendar_change"
    SCREEN_CHANGE = "screen_change"
    NOTIFICATION = "notification"
    TIMER = "timer"
    LOCATION_CHANGE = "location_change"


@dataclass
class Event:
    """系统收到的一件事实或输入。"""

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


# ---------------------------------------------------------------------------
# 2. 统一决策对象
# ---------------------------------------------------------------------------


class DecisionAction(str, Enum):
    IGNORE = "ignore"
    REMEMBER = "remember"
    ASK_USER = "ask_user"
    SUGGEST = "suggest"
    CREATE_TASK = "create_task"
    EXECUTE = "execute"


class AutonomyLevel(int, Enum):
    OBSERVE = 0
    REMIND = 1
    PROPOSE = 2
    LOW_RISK_AUTO = 3
    HIGH_RISK_AUTO = 4


@dataclass
class Intent:
    name: str
    entities: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    project_id: str | None = None


@dataclass
class MemoryItem:
    memory_id: str
    memory_type: str
    content: Any
    created_at: datetime
    importance: float = 0.5
    project_id: str | None = None
    source_event_id: str | None = None


@dataclass
class TaskItem:
    """接下来需要做什么。记忆和任务必须分开。"""

    task_id: str
    title: str
    task_type: str = "one_off"
    input_data: dict[str, Any] = field(default_factory=dict)
    target_agent: str | None = None
    capability: str | None = None
    status: str = "pending"
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
    intent: Intent | None = None


@dataclass
class PolicyDecision:
    """策略层决定系统是否、何时以及如何行动。"""

    action: DecisionAction
    reason: str = ""
    urgency: int = 0
    interruption_allowed: bool = False
    needs_confirmation: bool = False
    target_agent: str | None = None
    capability: str | None = None
    payload: Any = None


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


# ---------------------------------------------------------------------------
# 3. 记忆存储
# ---------------------------------------------------------------------------


class MemoryStore:
    """真实实现可以替换成数据库、全文检索或向量数据库。"""

    async def save(self, item: MemoryItem) -> None:
        raise NotImplementedError

    async def search(
        self,
        query: str,
        project_id: str | None = None,
    ) -> list[MemoryItem]:
        raise NotImplementedError

    async def forget(self, memory_id: str) -> None:
        raise NotImplementedError


class ShortTermMemory(MemoryStore):
    def __init__(self, max_items: int = 50) -> None:
        self.items: deque[MemoryItem] = deque(maxlen=max_items)

    async def save(self, item: MemoryItem) -> None:
        self.items.append(item)

    async def search(
        self,
        query: str,
        project_id: str | None = None,
    ) -> list[MemoryItem]:
        _ = query
        return [
            item for item in self.items
            if project_id is None or item.project_id == project_id
        ]

    async def forget(self, memory_id: str) -> None:
        self.items = deque(
            (item for item in self.items if item.memory_id != memory_id),
            maxlen=self.items.maxlen,
        )


# ---------------------------------------------------------------------------
# 4. Agent、能力和权限
# ---------------------------------------------------------------------------


@dataclass
class AgentCapability:
    name: str
    description: str
    input_schema: dict[str, str] = field(default_factory=dict)
    output_schema: dict[str, str] = field(default_factory=dict)
    required_permissions: set[str] = field(default_factory=set)
    autonomy_level: int = 2
    can_interrupt: bool = False


class Agent:
    """一个垂直能力单元，例如日程、邮件、笔记或提醒 Agent。"""

    name: str
    description: str
    capabilities: dict[str, AgentCapability]

    async def execute(
        self,
        capability: str,
        payload: dict[str, Any],
    ) -> ExecutionResult:
        raise NotImplementedError


class AgentRegistry:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        self.agents[agent.name] = agent

    def find(self, agent_name: str, capability: str) -> Agent | None:
        agent = self.agents.get(agent_name)
        if agent and capability in agent.capabilities:
            return agent
        return None


# ---------------------------------------------------------------------------
# 5. 输入和理解模块
# ---------------------------------------------------------------------------


class EventBus:
    """所有输入源都通过事件总线进入系统。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Event] = asyncio.Queue()

    async def publish(self, event: Event) -> None:
        await self.queue.put(event)

    async def subscribe(self) -> AsyncIterator[Event]:
        while True:
            yield await self.queue.get()


"""
输入源接口，所有输入源都必须实现此接口。
"""
class InputSource:
    async def listen(self) -> AsyncIterator[Event]:
        raise NotImplementedError


class EventNormalizer:
    async def process(self, event: Event) -> Event:
        """清洗语音、统一字段、去重、补充基础元数据。"""
        return event


class RelevanceFilter:
    async def should_process(self, event: Event) -> bool:
        """过滤噪音、重复事件和明显无价值的信息。"""
        return bool(event.content)


"""
上下文构建器负责从记忆中提取相关信息，构建完整的上下文。
"""
class ContextBuilder:
    def __init__(self, memory: MemoryStore, user: UserProfile) -> None:
        self.memory = memory
        self.user = user

    async def build(self, event: Event) -> Context:
        """从记忆中提取相关信息，构建完整的上下文。"""
        memories = await self.memory.search(str(event.content))
        return Context(event=event, user=self.user, memories=memories)


class IntentClassifier:
    async def classify(self, context: Context) -> Intent | None:
        """使用规则或大模型识别意图、实体和所属项目。"""
        raise NotImplementedError


class PolicyEngine:
    async def decide(self, context: Context) -> PolicyDecision:
        """负责风险、权限、自动化程度、重复提醒和打断策略。"""
        if context.intent is None:
            return PolicyDecision(action=DecisionAction.REMEMBER)
        return PolicyDecision(action=DecisionAction.ASK_USER)


@dataclass
class Planner:
    """负责根据上下文和决策创建任务计划。"""
    async def create_plan(
        self,
        context: Context,
        decision: PolicyDecision,
    ) -> Plan:
        task = TaskItem(
            task_id="generated-task-id",
            title=str(context.event.content),
            input_data=decision.payload or {},
            target_agent=decision.target_agent,
            capability=decision.capability,
            project_id=context.project.project_id if context.project else None,
        )
        return Plan(plan_id="generated-plan-id", goal=task.title, tasks=[task])


# ---------------------------------------------------------------------------
# 6. 执行、响应、打断和审计
# ---------------------------------------------------------------------------


@dataclass
class ServerResponse:
    text: str = ""
    action: str | None = None
    requires_confirmation: bool = False
    payload: Any = None


class TaskExecutor:
    """负责根据计划执行任务，调用 Agent 能力。"""

    def __init__(self, agents: AgentRegistry) -> None:
            self.agents = agents

    async def execute(self, task: TaskItem) -> ExecutionResult:
        if not task.target_agent or not task.capability:
            return ExecutionResult(False, error="任务没有目标 Agent 或能力")

        agent = self.agents.find(task.target_agent, task.capability)
        if agent is None:
            return ExecutionResult(False, error="找不到可执行的 Agent 能力")

        task.status = "running"
        result = await agent.execute(task.capability, task.input_data)
        task.status = "done" if result.success else "failed"
        task.result = result.output
        task.error = result.error
        return result


class ResponseManager:
    async def build(self, result: ExecutionResult) -> ServerResponse:
        if result.success:
            return ServerResponse(text=str(result.output or "已完成"))
        return ServerResponse(text=f"执行失败：{result.error or '未知错误'}")


class InterruptionManager:
    async def can_interrupt(
        self,
        decision: PolicyDecision,
        context: Context,
    ) -> bool:
        """结合紧急程度、用户状态、时间和历史提醒决定是否打断。"""
        _ = context
        return decision.interruption_allowed and decision.urgency > 0

    async def notify(self, text: str, urgent: bool = False) -> None:
        _ = (text, urgent)


@dataclass
class AuditRecord:
    timestamp: datetime
    event_id: str | None
    task_id: str | None
    action: str
    detail: Any = None


class AuditLog:
    async def append(self, record: AuditRecord) -> None:
        """记录系统为什么做了某件事，方便撤销、调试和复盘。"""
        _ = record


# ---------------------------------------------------------------------------
# 7. 总编排器
# ---------------------------------------------------------------------------


class JarvisOrchestrator:
    """只负责串联模块，不把所有逻辑塞进一个 Gateway 函数。"""

    def __init__(
        self,
        bus: EventBus,
        normalizer: EventNormalizer,
        relevance: RelevanceFilter,
        context_builder: ContextBuilder,
        classifier: IntentClassifier,
        policy: PolicyEngine,
        planner: Planner,
        executor: TaskExecutor,
        memory: MemoryStore,
        response: ResponseManager,
        interruption: InterruptionManager,
        audit: AuditLog,
    ) -> None:
        self.bus = bus
        self.normalizer = normalizer
        self.relevance = relevance
        self.context_builder = context_builder
        self.classifier = classifier
        self.policy = policy
        self.planner = planner
        self.executor = executor
        self.memory = memory
        self.response = response
        self.interruption = interruption
        self.audit = audit

    async def handle(self, event: Event) -> ServerResponse | None:
        event = await self.normalizer.process(event)
        if not await self.relevance.should_process(event):
            return None

        context = await self.context_builder.build(event)
        context.intent = await self.classifier.classify(context)
        decision = await self.policy.decide(context)

        if decision.action == DecisionAction.IGNORE:
            return None

        if decision.action == DecisionAction.REMEMBER:
            await self.memory.save(
                MemoryItem(
                    memory_id=f"memory-{event.event_id}",
                    memory_type="episodic",
                    content=event.content,
                    created_at=event.timestamp,
                    source_event_id=event.event_id,
                )
            )
            return None

        if decision.action == DecisionAction.ASK_USER:
            return ServerResponse(
                text="我还缺少一些信息，请你补充。",
                action="ask_user",
                payload=decision.payload,
            )

        if decision.action in (
            DecisionAction.CREATE_TASK,
            DecisionAction.EXECUTE,
        ):
            plan = await self.planner.create_plan(context, decision)
            if decision.needs_confirmation:
                return ServerResponse(
                    text=f"我准备执行：{plan.goal}，是否确认？",
                    action="confirm",
                    requires_confirmation=True,
                    payload=plan,
                )

            responses: list[ServerResponse] = []
            for task in plan.tasks:
                result = await self.executor.execute(task)
                responses.append(await self.response.build(result))
            return responses[-1] if responses else None

        return ServerResponse(text="我理解了，但暂时没有需要执行的动作。")

    async def run(self) -> None:
        async for event in self.bus.subscribe():
            await self.handle(event)


class JarvisClient:
    """客户端只负责采集输入、展示结果和执行本地白名单动作。"""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    async def forward(self, source: InputSource) -> None:
        async for event in source.listen():
            await self.bus.publish(event)

    async def execute_local_action(self, action: str, payload: Any) -> None:
        allowed_actions = {"show_notification", "play_sound", "open_app"}
        if action not in allowed_actions:
            raise PermissionError(f"不允许的本地动作：{action}")
        _ = payload


# ---------------------------------------------------------------------------
# 8. 产品流程和下一步
# ---------------------------------------------------------------------------


PRODUCT_FLOW = """
一次完整流程：
1. 麦克风、日历、屏幕或用户输入产生 Event。
2. EventBus 统一接收事件。
3. EventNormalizer 清洗、去重、统一格式。
4. RelevanceFilter 判断是否值得继续处理。
5. ContextBuilder 读取用户、项目、记忆和活动任务。
6. IntentClassifier 提取意图和实体。
7. PolicyEngine 决定忽略、记忆、追问、建议、创建任务或执行。
8. Planner 把目标拆成 TaskItem。
9. TaskExecutor 检查权限并调用 Agent 能力。
10. ResponseManager 生成结果。
11. InterruptionManager 决定现在是否打断用户。
12. AuditLog 记录整个决策和执行过程。
"""


MODULE_BOUNDARY = """
模块边界：
- InputSource：只负责采集，不理解业务。
- EventBus：只负责传递事件，不做判断。
- ContextBuilder：只负责组装上下文。
- IntentClassifier：只负责理解，不决定是否允许行动。
- PolicyEngine：负责风险、权限、打断和自动化策略。
- Planner：负责把目标拆成任务。
- Agent：负责领域能力。
- TaskExecutor：负责调用 Agent、重试和更新状态。
- MemoryStore：负责长期事实和上下文。
- AuditLog：负责解释系统为什么做了什么。
- Client：负责展示结果和执行本地白名单动作。
"""


NEXT_TO_DEFINE = """
推荐实现顺序：
1. 先写一个内存版 EventBus、MemoryStore 和 TaskQueue。
2. 实现一个 ReminderAgent，跑通“创建提醒 -> 到时触发 -> 通知 -> 完成”。
3. 再实现 IntentClassifier 的规则版，不要一开始完全依赖大模型。
4. 固定 PolicyDecision 的动作、权限和确认规则。
5. 加入任务取消、重试、超时和等待后续回复。
6. 最后接入语音、日历、屏幕和长期记忆。
"""


if __name__ == "__main__":
    print(PRODUCT_FLOW)
