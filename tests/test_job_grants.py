"""A job may not run shell commands without an approval; accidents are tripwired."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.db import Database
from app.jobs import JobStore
from app.service import JarvisService
from app.workspace_agent import WorkspaceAgent, validate_command


@pytest.fixture
def env(tmp_path):
    database = Database(str(tmp_path / "jobs.db"))
    database.init()
    service = JarvisService(database, artifacts_root=tmp_path / "artifacts")
    store = JobStore(database, tmp_path / "jobs")
    return service, store


class FakeLlm:
    def __init__(self, commands):
        self.commands = list(commands)

    async def __call__(self, messages, tools):
        if not self.commands:
            return {"content": "done", "tool_calls": []}
        command = self.commands.pop(0)
        return {
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "workspace.shell",
                                                    "arguments": {"command": command}}}],
        }


def run(agent, job_id):
    return asyncio.run(agent.run_job(job_id))


def test_without_a_grant_the_shell_never_runs(env):
    service, store = env
    job = store.create("touch something")
    executed = []

    def executor(command, workspace):
        executed.append(command)
        return {"ok": True, "stdout": "should not happen"}

    agent = WorkspaceAgent(store, llm=FakeLlm(["echo hi"]), shell_executor=executor,
                           grant_checker=lambda job_id: False)
    store.set_status(job["id"], "running")
    result = run(agent, job["id"])

    assert result["status"] == "failed"
    assert "未获准" in (result.get("error") or "")
    assert executed == [], "the command must not execute without a grant"


def test_with_a_grant_it_runs(env):
    service, store = env
    job = store.create("touch something")
    executed = []

    def executor(command, workspace):
        executed.append(command)
        return {"ok": True, "stdout": "hi"}

    agent = WorkspaceAgent(store, llm=FakeLlm(["echo hi"]), shell_executor=executor,
                           grant_checker=lambda job_id: True)
    store.set_status(job["id"], "running")
    result = run(agent, job["id"])

    assert result["status"] == "done"
    assert executed == ["echo hi"]


def test_the_service_reports_grant_liveness(env):
    service, store = env
    assert service.has_live_grant("job.run_shell", "job", "j1") is False
    service.create_permission_grant({
        "capability": "job.run_shell",
        "level": "per_action",
        "scope_kind": "job",
        "scope_value": "j1",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "single_use": False,
    })
    assert service.has_live_grant("job.run_shell", "job", "j1") is True
    assert service.has_live_grant("job.run_shell", "job", "j2") is False


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "cd work && rm -rf /",
        "sudo ls",
        "cd ..",
        # Reads are deliberately allowed: a shell cannot be confined for reads,
        # and blocking them only breaks ordinary work. Writes are what the guard
        # confines; see tests/test_workspace_guard.py.
        "echo x > /etc/passwd",
        "mkfs.ext4 /dev/sda",
        "dd if=/dev/zero of=/dev/sda",
    ],
)
def test_dangerous_commands_are_refused(command):
    workspace = Path(tempfile.mkdtemp())
    assert validate_command(command, workspace) is not None


@pytest.mark.parametrize(
    "command",
    ['echo hi', "ls -la", 'python3 -c "print(1)"', "python3 -m http.server 8801"],
)
def test_normal_commands_pass(command):
    workspace = Path(tempfile.mkdtemp())
    assert validate_command(command, workspace) is None
