from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .agents import AgentRegistry
from .domain import (
    Context,
    DecisionAction,
    Event,
    EventSource,
    EventType,
    ExecutionResult,
    Plan,
    PolicyDecision,
    ProjectContext,
    TaskItem,
    TaskStatus,
    UserProfile,
)
from .events import EventBus, EventNormalizer, IgnoreRuleEngine, RelevanceFilter
from .llm import complete_with_tools, decide
from .memory import MemoryStore
from .routing import current_event
from .policy import (
    InterruptionManager,
    PermissionDenied,
    PermissionUnavailable,
    PolicyEngine,
    capability_for_tool,
)
from .tool_loop import AgentLoop
from .tools import ToolRegistry, ToolRuntime
from .capabilities import capability_descriptions


PushCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


class ContextBuilder:
    def __init__(
        self,
        memory: MemoryStore,
        database: Any,
        user: UserProfile,
    ) -> None:
        self.memory = memory
        self.database = database
        self.user = user

    async def build(self, event: Event) -> Context:
        memories = await self.memory.search(str(event.content), limit=20)
        rows = self.database.fetch_all(
            """
            SELECT * FROM tasks
            WHERE status IN ('pending', 'waiting', 'running')
            ORDER BY created_at DESC LIMIT 20
            """
        )
        active_tasks = [
            TaskItem(
                task_id=row["id"],
                title=row["title"],
                status=TaskStatus(row["status"]),
                run_at=(
                    datetime.fromisoformat(row["run_at"].replace("Z", "+00:00"))
                    if row.get("run_at")
                    else None
                ),
                input_data=json.loads(row.get("payload") or "{}"),
            )
            for row in rows
        ]
        project = None
        project_id = event.context.get("project_id")
        if project_id:
            row = self.database.fetch_one(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            )
            if row:
                project = ProjectContext(
                    project_id=row["id"],
                    name=row["name"],
                    goal=row["goal"],
                    status=row["status"],
                    metadata=json.loads(row["metadata"] or "{}"),
                )
        runtime_context = self._runtime_context(event)
        event.context["runtime_context"] = runtime_context
        return Context(
            event=event,
            user=self.user,
            project=project,
            memories=memories,
            active_tasks=active_tasks,
            conversation=event.context.get("conversation"),
        )

    def _runtime_context(self, event: Event) -> dict[str, Any]:
        """Expose bounded live context; the model decides whether it matters."""
        device_id = event.context.get("device_id")
        if not device_id:
            return {}
        device = self.database.fetch_one(
            """
            SELECT id, name, platform, last_seen, status, battery_level,
                   tts_enabled, tts_ready, tts_error
            FROM devices WHERE id = ?
            """,
            (device_id,),
        )
        rows = self.database.fetch_all(
            """
            SELECT source, event_type, content, context, decision_action, created_at
            FROM events
            ORDER BY created_at DESC LIMIT 40
            """
        )
        recent_events = []
        recent_microphone_events = []
        for row in reversed(rows):
            try:
                event_context = json.loads(row.get("context") or "{}")
            except json.JSONDecodeError:
                continue
            if event_context.get("device_id") != device_id:
                continue
            if row["source"] not in {"microphone", "user", "device"}:
                continue
            item = {
                "source": row["source"],
                "type": row["event_type"],
                "content": str(row["content"])[:240],
                "decision": row.get("decision_action"),
                "created_at": row["created_at"],
            }
            recent_events.append(item)
            if row["source"] == "microphone":
                recent_microphone_events.append(item)
        recent_events = recent_events[-12:]
        recent_microphone_events = recent_microphone_events[-6:]
        return {
            "device": device,
            "recent_events": recent_events,
            "recent_microphone_events": recent_microphone_events,
            "note": "位置等高频传感器数据不自动注入；需要时由模型调用对应工具。",
        }


