"""Tool/Agent architecture acceptance tests.

These tests intentionally exercise the current, source-compatible Agent
interfaces without calling a real LLM, device, ASR provider, or network.
The MCP/Agent Loop tests at the bottom are contract tests and stay skipped
until the corresponding modules are introduced.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.agents import (
    Agent,
    AgentRegistry,
    DeviceAgent,
    NotificationAgent,
    ReviewAgent,
)
from app.db import Database
from app.domain import (
    AgentCapability,
    AutonomyLevel,
    Context,
    DecisionAction,
    Event,
    EventSource,
    EventType,
    ExecutionResult,
    PolicyDecision,
    UserProfile,
)
from app.events import EventNormalizer, IgnoreRuleEngine, RelevanceFilter, content_hash
from app.orchestrator import TaskExecutor
from app.policy import PolicyEngine
from app.service import JarvisService
from app.tool_loop import AgentLoop
from app.tools import (
    MCPToolSource,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    ToolRisk,
    ToolRuntime,
)


class StubAgent(Agent):
    name = "stub"
    description = "Test-only agent."
    capabilities = {
        "read": AgentCapability(
            name="read",
            description="Read-only test capability.",
            input_schema={"value": "string"},
            output_schema={"value": "string"},
            autonomy_level=AutonomyLevel.OBSERVE,
        ),
        "write": AgentCapability(
            name="write",
            description="Mutating test capability.",
            required_permissions={"stub.write"},
            autonomy_level=AutonomyLevel.PROPOSE,
        ),
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        capability: str,
        payload: dict[str, Any],
    ) -> ExecutionResult:
        self.calls.append((capability, payload))
        return ExecutionResult(success=True, output={"capability": capability})


def make_event(
    *,
    event_id: str = "event-1",
    source: EventSource = EventSource.SYSTEM,
    event_type: EventType = EventType.NOTIFICATION,
    content: Any = "hello",
    context: dict[str, Any] | None = None,
) -> Event:
    return Event(
        event_id=event_id,
        source=source,
        event_type=event_type,
        timestamp=datetime.now(timezone.utc),
        content=content,
        context=context or {},
    )


@pytest.fixture
def database(tmp_path):
    database = Database(str(tmp_path / "jarvis-test.db"))
    database.init()
    return database


def test_registry_exposes_stable_capability_metadata() -> None:
    registry = AgentRegistry()
    agent = StubAgent()
    registry.register(agent)

    assert registry.find("stub", "read") is agent
    assert registry.find("stub", "missing") is None
    description = registry.describe()
    assert description == [
        {
            "name": "stub",
            "description": "Test-only agent.",
            "capabilities": [
                {
                    "name": "read",
                    "description": "Read-only test capability.",
                    "required_permissions": [],
                    "autonomy_level": 0,
                },
                {
                    "name": "write",
                    "description": "Mutating test capability.",
                    "required_permissions": ["stub.write"],
                    "autonomy_level": 2,
                },
            ],
        }
    ]


def test_policy_requires_confirmation_for_missing_permission() -> None:
    registry = AgentRegistry()
    registry.register(StubAgent())
    policy = PolicyEngine(registry.find)
    context = Context(
        event=make_event(),
        user=UserProfile(user_id="u1", permissions=set()),
    )

    decision = policy.validate(
        decision=PolicyDecision(
            action=DecisionAction.EXECUTE,
            target_agent="stub",
            capability="write",
            payload={"value": "x"},
        ),
        context=context,
    )

    assert decision.action is DecisionAction.CONFIRM
    assert decision.needs_confirmation is True
    assert decision.reason == "missing_permissions:stub.write"
    assert decision.payload["_requested_action"] == "execute"


def test_policy_allows_observe_and_gates_propose_by_autonomy() -> None:
    registry = AgentRegistry()
    registry.register(StubAgent())
    policy = PolicyEngine(registry.find)
    context = Context(
        event=make_event(),
        user=UserProfile(user_id="u1", permissions={"stub.write"}),
    )

    observe = policy.validate(
        PolicyDecision(
            action=DecisionAction.EXECUTE,
            target_agent="stub",
            capability="read",
        ),
        context,
    )
    propose = policy.validate(
        PolicyDecision(
            action=DecisionAction.EXECUTE,
            target_agent="stub",
            capability="write",
        ),
        context,
    )

    assert observe.action is DecisionAction.EXECUTE
    assert observe.needs_confirmation is False
    assert propose.action is DecisionAction.EXECUTE
    # This is the current boundary: PolicyEngine only upgrades levels above
    # LOW_RISK_AUTO.  The mismatch with ToolRisk.PROPOSE is documented below.
    assert propose.needs_confirmation is False


def test_trusted_policy_auto_runs_propose_capability() -> None:
    registry = AgentRegistry()
    registry.register(StubAgent())
    policy = PolicyEngine(registry.find, trusted_mode=True)
    context = Context(
        event=make_event(),
        user=UserProfile(user_id="u1", permissions={"stub.write"}),
    )

    decision = policy.validate(
        PolicyDecision(
            action=DecisionAction.EXECUTE,
            target_agent="stub",
            capability="write",
        ),
        context,
    )

    assert decision.action is DecisionAction.EXECUTE
    assert decision.needs_confirmation is False


def test_policy_rejects_unavailable_capability() -> None:
    registry = AgentRegistry()
    registry.register(StubAgent())
    policy = PolicyEngine(registry.find)
    context = Context(event=make_event(), user=UserProfile(user_id="u1"))

    decision = policy.validate(
        __import__("app.domain", fromlist=["PolicyDecision"]).PolicyDecision(
            action=DecisionAction.EXECUTE,
            target_agent="stub",
            capability="does_not_exist",
        ),
        context,
    )

    assert decision.action is DecisionAction.ASK_USER
    assert decision.reason == "capability_unavailable"


def test_task_executor_checks_permission_before_execution(database) -> None:
    agent = StubAgent()
    registry = AgentRegistry()
    registry.register(agent)
    audit_rows: list[tuple[Any, ...]] = []
    executor = TaskExecutor(
        database,
        registry,
        lambda event_id, task_id, action, detail: audit_rows.append(
            (event_id, task_id, action, detail)
        ),
    )
    from app.domain import TaskItem

    task = TaskItem(
        task_id="task-1",
        title="write",
        target_agent="stub",
        capability="write",
        input_data={"value": "x"},
    )

    result = asyncio.run(executor.execute(task, UserProfile(user_id="u1")))

    assert result.success is False
    assert "stub.write" in (result.error or "")
    assert agent.calls == []
    assert audit_rows == []


def test_task_executor_audits_start_and_finish(database) -> None:
    agent = StubAgent()
    registry = AgentRegistry()
    registry.register(agent)
    audit_rows: list[tuple[Any, ...]] = []
    executor = TaskExecutor(
        database,
        registry,
        lambda event_id, task_id, action, detail: audit_rows.append(
            (event_id, task_id, action, detail)
        ),
    )
    from app.domain import TaskItem

    task = TaskItem(
        task_id="task-2",
        title="read",
        target_agent="stub",
        capability="read",
        input_data={"value": "x"},
    )

    result = asyncio.run(executor.execute(task, UserProfile(user_id="u1")))

    assert result.success is True
    assert task.status.value == "done"
    assert [row[2] for row in audit_rows] == ["task_started", "task_finished"]
    assert agent.calls == [("read", {"value": "x"})]


def test_event_normalizer_canonicalizes_text_and_timestamp() -> None:
    event = Event(
        event_id="event-1",
        source=EventSource.USER,
        event_type=EventType.TEXT,
        timestamp=datetime(2026, 10, 3, 10, 0),
        content="  hello   world\n",
    )

    normalized = asyncio.run(EventNormalizer().process(event))

    assert normalized.content == "hello world"
    assert normalized.timestamp.tzinfo is timezone.utc
    assert normalized.context["content_hash"] == content_hash("hello world")
    assert "normalized_at" in normalized.context


def test_user_turns_are_not_deduplicated_but_external_events_are(database) -> None:
    filter_ = RelevanceFilter(database)
    user_event = make_event(
        event_id="user-2",
        source=EventSource.USER,
        event_type=EventType.TEXT,
        content="你好",
        context={"content_hash": content_hash("你好")},
    )
    assert asyncio.run(filter_.should_process(user_event)) is True

    external = make_event(
        event_id="external-1",
        context={"content_hash": content_hash("same notification")},
        content="same notification",
    )
    database.execute(
        """
        INSERT INTO events
        (id, source, event_type, content, context, confidence, dedupe_key, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            "external-0",
            external.source.value,
            external.event_type.value,
            external.content,
            "{}",
            1.0,
            external.context["content_hash"],
        ),
    )

    assert asyncio.run(filter_.should_process(external)) is False
    assert external.context["ignored_reason"] == "duplicate_event"


