"""A job that delivers a file must end `done`, not `failed`.

The regression: the deliver tool returned an artifact dict without `ok`, and the
failure counter incremented on successes too, so three good steps produced a
"repeated command failure".
"""

import asyncio
import pathlib
import tempfile

from app.db import Database
from app.jobs import JobStore
from app.service import JarvisService
from app.workspace_agent import WorkspaceAgent


class ScriptedLlm:
    def __init__(self, steps):
        self.steps = list(steps)

    async def __call__(self, messages, tools):
        if not self.steps:
            return {"content": "done", "tool_calls": []}
        step = self.steps.pop(0)
        return {"content": "", "tool_calls": [{
            "id": "c1", "function": {"name": step["tool"], "arguments": step["args"]}}]}


def test_write_list_deliver_ends_done():
    tmp = pathlib.Path(tempfile.mkdtemp())
    database = Database(str(tmp / "x.db"))
    database.init()
    service = JarvisService(database, artifacts_root=tmp / "artifacts")
    store = JobStore(database, tmp / "jobs")

    agent = WorkspaceAgent(
        store,
        llm=ScriptedLlm([
            {"tool": "workspace_write", "args": {"path": "weekly.md", "content": "# 周报"}},
            {"tool": "workspace_list", "args": {"path": "."}},
            {"tool": "workspace_deliver", "args": {"path": "weekly.md"}},
        ]),
        grant_checker=lambda job_id: True,
        artifacts=service.artifacts,
    )
    job = store.create("write and deliver")
    store.set_status(job["id"], "running")
    final = asyncio.run(agent.run_job(job["id"]))

    assert final["status"] == "done", final.get("error")
    assert len(final["steps"] if isinstance(final["steps"], list) else []) in (0, 3)
    produced = service.artifacts.for_job(job["id"])
    assert len(produced) == 1
    assert produced[0]["code"]


def test_three_successes_never_trip_the_failure_counter():
    tmp = pathlib.Path(tempfile.mkdtemp())
    database = Database(str(tmp / "y.db"))
    database.init()
    service = JarvisService(database, artifacts_root=tmp / "artifacts")
    store = JobStore(database, tmp / "jobs")

    agent = WorkspaceAgent(
        store,
        llm=ScriptedLlm([
            {"tool": "workspace_list", "args": {"path": "."}},
            {"tool": "workspace_list", "args": {"path": "."}},
            {"tool": "workspace_list", "args": {"path": "."}},
            {"tool": "workspace_list", "args": {"path": "."}},
        ]),
        grant_checker=lambda job_id: True,
        artifacts=service.artifacts,
    )
    job = store.create("list four times")
    store.set_status(job["id"], "running")
    final = asyncio.run(agent.run_job(job["id"]))
    assert final["status"] == "done", final.get("error")
