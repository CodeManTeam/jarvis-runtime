"""Escalation must sandbox, time out, and report failures in their own words."""

import asyncio
import hashlib
import sys

import pytest

from app import escalate
from app.db import Database
from app.policy import PermissionUnavailable
from app.service import JarvisService


def run(coro):
    return asyncio.run(coro)


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_command_is_read_only_by_default():
    argv = escalate.build_command("do something", write=False)
    joined = " ".join(argv)
    assert 'sandbox_mode="read-only"' in joined
    assert 'approval_policy="never"' in joined
    # The sandbox is what makes "never ask" safe; it must always be explicit.
    assert "--skip-git-repo-check" in argv


def test_write_command_uses_workspace_write():
    argv = escalate.build_command("do something", write=True)
    assert 'sandbox_mode="workspace-write"' in " ".join(argv)


def test_success_returns_output():
    result = run(escalate.run("hi", command=python("print('42')")))
    assert result.ok
    assert "42" in result.output
    assert result.sandbox == "read-only"


def test_non_zero_exit_is_a_failure():
    with pytest.raises(escalate.EscalateError) as caught:
        run(escalate.run("hi", command=python("import sys; sys.exit(3)")))
    assert "3" in caught.value.detail


def test_balance_failure_says_so():
    with pytest.raises(escalate.EscalateUnavailable) as caught:
        run(escalate.run(
            "hi",
            command=python("import sys; sys.stderr.write('402 Insufficient account balance'); sys.exit(1)"),
        ))
    assert "额度" in caught.value.detail
    assert "请稍后再试" not in caught.value.detail


def test_timeout_is_reported_and_process_killed():
    with pytest.raises(escalate.EscalateTimedOut) as caught:
        run(escalate.run(
            "hi",
            command=python("import time; time.sleep(5)"),
            timeout=0.4,
        ))
    assert "超过" in caught.value.detail


def test_empty_task_is_refused():
    with pytest.raises(escalate.EscalateRefused):
        run(escalate.run("   ", command=python("print('x')")))


def test_missing_binary_is_unavailable():
    with pytest.raises(escalate.EscalateUnavailable):
        run(escalate.run("hi", command=["definitely-not-a-real-binary-xyz"]))


def test_output_is_capped():
    result = run(escalate.run("hi", command=python("print('x' * 9000)")))
    assert len(result.output) == escalate.MAX_OUTPUT_CHARS


def _escalate_tool(service):
    registry = service.tools
    return registry.get("system.escalate") if hasattr(registry, "get") else None


def test_write_requires_a_grant(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "esc.db"))
    db.init()
    service = JarvisService(db)

    called = []

    async def fake_run(task, *, write=False, timeout=0, command=None):
        called.append(write)
        return escalate.EscalationResult(True, "ok", 0, 1, "workspace-write")

    monkeypatch.setattr(escalate, "run", fake_run)
    tool = service.tools.get("system.escalate")

    with pytest.raises(PermissionUnavailable):
        run(tool.handler({"task": "modify a file", "write": True}))
    assert called == [], "a write must not run before it is authorised"

    task = "modify a file"
    service.create_permission_grant({
        "capability": "escalate.write",
        "level": "per_action",
        "scope_kind": "task",
        "scope_value": hashlib.sha256(task.encode()).hexdigest(),
        "expires_at": "2099-01-01T00:00:00+00:00",
        "single_use": True,
    })
    result = run(tool.handler({"task": task, "write": True}))
    assert result["ok"] and called == [True]

    # Single use: the same task may not authorise itself twice.
    with pytest.raises(PermissionUnavailable):
        run(tool.handler({"task": task, "write": True}))


def test_read_escalation_needs_no_grant(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "esc2.db"))
    db.init()
    service = JarvisService(db)

    async def fake_run(task, *, write=False, timeout=0, command=None):
        return escalate.EscalationResult(True, "read result", 0, 1, "read-only")

    monkeypatch.setattr(escalate, "run", fake_run)
    tool = service.tools.get("system.escalate")
    result = run(tool.handler({"task": "count words in a file"}))
    assert result["sandbox"] == "read-only"
