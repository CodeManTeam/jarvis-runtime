import asyncio
import json
from pathlib import Path

from app.db import Database
from app.jobs import JobStore
from app.workspace_agent import WorkspaceAgent, workspace_shell


def make_store(tmp_path):
    db = Database(tmp_path / "jarvis.db")
    return JobStore(db, tmp_path / "jobs")


def test_create_complete_workspace_file(tmp_path):
    store = make_store(tmp_path)
    job = store.create("create a file", max_steps=3)

    async def llm(messages, tools):
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "done", "tool_calls": []}
        return {"tool_calls": [{"id": "1", "function": {"name": "workspace.shell", "arguments": json.dumps({"command": "echo hi > result.txt"})}}]}

    result = asyncio.run(WorkspaceAgent(store, llm=llm).run_job(job["id"]))
    assert result["status"] == "done"
    assert (Path(job["workspace"]) / "result.txt").exists()


def test_boundary_rejected_without_execution(tmp_path):
    store = make_store(tmp_path)
    job = store.create("bad command")
    called = []

    async def executor(command, workspace):
        called.append(command)
        return {"ok": True}

    result = asyncio.run(workspace_shell("echo x > ../outside.txt", Path(job["workspace"]), executor))
    assert result["ok"] is False
    assert result["rejected"] is True
    assert called == []


def test_repeated_failures_mark_failed(tmp_path):
    store = make_store(tmp_path)
    job = store.create("fail", max_steps=5)

    async def llm(messages, tools):
        return {"tool_calls": [{"id": str(len(messages)), "function": {"name": "workspace.shell", "arguments": json.dumps({"command": "definitely-not-a-command"})}}]}

    result = asyncio.run(WorkspaceAgent(store, llm=llm).run_job(job["id"]))
    assert result["status"] == "failed"
    assert result["error"]


def test_trajectory_persist_and_reload(tmp_path):
    store = make_store(tmp_path)
    job = store.create("persist")
    messages = [{"role": "user", "content": "hello"}, {"role": "tool", "content": "world"}]
    store.save_trajectory(job["id"], messages)
    assert store.load_trajectory(job["id"]) == messages


def test_recover_running_jobs(tmp_path):
    store = make_store(tmp_path)
    job = store.create("resume")
    store.set_status(job["id"], "running")
    recovered = store.recover()
    assert [item["id"] for item in recovered] == [job["id"]]


def test_approve_keeps_a_queued_job_queued(tmp_path):
    """Approval is the gate a job must pass; it must not crash on the happy path."""
    store = make_store(tmp_path)
    job = store.create("needs approval")
    approved = store.approve(job["id"])
    assert approved["status"] == "queued"
    assert approved["error"] is None
    assert approved["updated_at"]


def test_approve_revives_a_failed_job_and_clears_the_error(tmp_path):
    store = make_store(tmp_path)
    job = store.create("was broken")
    store.set_status(job["id"], "failed", error="boom")
    revived = store.approve(job["id"])
    assert revived["status"] == "queued"
    assert revived["error"] is None


def test_approve_leaves_a_running_job_alone(tmp_path):
    store = make_store(tmp_path)
    job = store.create("already going")
    store.set_status(job["id"], "running")
    assert store.approve(job["id"])["status"] == "running"


def test_approve_returns_none_for_an_unknown_job(tmp_path):
    store = make_store(tmp_path)
    assert store.approve("does-not-exist") is None