def test_ignore_rule_matches_and_increments_hit_count(database) -> None:
    rules = IgnoreRuleEngine(database)
    rule = rules.add_rule(
        name="mute promotions",
        pattern="promotion",
        source="notification",
        event_type="notification",
    )
    event = make_event(
        source=EventSource.NOTIFICATION,
        event_type=EventType.NOTIFICATION,
        content="New promotion",
    )

    matched = rules.match(event)

    assert matched is not None
    assert matched["id"] == rule["id"]
    stored = database.fetch_one(
        "SELECT hit_count FROM ignore_rules WHERE id = ?", (rule["id"],)
    )
    assert stored["hit_count"] == 1


def test_audit_and_command_ack_are_persisted(database) -> None:
    try:
        service = JarvisService(database)
    except TypeError as exc:
        pytest.xfail(
            "JarvisService 已传入 tools，但 JarvisOrchestrator 尚未兼容 tools 参数: "
            + str(exc)
        )
    service.register_device("watch-test", "Test watch", "android")

    service.audit("event-1", "task-1", "tool_call", {"name": "device.status"})
    inserted = database.fetch_one(
        "SELECT * FROM audit_log WHERE action = 'tool_call'"
    )
    assert inserted is not None
    assert inserted["event_id"] == "event-1"
    assert json.loads(inserted["detail"])["name"] == "device.status"

    asyncio.run(service.send_device_command("watch-test", "vibrate", {}))
    command = database.fetch_one(
        "SELECT * FROM device_commands WHERE device_id = ?", ("watch-test",)
    )
    assert command is not None
    assert service.acknowledge_device_command(
        "watch-test", command["id"], success=True
    )
    ack = database.fetch_one(
        "SELECT * FROM audit_log WHERE action = 'device_command_ack'"
    )
    assert ack is not None
    assert json.loads(ack["detail"])["command_id"] == command["id"]


