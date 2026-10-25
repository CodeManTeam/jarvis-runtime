import asyncio

from app.db import Database
from app.jobs import JobStore
from app.workspace_agent import WorkspaceAgent


def test_cancelled_job_does_not_add_steps(tmp_path):
    store = JobStore(Database(tmp_path / "db.sqlite"), tmp_path / "jobs")
    job = store.create("cancel", max_steps=5)
    store.set_status(job["id"], "cancelled")

    async def llm(messages, tools):
        raise AssertionError("cancelled job must not call llm")

    result = asyncio.run(WorkspaceAgent(store, llm=llm).run_job(job["id"]))
    assert result["status"] == "cancelled"
    assert result["steps"] == 0