class AIIntentClassifier:
    """模型负责理解并返回结构化决策，不在这里写自然语言规则。"""

    def __init__(
        self,
        agent_registry: AgentRegistry,
        tool_registry: ToolRegistry | None = None,
        audit: Callable[[str, str, Any], None] | None = None,
    ) -> None:
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry
        self.tool_runtime = (
            ToolRuntime(tool_registry) if tool_registry is not None else None
        )
        self._audit = audit
        self._llm_tool_names: dict[str, str] = {}
        self.permission_service: Any = None

    def _tool_schemas(self) -> list[dict[str, Any]]:
        if self.tool_registry is None:
            return []
        schemas: list[dict[str, Any]] = []
        self._llm_tool_names = {}
        for schema in self.tool_registry.list_schemas():
            tool_name = schema["name"]
            llm_name = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_name)
            if not llm_name or llm_name[0].isdigit():
                llm_name = f"tool_{llm_name}"
            original = llm_name
            index = 2
            while llm_name in self._llm_tool_names:
                llm_name = f"{original}_{index}"
                index += 1
            self._llm_tool_names[llm_name] = tool_name
            schemas.append({
                "type": "function",
                "function": {
                    "name": llm_name,
                    "description": schema["description"],
                    "parameters": schema["inputSchema"],
                },
            })
        return schemas

    async def classify(self, context: Context) -> PolicyDecision:
        agent_descriptions = self.agent_registry.describe()
        system = """你是 Jarvis 的理解与决策模型。
请根据用户事件、记忆、任务和可用 Agent 决定下一步。
回答时使用用户当前使用的语言，默认使用简体中文。
如果需要额外事实，优先调用可用工具获取，不要凭空猜测。
必须只输出合法 JSON，不能输出 Markdown。

action 只能是：
reply、ask_user、remember、suggest、create_task、execute。

**你没有"忽略"这个选项。** 内容型输入（用户说的话、通知、日历）一律先留住：
- 拿不准但可能有价值 → action=remember（写入短期记忆，不打扰用户）
- 需要用户补一句话才能继续 → action=ask_user
- 明确是环境声、旁人对话、无意义片段 → action=remember，并在 reason 里写明依据
  （如 reason=ambient_noise），系统会把它记为"已留存但未打扰"。
真正丢弃事件只能由系统忽略规则完成，不由你决定。

当 event.source 是 microphone 时，输入来自持续环境监听，但本次 ASR 文本已经
通过传输层降噪，必须由你根据事件内容、最近对话、设备状态和可用工具动态决定
是否相关。不要依赖固定关键词，也不要因为没有说 Jarvis 就自动忽略。
明确是环境声、旁人对话或没有语义的片段时用 action=remember 并注明
reason=ambient_noise；像是在和用户对话但信息不足时用 action=ask_user；位置、时间、设备状态等实时信息应按需
调用工具，不要假设或把高频 GPS 自动当作用户请求。如果当前麦克风内容明显是在
回答你刚才的问题，且是低风险、可逆的操作（例如取消提醒、停止播放、修改一个
提醒），你可以结合 conversation.pending_question、recent_microphone_events 和
当前内容自行判断是否执行；不要因为来源是 microphone 就一律要求改用文字确认。
连续 ASR 可能把一句话拆成多个片段：不完整片段先 action=remember 留存，或 ask_user
澄清；等最近片段合起来足够明确后再行动。不要用 ignore 丢弃。高风险、不可逆、对外发送、权限授予或脚本执行仍必须
走正式确认和权限策略。

你能做什么，以真实工具清单为准。下面这份是系统实际注册给你的，
不在清单里的能力就是没有，**不要声称能"跑起来"、能部署网站、能给用户一个可访问的网页**：

__TOOL_LIST__

产出交付物之后不要停在"已生成"。主动把链接给用户，并问清交付方式：
链接够不够用？要不要发到别处？页数要不要改？需不需要转成别的格式？
做不到的部分直接说不做不到，并给出你能做的替代方案。

JSON 格式：
{
  "action": "reply",
  "reply": "给用户的简短回复",
  "title": "",
  "run_at": null,
  "memory_content": "",
  "memory_type": "episodic",
  "target_agent": null,
  "capability": null,
  "payload": {},
  "urgency": 0,
  "interruption_allowed": false,
  "needs_confirmation": false,
  "reason": ""
}
"""
        payload = {
            "event": {
                "source": context.event.source.value,
                "type": context.event.event_type.value,
                "content": context.event.content,
                "timestamp": context.event.timestamp.isoformat(),
                "context": context.event.context,
            },
            "user": {
                "timezone": context.user.timezone,
                "preferences": context.user.preferences,
                "interruption_policy": context.user.interruption_policy,
            },
            "memories": [memory.content for memory in context.memories],
            "active_tasks": context.active_tasks,
            "conversation": context.conversation,
            "runtime_context": context.event.context.get("runtime_context", {}),
            "project": (
                {
                    "id": context.project.project_id,
                    "name": context.project.name,
                    "goal": context.project.goal,
                    "status": context.project.status,
                }
                if context.project
                else None
            ),
            "agents": agent_descriptions,
            "now": iso_now(),
        }
        # Fill the placeholder with the tools that actually exist, so the model
        # cannot promise a capability the system does not have.
        available = []
        if self.tool_registry is not None:
            available = [schema["name"] for schema in self.tool_registry.list_schemas()]
        tool_lines = "\n".join(f"- {name}" for name in available) or "- （当前没有任何工具）"
        capability_lines = [
            f"- {item['name']}：{item['description']}"
            + (f"（产出：{item['produces']}）" if item.get("produces") else "")
            + (
                "\n  交付提示：" + "；".join(item["delivery"])
                if item.get("delivery") else ""
            )
            for item in capability_descriptions()
        ]
        tool_lines += "\n\n已注册的扩展能力：\n" + (
            "\n".join(capability_lines) or "- （无）"
        )
        system = system.replace("__TOOL_LIST__", tool_lines)

        schema = {
            "action": "reply",
            "reply": "",
            "title": "",
            "run_at": None,
            "memory_content": "",
            "memory_type": "episodic",
            "target_agent": None,
            "capability": None,
            "payload": {},
            "urgency": 0,
            "interruption_allowed": False,
            "needs_confirmation": False,
            "reason": "",
            "project_id": None,
        }
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, default=str),
            },
        ]
        tools = self._tool_schemas()
        if tools and self.tool_runtime is not None:
            blocked_tool: dict[str, Any] = {}

            async def call_tool(
                name: str,
                arguments: dict[str, Any],
            ) -> dict[str, Any]:
                value = await self.invoke_tool(context, name, arguments)
                if value.get("blocked"):
                    blocked_tool.update(
                        capability=value.get("capability"),
                        scope=value.get("scope"),
                        name=value.get("name") or name,
                        arguments=arguments,
                    )
                return value

            loop = AgentLoop(
                llm=complete_with_tools,
                tool_executor=call_tool,
                max_rounds=8,
                llm_timeout=90,
                tool_timeout=30,
            )
            loop_result = await loop.run(messages, tools)
            if loop_result.completed and loop_result.final_text:
                result = self._parse_decision(loop_result.final_text, schema)
                if blocked_tool:
                    result.update({
                        "action": "confirm",
                        "needs_confirmation": True,
                        "target_agent": "tool",
                        "capability": blocked_tool["capability"],
                        "payload": {
                            **(result.get("payload") or {}),
                            "scope_kind": "tool",
                            "scope_value": blocked_tool["scope"],
                            "tool_name": blocked_tool["name"],
                            "tool_arguments": blocked_tool["arguments"],
                        },
                        "reply": "这个工具操作需要单独许可。",
                        "reason": "tool_permission_required",
                    })
            else:
                # A broken run is a failure, not a question. Telling the user
                # "please try later" hid a real cause and then parked the text in
                # conversations as an unanswerable pending question.
                cause = str(loop_result.error or loop_result.status or "")
                result = {
                    **schema,
                    "action": "reply",
                    "reply": _failure_reply(cause),
                    "reason": f"run_failed:{cause}",
                    "failed": True,
                }
        else:
            result = await decide(messages, schema)
        raw_action = str(result.get("action", "reply")).lower()
        try:
            action = DecisionAction(raw_action)
        except ValueError:
            action = DecisionAction.ASK_USER
        result = coerce_ignored_decision(result, str(context.event.content))
        action = DecisionAction.REMEMBER if result.get("action") == "remember" else action
        return PolicyDecision(
            action=action,
            failed=bool(result.get("failed")),
            reason=str(result.get("reason") or ""),
            urgency=int(result.get("urgency") or 0),
            interruption_allowed=bool(result.get("interruption_allowed")),
            needs_confirmation=bool(result.get("needs_confirmation")),
            target_agent=result.get("target_agent"),
            capability=result.get("capability"),
            payload=result.get("payload") or {},
            reply=str(result.get("reply") or ""),
            title=str(result.get("title") or ""),
            run_at=result.get("run_at"),
            memory_content=str(result.get("memory_content") or ""),
            memory_type=str(result.get("memory_type") or "episodic"),
            project_id=result.get("project_id") or (
                context.project.project_id if context.project else None
            ),
            confidence=result.get("confidence"),
            confidence_basis="model" if result.get("confidence") is not None else None,
        )

    def _tool_audit(self, event_id: str, action: str, detail: Any) -> None:
        """The tool loop audits through this. It previously existed only on the
        orchestrator, so any gated tool call raised AttributeError at runtime."""
        if self._audit is not None:
            # Injected by JarvisOrchestrator as its own _tool_audit(event_id,
            # action, detail). Passing a task_id here broke every gated call.
            self._audit(event_id, action, detail)

    async def invoke_tool(
        self,
        context: Context,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """The single place the model loop may invoke a tool.

        The gate lives here, before execution, because validating the final
        decision afterwards is too late: the side effect already happened.
        Extracted from the loop closure so it can be tested directly.
        """
        actual_name = self._llm_tool_names.get(name, name)
        capability = capability_for_tool(actual_name)
        event_id = context.event.event_id
        scope = None
        if capability:
            encoded = json.dumps(
                [actual_name, arguments],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            scope = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            blocked = {"name": actual_name, "capability": capability, "scope": scope}
            if self.permission_service is None:
                return {"ok": False, "blocked": "deny", **blocked}
            try:
                self.permission_service.validate_permission(
                    capability,
                    {"scope_kind": "tool", "scope_value": scope},
                    event_id,
                    subject="user",
                )
            except PermissionDenied:
                self._tool_audit(event_id, "tool_blocked", blocked | {"blocked": "deny"})
                return {"ok": False, "blocked": "deny", **blocked}
            except PermissionUnavailable:
                self._tool_audit(
                    event_id, "tool_blocked", blocked | {"blocked": "needs_permission"}
                )
                return {"ok": False, "blocked": "needs_permission", **blocked}
            self._tool_audit(event_id, "tool_gate_passed", blocked)

        result = await self.tool_runtime.execute(
            actual_name, arguments, permissions=context.user.permissions
        )
        value = result.to_dict()
        self._tool_audit(event_id, "tool_call", {
            "name": actual_name,
            "llm_name": name,
            "arguments": arguments,
            "result": value,
        })
        return value

    @staticmethod
    def _parse_decision(text: str, schema: dict[str, Any]) -> dict[str, Any]:
        content = text.strip()
        if "```" in content:
            content = content.replace("```json", "").replace("```", "").strip()
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            start = content.find("{")
            end = content.rfind("}")
            if start < 0 or end <= start:
                return {
                    **schema,
                    "action": "reply",
                    "reply": text,
                }
            try:
                value = json.loads(content[start : end + 1])
            except json.JSONDecodeError:
                return {
                    **schema,
                    "action": "reply",
                    "reply": text,
                }
        if not isinstance(value, dict):
            return {**schema, "action": "reply", "reply": str(value)}
        return {**schema, **value}


class Planner:
    def create_plan(self, context: Context, decision: PolicyDecision) -> Plan:
        task = TaskItem(
            task_id=str(uuid.uuid4()),
            title=decision.title or str(context.event.content),
            input_data=decision.payload,
            target_agent=decision.target_agent,
            capability=decision.capability,
            run_at=self.parse_datetime(decision.run_at, context.user.timezone),
            status=(
                TaskStatus.WAITING_CONFIRMATION
                if decision.needs_confirmation
                else TaskStatus.PENDING
            ),
        )
        return Plan(
            plan_id=str(uuid.uuid4()),
            goal=task.title,
            tasks=[task],
            requires_confirmation=decision.needs_confirmation,
        )

    @staticmethod
    def parse_datetime(
        value: str | None,
        timezone_name: str = "Asia/Shanghai",
    ) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed.astimezone(timezone.utc)
        try:
            from zoneinfo import ZoneInfo

            return parsed.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(timezone.utc)
        except Exception:
            return parsed.replace(tzinfo=timezone.utc)


class TaskExecutor:
    def __init__(
        self,
        database: Any,
        agents: AgentRegistry,
        audit: Callable[[str | None, str | None, str, Any], None],
    ) -> None:
        self.database = database
        self.agents = agents
        self.audit = audit

    async def execute(self, task: TaskItem, user: UserProfile) -> ExecutionResult:
        if not task.target_agent or not task.capability:
            return ExecutionResult(False, error="任务没有目标 Agent 或能力")
        agent = self.agents.find(task.target_agent, task.capability)
        if agent is None:
            return ExecutionResult(False, error="找不到可执行的 Agent 能力")
        capability = agent.capabilities[task.capability]
        missing = capability.required_permissions - user.permissions
        if missing:
            return ExecutionResult(
                False,
                error=f"缺少权限：{', '.join(sorted(missing))}",
            )
        task.status = TaskStatus.RUNNING
        self.audit(None, task.task_id, "task_started", task.title)
        result = await agent.execute(task.capability, task.input_data)
        task.status = TaskStatus.DONE if result.success else TaskStatus.FAILED
        task.result = result.output
        task.error = result.error
        self.audit(
            None,
            task.task_id,
            "task_finished" if result.success else "task_failed",
            result.output or result.error,
        )
        return result


def coerce_ignored_decision(result: dict, content: str) -> dict:
    """The model may not drop content; keep it and record why we did.

    Dropping stays a rule-level operation with its own receipt. A model that
    answers "ignore" would otherwise silently lose things like a spoken homework
    note, which is exactly what the v1 production data shows happening.
    """
    if str(result.get("action") or "").lower() != "ignore":
        return result
    return {
        **result,
        "action": "remember",
        "memory_content": result.get("memory_content") or str(content)[:500],
        "reason": "coerced_from_ignore:" + str(result.get("reason") or ""),
    }


def _failure_reply(cause: str) -> str:
    """Name the cause. Never answer a failure with a bare "try again later"."""
    text = (cause or "").lower()
    if any(k in text for k in ("402", "insufficient", "quota", "balance", "payment")):
        return "上游模型额度不足，这次没有完成。已经保留你的原始输入，额度恢复后可以重试。"
    if any(k in text for k in ("401", "403", "unauthorized", "api key", "forbidden")):
        return "模型鉴权失败，这次没有完成。需要检查服务端的模型密钥。"
    if any(k in text for k in ("timeout", "timed out", "deadline")):
        return "模型响应超时，这次没有完成。已经保留你的原始输入，可以重试。"
    if any(k in text for k in ("connect", "network", "dns", "unreachable")):
        return "连接模型服务失败，这次没有完成。已保留原始输入。"
    detail = cause[:60] if cause else "未知原因"
    return f"这次没有完成（{detail}）。已保留你的原始输入。"


class JarvisOrchestrator:
    def __init__(
        self,
        database: Any,
        memory: MemoryStore,
        agents: AgentRegistry,
        user: UserProfile,
        push: PushCallback | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        self.database = database
        self.event_publisher: Callable[[dict[str, Any]], None] | None = None
        self.memory = memory
        self.agents = agents
        self.user = user
        self.push = push
        self.bus = EventBus()
        self.normalizer = EventNormalizer()
        self.relevance = RelevanceFilter(database)
        self.ignore_rules = IgnoreRuleEngine(database)
        self.context_builder = ContextBuilder(memory, database, user)
        self.classifier = AIIntentClassifier(agents, tools, self._tool_audit)
        self.policy = PolicyEngine(
            agents.find,
            trusted_mode=bool(user.preferences.get("trusted_mode", False)),
        )
        self.planner = Planner()
        self.executor = TaskExecutor(database, agents, self.audit)
        self.interruption = InterruptionManager()

    def _tool_audit(self, event_id: str, action: str, detail: Any) -> None:
        self.audit(event_id, None, action, detail)

    async def handle(self, event: Event) -> dict[str, Any]:
        event = await self.normalizer.process(event)
        if not await self.relevance.should_process(event):
            self.record_event(event, decision_action=DecisionAction.IGNORE.value)
            self.audit(event.event_id, None, "event_ignored", {
                "reason": event.context.get("ignored_reason", "irrelevant"),
            })
            return {
                "action": DecisionAction.IGNORE.value,
                "event_id": event.event_id,
                "reply": "已忽略重复事件。",
                "reason": event.context.get("ignored_reason", "irrelevant"),
            }
        rule = self.ignore_rules.match(event)
        if rule:
            self.record_event(event, decision_action=DecisionAction.IGNORE.value)
            self.audit(event.event_id, None, "event_ignored", {
                "reason": "ignore_rule",
                "rule_id": rule["id"],
                "rule_name": rule["name"],
            })
            return {
                "action": DecisionAction.IGNORE.value,
                "event_id": event.event_id,
                "reply": "已按忽略规则跳过。",
                "reason": f"ignore_rule:{rule['name']}",
            }
        self.record_event(event)
        # Attribute any model degradation to the activity it happened in.
        current_event.set(event.event_id)
        context = await self.context_builder.build(event)
        decision = await self.classifier.classify(context)
        try:
            decision = self.policy.validate(decision, context)
        except PermissionDenied as exc:
            # A hard deny must stay a decision, not an exception: the caller
            # records whatever validate() returns.
            self.audit(event.event_id, None, "permission_denied", {"reason": str(exc)})
            decision = PolicyDecision(
                action=DecisionAction.REPLY,
                reply="这个动作被当前的权限设置禁止了。",
                reason=f"permission_denied:{exc}",
            )
        except PermissionUnavailable as exc:
            # Missing grant becomes the existing confirmation flow so the user
            # can authorise exactly this one action.
            self.audit(event.event_id, None, "permission_requested", {"reason": str(exc)})
            decision = PolicyDecision(
                action=DecisionAction.CONFIRM,
                reply="这个操作需要你单独许可。",
                reason="permission_grant_required",
                needs_confirmation=True,
                target_agent=decision.target_agent,
                capability=decision.capability,
                payload={
                    **decision.payload,
                    **exc.details,
                    "expires_at": (utc_now() + timedelta(minutes=15)).isoformat(),
                },
            )
        self.record_event(event, decision_action=decision.action.value)
        decision.interruption_allowed = self.interruption.can_interrupt(
            decision,
            context,
        )
        self.audit(event.event_id, None, "policy_decision", decision.__dict__)

        if decision.action == DecisionAction.IGNORE:
            return self.result(event, decision)
        if decision.action == DecisionAction.REPLY:
            return self.result(event, decision)
        if decision.action == DecisionAction.REMEMBER:
            if decision.memory_content:
                memory = await self.save_memory(decision, event)
                return self.result(event, decision, memory=memory)
            return self.result(event, decision)
        if decision.action in {
            DecisionAction.ASK_USER,
            DecisionAction.SUGGEST,
        }:
            return self.result(event, decision)
        if decision.action == DecisionAction.CONFIRM:
            pending = self._save_pending(event, decision)
            return self.result(
                event,
                decision,
                confirmation_id=pending["id"],
                needs_confirmation=True,
            )
        if decision.action == DecisionAction.CREATE_TASK:
            plan = self.planner.create_plan(context, decision)
            if plan.requires_confirmation:
                pending = self.database.fetch_one(
                    "SELECT id FROM pending_actions WHERE event_id = ?",
                    (event.event_id,),
                )
                if not pending:
                    pending = self._save_pending(event, decision)
                return self.result(
                    event,
                    decision,
                    plan=plan.__dict__,
                    confirmation_id=pending["id"],
                    needs_confirmation=True,
                )
            task = self.persist_task(plan.tasks[0], event)
            return self.result(event, decision, task=task)
        if decision.action == DecisionAction.EXECUTE:
            plan = self.planner.create_plan(context, decision)
            if plan.requires_confirmation:
                pending = self._save_pending(event, decision)
                return self.result(
                    event,
                    decision,
                    plan=plan.__dict__,
                    confirmation_id=pending["id"],
                    needs_confirmation=True,
                )
            execution = await self.executor.execute(plan.tasks[0], context.user)
            return self.result(event, decision, execution=execution.__dict__)
        return self.result(event, decision)

    def _save_pending(self, event: Event, decision: PolicyDecision) -> dict[str, Any]:
        pending = self.database.fetch_one(
            """
            INSERT INTO pending_actions
            (id, event_id, action, title, run_at, target_agent, capability, payload,
             device_id, project_id, scope_kind, scope_value, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING *
            """,
            (
                str(uuid.uuid4()), event.event_id, decision.action.value,
                decision.title or str(event.content), decision.run_at,
                decision.target_agent, decision.capability,
                json.dumps(decision.payload, ensure_ascii=False, default=str),
                event.context.get("device_id"), decision.project_id,
                decision.payload.get("scope_kind"), decision.payload.get("scope_value"),
                iso_now(),
                (utc_now() + timedelta(minutes=15)).isoformat(),
            ),
        ) or {}
        if self.event_publisher:
            self.event_publisher({
                "event": "pending_action",
                "action": "created",
                "id": pending.get("id"),
                "capability": pending.get("capability"),
                "title": pending.get("title"),
            })
        return pending

    async def save_memory(
        self,
        decision: PolicyDecision,
        event: Event,
    ) -> dict[str, Any]:
        from .domain import MemoryItem

        item = MemoryItem(
            memory_id=str(uuid.uuid4()),
            memory_type=decision.memory_type,
            content=decision.memory_content,
            created_at=utc_now(),
            source_event_id=event.event_id,
        )
        await self.memory.save(item)
        self.audit(event.event_id, None, "memory_saved", item.__dict__)
        return item.__dict__

    def persist_task(self, task: TaskItem, event: Event) -> dict[str, Any]:
        timestamp = iso_now()
        self.database.execute(
            """
            INSERT INTO tasks
            (id, title, status, run_at, source, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.task_id,
                task.title,
                task.status.value,
                task.run_at.isoformat() if task.run_at else None,
                "orchestrator",
                json.dumps(
                    {
                        "device_id": event.context.get("device_id"),
                        "event_id": event.event_id,
                        "input": task.input_data,
                        "project_id": event.context.get("project_id"),
                    },
                    ensure_ascii=False,
                ),
                timestamp,
                timestamp,
            ),
        )
        row = self.database.fetch_one(
            "SELECT * FROM tasks WHERE id = ?", (task.task_id,)
        )
        self.audit(event.event_id, task.task_id, "task_created", row)
        return row or {}

    def result(
        self,
        event: Event,
        decision: PolicyDecision,
        **extra: Any,
    ) -> dict[str, Any]:
        result = {
            "event_id": event.event_id,
            "action": decision.action.value,
            "reply": decision.reply,
            "reason": decision.reason,
            "interrupt": decision.interruption_allowed,
            **({"failed": True} if decision.failed else {}),
            **extra,
        }
        self.audit(event.event_id, None, "event_result", result)
        return result

    async def submit(self, event: Event) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        result: asyncio.Future[Any] = loop.create_future()
        await self.bus.publish(event, result)
        return await result

    async def run_worker(self) -> None:
        async for event, result in self.bus.subscribe():
            try:
                value = await self.handle(event)
                if result and not result.done():
                    result.set_result(value)
            except Exception as exc:
                if result and not result.done():
                    result.set_exception(exc)

    def record_event(
        self,
        event: Event,
        decision_action: str | None = None,
    ) -> None:
        self.database.execute(
            """
            INSERT OR REPLACE INTO events
            (id, source, event_type, content, context, confidence, dedupe_key,
             decision_action, processed_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.source.value,
                event.event_type.value,
                str(event.content),
                json.dumps(event.context, ensure_ascii=False, default=str),
                event.confidence,
                event.context.get("content_hash"),
                decision_action,
                iso_now(),
                event.timestamp.isoformat(),
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
                iso_now(),
            ),
        )