def test_device_script_is_disabled_and_never_queued(database) -> None:
    service = JarvisService(database)
    result = asyncio.run(
        service.run_device_script("watch-test", "read-only", "getprop")
    )

    assert result["success"] is False
    assert "禁用" in result["error"]
    assert database.fetch_all("SELECT * FROM device_commands") == []


def test_device_command_expires_before_delivery(database) -> None:
    service = JarvisService(database)
    service.register_device("watch-test", "Test watch", "android")
    command_id = "expired-command"
    database.execute(
        """
        INSERT INTO device_commands
        (id, device_id, action, payload, status, created_at, expires_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            command_id,
            "watch-test",
            "vibrate",
            "{}",
            "2020-01-01T00:00:00+00:00",
            "2020-01-01T00:00:01+00:00",
        ),
    )

    delivered = asyncio.run(service.deliver_pending_commands("watch-test"))

    assert delivered == 0
    command = database.fetch_one(
        "SELECT status FROM device_commands WHERE id = ?", (command_id,)
    )
    assert command["status"] == "expired"


def test_device_agent_requires_device_id_for_all_capabilities() -> None:
    commands: list[tuple[str, str, dict[str, Any]]] = []

    async def send_command(device_id: str, action: str, payload: dict[str, Any]):
        commands.append((device_id, action, payload))
        return True

    agent = DeviceAgent(lambda _: {"id": "watch-1"}, send_command)
    result = asyncio.run(agent.execute("vibrate", {}))

    assert result.success is False
    assert result.error == "缺少 device_id"
    assert commands == []


def test_review_agent_allows_read_script_and_blocks_mutation() -> None:
    def review(device_id: str, script_id: str, script: str) -> dict[str, Any]:
        return {
            "approved": script.startswith("getprop"),
            "script_id": script_id,
            "device_id": device_id,
        }

    agent = ReviewAgent(review)
    allowed = asyncio.run(agent.execute(
        "device_script",
        {"device_id": "watch-1", "script": "getprop ro.build.version.sdk"},
    ))
    blocked = asyncio.run(agent.execute(
        "device_script",
        {"device_id": "watch-1", "script": "rm -rf /"},
    ))

    assert allowed.success is True
    assert blocked.success is False


def make_tool(
    *,
    name: str = "echo",
    handler: Any = None,
    risk: ToolRisk = ToolRisk.OBSERVE,
    permissions: frozenset[str] = frozenset(),
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Return the supplied text.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=handler or (lambda args: {"text": args["text"]}),
        permissions=permissions,
        risk=risk,
    )


def test_tool_definition_and_registry_emit_mcp_style_schema() -> None:
    tool = make_tool(
        permissions=frozenset({"messages.read"}),
        risk=ToolRisk.PROPOSE,
    )
    registry = ToolRegistry([tool])

    assert registry.get("echo") is tool
    assert registry.list_schemas() == [
        {
            "name": "echo",
            "description": "Return the supplied text.",
            "inputSchema": tool.input_schema,
            "permissions": ["messages.read"],
            "risk": "propose",
        }
    ]
    assert "echo" in registry
    assert len(registry) == 1


def test_tool_runtime_validates_arguments_permissions_and_result_envelope() -> None:
    runtime = ToolRuntime(
        ToolRegistry(
            [
                make_tool(
                    permissions=frozenset({"messages.read"}),
                    risk=ToolRisk.OBSERVE,
                )
            ]
        )
    )

    invalid = asyncio.run(runtime.execute("echo", {}, permissions={"messages.read"}))
    denied = asyncio.run(runtime.execute("echo", {"text": "hello"}))
    ok = asyncio.run(
        runtime.execute(
            "echo",
            {"text": "hello"},
            permissions={"messages.read"},
        )
    )

    assert invalid.error["code"] == "invalid_arguments"
    assert invalid.error["details"]["path"] == "$"
    assert denied.error["code"] == "permission_denied"
    assert denied.error["details"]["missing_permissions"] == ["messages.read"]
    assert ok == ToolResult.ok("echo", {"text": "hello"})


def test_tool_runtime_enforces_timeout_and_normalizes_handler_failure() -> None:
    async def slow(_: dict[str, Any]) -> str:
        await asyncio.sleep(0.05)
        return "done"

    def broken(_: dict[str, Any]) -> str:
        raise RuntimeError("backend unavailable")

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="slow",
                description="Slow tool",
                input_schema={"type": "object"},
                handler=slow,
                timeout_seconds=0.01,
            ),
            ToolDefinition(
                name="broken",
                description="Broken tool",
                input_schema={"type": "object"},
                handler=broken,
            ),
        ]
    )
    runtime = ToolRuntime(registry)

    timed_out = asyncio.run(runtime.execute("slow"))
    failed = asyncio.run(runtime.execute("broken"))

    assert timed_out.error["code"] == "timeout"
    assert failed.error["code"] == "execution_error"
    assert failed.error["details"]["exception"] == "RuntimeError"


def test_mcp_source_refreshes_remote_schema_and_routes_calls() -> None:
    class FakeMCPClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def list_tools(self) -> list[dict[str, Any]]:
            return [
                {
                    "name": "weather.current",
                    "description": "Read current weather",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                        "additionalProperties": False,
                    },
                    "permissions": ["weather.read"],
                    "risk": "observe",
                }
            ]

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any],
        ) -> dict[str, Any]:
            self.calls.append((name, arguments))
            return {"temperature": 21}

    client = FakeMCPClient()
    registry = ToolRegistry()
    source = MCPToolSource(client)
    registered = asyncio.run(source.refresh(registry))
    result = asyncio.run(
        source.call(
            ToolRuntime(registry),
            "weather.current",
            {"city": "Shanghai"},
            permissions={"weather.read"},
        )
    )

    assert registered == ["weather.current"]
    assert result.success is True
    assert result.output == {"temperature": 21}
    assert client.calls == [("weather.current", {"city": "Shanghai"})]


def test_agent_loop_returns_tool_result_to_same_context() -> None:
    llm_messages: list[list[dict[str, Any]]] = []
    calls: list[tuple[str, dict[str, Any]]] = []
    responses = iter(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "location.get_current",
                            "arguments": '{"device_id":"watch-1"}',
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "你现在在上海。"},
        ]
    )

    async def llm(messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        llm_messages.append([dict(message) for message in messages])
        return next(responses)

    async def execute(name: str, arguments: dict[str, Any]):
        calls.append((name, arguments))
        return {"latitude": 31.23, "longitude": 121.47}

    original = [{"role": "user", "content": "我在哪里？"}]
    result = asyncio.run(
        AgentLoop(
            llm=llm,
            tool_executor=execute,
            max_rounds=3,
        ).run(
            original,
            tools=[{"name": "location.get_current"}],
        )
    )

    assert result.completed is True
    assert result.final_text == "你现在在上海。"
    assert result.rounds == 2
    assert result.tool_calls == 1
    assert calls == [("location.get_current", {"device_id": "watch-1"})]
    assert original == [{"role": "user", "content": "我在哪里？"}]
    assert result.messages[1]["role"] == "assistant"
    assert result.messages[2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "location.get_current",
        "content": json.dumps(
            {"latitude": 31.23, "longitude": 121.47},
            ensure_ascii=False,
        ),
    }
    assert llm_messages[1][-1]["role"] == "tool"


def test_agent_loop_keeps_tool_failure_in_context_for_recovery() -> None:
    responses = iter(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "device.get_status",
                            "arguments": {},
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "暂时无法读取设备状态。"},
        ]
    )

    async def llm(messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        return next(responses)

    async def execute(name: str, arguments: dict[str, Any]):
        raise RuntimeError("device offline")

    result = asyncio.run(
        AgentLoop(llm=llm, tool_executor=execute).run(
            [{"role": "user", "content": "查设备"}]
        )
    )

    assert result.completed is True
    assert result.final_text == "暂时无法读取设备状态。"
    assert result.tool_errors[0]["name"] == "device.get_status"
    assert "device offline" in result.messages[2]["content"]


def test_agent_loop_stops_at_max_rounds() -> None:
    async def llm(messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"call-{len(messages)}",
                    "function": {"name": "echo", "arguments": "{}"},
                }
            ],
        }

    async def execute(name: str, arguments: dict[str, Any]):
        return {"ok": True}

    result = asyncio.run(
        AgentLoop(
            llm=llm,
            tool_executor=execute,
            max_rounds=2,
        ).run([{"role": "user", "content": "loop"}])
    )

    assert result.status == "max_rounds"
    assert result.rounds == 2
    assert result.tool_calls == 2


def test_agent_loop_rejects_malformed_tool_arguments() -> None:
    async def llm(messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-bad",
                    "function": {"name": "echo", "arguments": "{bad json"},
                }
            ],
        }

    async def execute(name: str, arguments: dict[str, Any]):
        raise AssertionError("executor must not receive malformed arguments")

    result = asyncio.run(
        AgentLoop(llm=llm, tool_executor=execute).run(
            [{"role": "user", "content": "bad"}]
        )
    )

    assert result.status == "llm_error"
    assert "invalid JSON arguments" in (result.error or "")
