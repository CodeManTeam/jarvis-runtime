"""The gate must sit before the tool runs, not after the decision.

The production incident this covers: `device.open_url` was executed with
`confirmation_id = None`, because policy only validated the final decision while
the tool loop had already acted.
"""

import asyncio
import hashlib
import json

import pytest

from app.db import Database
from app.domain import Context, Event, EventSource, EventType, UserProfile
from app.policy import capability_for_tool
from app.service import JarvisService
from app.tools import ToolDefinition, ToolRegistry

URL_ARGS = {"url": "https://example.com", "device_id": "w1"}


def scope_of(tool: str, arguments: dict) -> str:
    encoded = json.dumps(
        [tool, arguments], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def context_for() -> Context:
    event = Event(
        event_id="ev-gate",
        source=EventSource.USER,
        event_type=EventType.TEXT,
        timestamp=None,
        content="open a link",
    )
    return Context(event=event, user=UserProfile(user_id="u1"))


@pytest.fixture
def harness(tmp_path):
    db = Database(str(tmp_path / "gate.db"))
    db.init()
    service = JarvisService(db, artifacts_root=tmp_path / "artifacts")
    calls: list[dict] = []

    def handler(args):
        calls.append(args)
        return {"ok": True, "opened": args.get("url")}

    # Assemble exactly like production does, so the audit callback is the real
    # one rather than a lookalike that hides arity bugs.
    registry = service.tools
    registry.register(
        ToolDefinition(
            name="device.open_url",
            description="open a url on the device",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}, "device_id": {"type": "string"}},
                "required": ["url", "device_id"],
                "additionalProperties": False,
            },
            handler=handler,
        )
    )
    return service, service.orchestrator.classifier, calls


def invoke(target, name, arguments):
    return asyncio.run(target.invoke_tool(context_for(), name, arguments))


def grant(service, arguments):
    service.create_permission_grant({
        "capability": "device.open_url",
        "level": "per_action",
        "scope_kind": "tool",
        "scope_value": scope_of("device.open_url", arguments),
        "expires_at": "2099-01-01T00:00:00+00:00",
        "single_use": True,
    })


def test_the_mapping_is_explicit():
    assert capability_for_tool("device.open_url") == "device.open_url"
    assert capability_for_tool("memory.search") is None
    assert capability_for_tool("task.list") is None


def test_unauthorised_tool_never_runs(harness):
    service, orchestrator, calls = harness
    result = invoke(orchestrator, "device.open_url", URL_ARGS)

    assert result["blocked"] == "needs_permission"
    assert calls == [], "the handler must not run before permission exists"
    assert result["scope"] == scope_of("device.open_url", URL_ARGS)
    assert service.database.fetch_one(
        "SELECT 1 FROM audit_log WHERE action = 'tool_blocked'"
    )


def test_an_authorised_call_runs_exactly_once(harness):
    service, orchestrator, calls = harness
    grant(service, URL_ARGS)

    first = invoke(orchestrator, "device.open_url", URL_ARGS)
    assert first.get("success") is True
    assert first.get("output") == {"ok": True, "opened": URL_ARGS["url"]}
    assert len(calls) == 1

    second = invoke(orchestrator, "device.open_url", URL_ARGS)
    assert second.get("blocked") == "needs_permission"
    assert len(calls) == 1, "a spent grant must not authorise a second run"


def test_changed_arguments_need_a_new_permission(harness):
    service, orchestrator, calls = harness
    grant(service, URL_ARGS)

    changed = {"url": "https://evil.example", "device_id": "w1"}
    result = invoke(orchestrator, "device.open_url", changed)
    assert result["blocked"] == "needs_permission"
    assert calls == []


def test_a_tool_without_a_capability_runs_without_a_grant(harness):
    """No capability means no gate: the handler is reached and nothing is looked up."""
    service, orchestrator, calls = harness

    from app.tools import ToolDefinition

    def echo(_args):
        calls.append({"echo": True})
        return {"ok": True}

    orchestrator.tool_runtime.registry.register(
        ToolDefinition(
            name="debug.echo",
            description="read-only test tool",
            input_schema={"type": "object", "properties": {}, "additionalProperties": True},
            handler=echo,
        )
    )
    result = invoke(orchestrator, "debug.echo", {"x": 1})
    assert result.get("success") is True
    assert calls == [{"echo": True}]


def test_a_declared_capability_pack_is_gated_too():
    # Capability packs declare their own capability, so the same gate applies.
    assert capability_for_tool("artifact.create_pptx") == "artifact.create"

def test_an_auto_capability_runs_without_a_grant(harness):
    """`artifact.create` is auto: the gate must pass, not raise.

    This path was untested, and the missing coverage hid a TypeError that made
    every real pptx request fail in production.
    """
    service, orchestrator, _calls = harness
    result = invoke(
        orchestrator,
        "artifact.create_pptx",
        {"title": "闸门自检", "slides": [{"title": "一", "bullets": ["a"]}]},
    )
    assert result.get("success") is True, result
    assert result["output"]["code"]
    assert result["output"]["path"].startswith("/files/")
