from __future__ import annotations
from typing import Any
from ..domain import PermissionLevel
from ..workspace_agent import workspace_shell
from .base import CapabilityPack

def _start(args: dict[str, Any], service: Any) -> dict[str, Any]:
    return service.start_job(str(args.get("goal") or ""), int(args.get("max_steps") or 60))

def _status(args: dict[str, Any], service: Any) -> dict[str, Any]:
    return service.job_store.get(str(args.get("job_id") or "")) or {"error": "job not found"}

async def _shell(args: dict[str, Any], service: Any) -> dict[str, Any]:
    job = service.job_store.get(str(args.get("job_id") or ""))
    if not job:
        return {"ok": False, "error": "job not found"}
    return await workspace_shell(str(args.get("command") or ""), __import__("pathlib").Path(job["workspace"]))

PACKS = [
    CapabilityPack("job.start", "Start a long-running workspace job.", {"type":"object","properties":{"goal":{"type":"string"},"max_steps":{"type":"integer"}},"required":["goal"]}, _start, "job.start", PermissionLevel.AUTO.value),
    CapabilityPack("job.status", "Read a workspace job.", {"type":"object","properties":{"job_id":{"type":"string"}},"required":["job_id"]}, _status, "job.status", PermissionLevel.AUTO.value),
    CapabilityPack("workspace.shell", "Run a safe shell command in a job workspace.", {"type":"object","properties":{"job_id":{"type":"string"},"command":{"type":"string"}},"required":["job_id","command"]}, _shell, "job.run_shell", PermissionLevel.PER_ACTION.value, levels=("auto","once","per_action","deny")),
]
