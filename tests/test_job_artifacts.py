"""Job products must reach the user: a workspace the user cannot open is nothing."""

import asyncio
import json

import pytest

from app.db import Database
from app.jobs import JobStore
from app.service import JarvisService
from app.workspace_agent import WorkspaceAgent


@pytest.fixture
def env(tmp_path):
    database = Database(str(tmp_path / "bridge.db"))
    database.init()
    service = JarvisService(database, artifacts_root=tmp_path / "artifacts")
    store = JobStore(database, tmp_path / "jobs")
    return service, store


class ScriptedLlm:
    """Writes a file, then declares the job finished."""

    def __init__(self, steps):
        self.steps = list(steps)

    async def __call__(self, messages, tools):
        if not self.steps:
            return {"content": "done", "tool_calls": []}
        step = self.steps.pop(0)
        return {
            "content": "",
            "tool_calls": [{
                "id": f"c{len(self.steps)}",
                "function": {"name": step["tool"], "arguments": step["args"]},
            }],
        }


def run_agent(service, store, steps, job_id_holder):
    agent = WorkspaceAgent(
        store,
        llm=ScriptedLlm(steps),
        grant_checker=lambda job_id: True,
        artifacts=service.artifacts,
    )
    job = store.create("produce a report")
    job_id_holder.append(job["id"])
    store.set_status(job["id"], "running")
    asyncio.run(agent.run_job(job["id"]))
    return store.get(job["id"])


def test_a_delivered_file_becomes_an_artifact(env):
    service, store = env
    ids = []
    job = run_agent(service, store, [
        {"tool": "workspace_write", "args": {"path": "report.md", "content": "# 周报\n完成"}},
        {"tool": "workspace_deliver", "args": {"path": "report.md", "title": "本周周报"}},
    ], ids)

    produced = service.artifacts.for_job(ids[0])
    assert len(produced) == 1
    item = produced[0]
    assert item["title"] == "本周周报"
    assert item["code"] and len(item["code"]) == 4
    assert item["path"].startswith("/files/")
    # the file really exists in the artifact root and is readable
    stored = service.artifacts.root / item["path"].rsplit("/", 1)[-1]
    assert "完成" in stored.read_text(encoding="utf-8")


def test_files_are_collected_even_without_an_explicit_deliver(env):
    service, store = env
    ids = []
    run_agent(service, store, [
        {"tool": "workspace_write", "args": {"path": "a.txt", "content": "alpha"}},
        {"tool": "workspace_write", "args": {"path": "b.txt", "content": "beta"}},
    ], ids)

    produced = service.artifacts.for_job(ids[0])
    assert {item["title"] for item in produced} == {"a.txt", "b.txt"}, produced


def test_unsupported_files_are_skipped_not_faked(env):
    service, store = env
    ids = []
    run_agent(service, store, [
        {"tool": "workspace_write", "args": {"path": "blob.bin", "content": "??"}},
        {"tool": "workspace_write", "args": {"path": "note.txt", "content": "keep me"}},
    ], ids)

    produced = service.artifacts.for_job(ids[0])
    assert [item["title"] for item in produced] == ["note.txt"]


def test_deliver_refuses_paths_outside_the_workspace(env):
    service, store = env
    ids = []
    run_agent(service, store, [
        {"tool": "workspace_deliver", "args": {"path": "../secret.txt"}},
    ], ids)

    assert service.artifacts.for_job(ids[0]) == []


def test_the_job_api_reports_its_artifacts(env):
    service, store = env
    ids = []
    run_agent(service, store, [
        {"tool": "workspace_write", "args": {"path": "out.md", "content": "# done"}},
    ], ids)
    produced = service.artifacts.for_job(ids[0])
    assert produced, "the API reads this list"
    payload = json.dumps(produced, ensure_ascii=False)
    assert "http" in payload and "code" in payload
