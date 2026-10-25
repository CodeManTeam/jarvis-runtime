from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Awaitable, Callable

from .jobs import JobStore
from .llm import complete_with_tools
from .tool_loop import _serialize_content, compact

MAX_STEPS = 60
SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "workspace_shell",
        "description": "Run a command inside the job workspace and return its output.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    },
}
DELIVER_TOOL = {
    "type": "function",
    "function": {
        "name": "workspace_deliver",
        "description": (
            "Hand a finished file to the user: it is copied out of the workspace, "
            "gets a short code and a link, and appears in the console. Use it for "
            "the files the user should actually open."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["path"],
        },
    },
}

WORKSPACE_TOOLS = [
    SHELL_TOOL,
    {"type": "function", "function": {"name": "workspace_write", "description": "Write a file in the job workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "overwrite": {"type": "boolean"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "workspace_read", "description": "Read a file from the job workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "workspace_list", "description": "List files and directories in the job workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}}, "required": []}}},
    DELIVER_TOOL,
]


# Reads cannot be confined by a shell guard (any script can read /etc/passwd), so
# pretending otherwise only breaks ordinary commands. Writes are what matter.
SAFE_WRITE_PREFIXES = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/zero", "/tmp/")
WRITE_REDIRECT = re.compile(r"(?:^|[\s;|&])>{1,2}\s*([^\s;&|]+)")
WRITE_FLAGS = re.compile(r"(?:\s-o\s|\s--output[=\s])\s*([^\s;&|]+)")
WRITE_TEE = re.compile(r"\btee(?:\s+-a)?\s+([^\s;&|]+)")

DESTRUCTIVE = ("rm -rf /", "rm -rf ~", "mkfs", "dd if=", ":(){", "chmod -r 000 /")


def _write_targets(text: str) -> list[str]:
    targets: list[str] = []
    for pattern in (WRITE_REDIRECT, WRITE_FLAGS, WRITE_TEE):
        targets.extend(pattern.findall(text))
    return targets


def validate_command(command: str, workspace: Path) -> str | None:
    """Tripwire for accidents. The real control is the job-scoped approval."""
    text = command.strip()
    lowered = text.lower()
    if "sudo" in lowered:
        return "command rejected: privileged command"
    if ".." in text:
        return "command rejected: parent traversal is not allowed in the workspace"
    for pattern in DESTRUCTIVE:
        if pattern in lowered:
            return f"command rejected: destructive pattern ({pattern.strip()})"

    root = workspace.resolve()
    for target in _write_targets(text):
        if target in {"-", "&1", "&2"} or target.startswith("$"):
            continue
        if target.startswith(SAFE_WRITE_PREFIXES):
            continue
        reason = validate_path(target, workspace, allow_dot=True)
        if isinstance(reason, str):
            return f"command rejected: write target outside workspace ({target})"
    return None


def validate_path(path: str, workspace: Path, *, allow_dot: bool = False) -> Path | str:
    raw = str(path or "")
    if not raw or (raw == "." and not allow_dot):
        return "path must be relative"
    candidate = Path(raw)
    if candidate.is_absolute() or raw.startswith(("/", "\\", "~")) or (len(raw) > 1 and raw[1] == ":"):
        return "path must be relative"
    if any(part == ".." for part in candidate.parts):
        return "parent traversal is not allowed"
    resolved = (workspace / candidate).resolve()
    root = workspace.resolve()
    if resolved != root and root not in resolved.parents:
        return "path outside workspace"
    return resolved


async def workspace_write(path: str, content: str, workspace: Path, overwrite: bool = False) -> dict[str, Any]:
    target = validate_path(path, workspace)
    if isinstance(target, str):
        return {"ok": False, "error": target}
    if target.exists() and not overwrite:
        return {"ok": False, "error": f"file already exists: {path}; set overwrite=true to replace it"}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def workspace_read(path: str, workspace: Path, max_chars: int = 8000) -> dict[str, Any]:
    target = validate_path(path, workspace)
    if isinstance(target, str):
        return {"ok": False, "error": target}
    if not target.is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        text = target.read_text(encoding="utf-8")
        limit = max(1, int(max_chars or 8000))
        truncated = len(text) > limit
        return {"ok": True, "path": path, "content": text[:limit] + ("...[truncated]" if truncated else ""), "truncated": truncated}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def workspace_list(path: str = ".", workspace: Path = Path("."), depth: int = 1) -> dict[str, Any]:
    target = validate_path(path, workspace, allow_dot=True)
    if isinstance(target, str):
        return {"ok": False, "error": target}
    if not target.is_dir():
        return {"ok": False, "error": f"directory not found: {path}"}
    base_depth = len(target.relative_to(workspace.resolve()).parts)
    items = []
    for child in sorted(target.rglob("*")):
        rel_depth = len(child.relative_to(workspace.resolve()).parts) - base_depth
        if rel_depth > max(0, int(depth or 1)):
            continue
        items.append({"path": str(child.relative_to(workspace.resolve())).replace("\\", "/"), "type": "directory" if child.is_dir() else "file", "bytes": child.stat().st_size if child.is_file() else 0})
    return {"ok": True, "path": path, "items": items}


async def workspace_shell(command: str, workspace: Path,
                          executor: Callable[..., Any] | None = None) -> dict[str, Any]:
    reason = validate_command(command, workspace)
    if reason:
        return {"ok": False, "error": reason, "rejected": True}
    runner = executor or _run_subprocess
    try:
        value = runner(command, workspace)
        if isinstance(value, Awaitable):
            value = await value
        return value if isinstance(value, dict) else {"ok": True, "output": str(value)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _wants_background(command: str) -> bool:
    """Best-effort hint. Not load-bearing: the executor never relies on it."""
    stripped = command.strip()
    return stripped.endswith("&") or "nohup " in stripped or "disown" in stripped


async def _run_subprocess(command: str, workspace: Path) -> dict[str, Any]:
    """Run one command with file-backed output.

    Pipes were the bug: a backgrounded child inherits them, so communicate()
    waited for a server that was never going to exit, and the job stalled until
    the timeout. Output goes to temp files instead, and the process gets its own
    session so the whole group can be stopped at once.
    """
    import tempfile

    handle_out = tempfile.NamedTemporaryFile(prefix="jarvis-cmd-", suffix=".out", delete=False)
    handle_err = tempfile.NamedTemporaryFile(prefix="jarvis-cmd-", suffix=".err", delete=False)
    handle_out.close()
    handle_err.close()

    try:
        with open(handle_out.name, "wb") as out, open(handle_err.name, "wb") as err:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(workspace),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
        timed_out = False
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=60)
        except asyncio.TimeoutError:
            timed_out = True
            _stop_group(process.pid)
            try:
                returncode = await asyncio.wait_for(process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                returncode = -9

        stdout = Path(handle_out.name).read_text(errors="replace")[-4000:]
        stderr = Path(handle_err.name).read_text(errors="replace")[-4000:]
        if timed_out:
            return {
                "ok": False,
                "error": "command timed out after 60s",
                "stdout": stdout,
                "stderr": stderr,
                "returncode": returncode,
            }
        ok = returncode == 0
        return {
            "ok": ok,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            # Every command runs in its own session; remembering the pgid lets a
            # finished job stop anything the model left running in the background.
            "pgid": process.pid,
            "error": None if ok else (stderr.strip()[-400:] or f"exit {returncode}"),
        }
    finally:
        for name in (handle_out.name, handle_err.name):
            try:
                os.unlink(name)
            except OSError:
                pass


def _stop_group(pid: int) -> None:
    """Best effort: stop a command and anything it spawned."""
    import signal

    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass


class WorkspaceAgent:
    def __init__(self, store: JobStore, *, llm: Callable[..., Any] = complete_with_tools,
                 shell_executor: Callable[..., Any] | None = None,
                 push_callback: Callable[..., Awaitable[Any]] | None = None,
                 event_publisher: Callable[[dict[str, Any]], None] | None = None,
                 grant_checker: Callable[[str], bool] | None = None,
                 artifacts: Any = None) -> None:
        self.store, self.llm, self.shell_executor, self.push_callback = store, llm, shell_executor, push_callback
        self.event_publisher = event_publisher
        # A job may only touch the shell while a job-scoped grant is live. The
        # string guard is a tripwire for accidents, not a boundary.
        self.grant_checker = grant_checker
        self.artifacts = artifacts
        # Background pids per job, so a finished job leaves nothing running.
        self._background: dict[str, list[int]] = {}

    async def _emit(self, job: dict[str, Any], seq: int, kind: str, detail: Any, status: str) -> None:
        if self.push_callback:
            await self.push_callback(job["id"], {"type": "job_progress", "job_id": job["id"],
                "seq": seq, "kind": kind, "detail": detail, "status": status})
        if self.event_publisher:
            self.event_publisher({"event": "job_progress", "job_id": job["id"],
                "seq": seq, "kind": kind, "detail": detail, "status": status})

    def _deliver(self, job: dict[str, Any], path: str, title: Any) -> dict[str, Any]:
        """Copy a workspace file into the delivery chain (artifact + short code)."""
        if self.artifacts is None:
            return {"ok": False, "error": "delivery is not configured"}
        workspace = Path(job["workspace"])
        resolved = validate_path(path, workspace)
        if isinstance(resolved, str):  # validate_path returns a message on refusal
            return {"ok": False, "error": resolved}
        try:
            artifact = self.artifacts.adopt(
                resolved,
                title=(str(title).strip() if title else None),
                job_id=str(job["id"]),
            )
            # The loop judges success by `ok`; an artifact dict alone reads as a
            # failure and three of them used to fail the whole job.
            return {"ok": True, **artifact}
        except Exception as exc:  # noqa: BLE001 - report, never fake success
            detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
            return {"ok": False, "error": detail}

    def _auto_deliver(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        """If the model did not hand anything over, take what it left behind.

        A job that produces files but shows the user nothing is the gap this
        closes; skipping it would leave the work stranded in the workspace.
        """
        if self.artifacts is None:
            return []
        workspace = Path(job["workspace"])
        if not workspace.exists():
            return []
        delivered: list[dict[str, Any]] = []
        for path in sorted(workspace.rglob("*")):
            if len(delivered) >= 10:
                break
            if not path.is_file():
                continue
            if any(part.startswith(".") or part == "__pycache__" for part in path.parts):
                continue
            try:
                delivered.append(
                    self.artifacts.adopt(path, job_id=str(job["id"]))
                )
            except Exception:  # noqa: BLE001 - an unsupported file is not a failure
                continue
        return delivered

    def _stop_background(self, job_id: str) -> None:
        """A finished job must not leave a server running on the host."""
        import os
        import signal

        for pid in self._background.pop(job_id, []):
            _stop_group(pid)

    async def run_job(self, job_id: str) -> dict[str, Any]:
        try:
            return await self._run_job_inner(job_id)
        finally:
            self._stop_background(job_id)
            job = self.store.get(job_id)
            if job is not None and self.artifacts is not None:
                if not self.artifacts.for_job(job_id):
                    self._auto_deliver(job)

    async def _run_job_inner(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if not job:
            raise KeyError(job_id)
        if job["status"] == "cancelled":
            return job
        self.store.set_status(job_id, "running")
        job = self.store.get(job_id) or job
        messages = self.store.load_trajectory(job_id) or [
            {"role": "system", "content": (
                "You are a workspace agent working inside a sandbox directory. "
                "Use workspace_write to create or replace files (prefer it over shell "
                "heredocs), workspace_read to inspect a file, workspace_list to see the "
                "tree, and workspace_shell to run commands. Verify your work by running "
                "it; report honestly if something fails."
            )},
            {"role": "user", "content": job["goal"]},
        ]
        failures: dict[str, int] = {}
        while int(job["steps"]) < int(job["max_steps"]) and job["status"] == "running":
            # Measure with a full serialization: _serialize_content truncates at
            # 6000 chars, which made the reported saving look like 1 byte.
            before = len(json.dumps(messages, ensure_ascii=False, default=str).encode("utf-8"))
            compacted = compact(messages)
            if compacted != messages:
                after = len(json.dumps(compacted, ensure_ascii=False, default=str).encode("utf-8"))
                self.store.audit(job_id, "compacted", {
                    "messages": max(0, len(messages) - len(compacted)),
                    "bytes_saved": max(0, before - after),
                })
            messages = compacted
            try:
                reply = await self.llm(messages, WORKSPACE_TOOLS)
            except Exception as exc:
                self.store.set_status(job_id, "failed", error=str(exc))
                return self.store.get(job_id)  # type: ignore[return-value]
            calls = (reply or {}).get("tool_calls") or []
            if not calls:
                self.store.save_trajectory(job_id, messages + [{"role": "assistant", "content": (reply or {}).get("content", "")}])
                self.store.set_status(job_id, "done", result={"text": (reply or {}).get("content", "")})
                return self.store.get(job_id)  # type: ignore[return-value]
            for call in calls:
                if (self.store.get(job_id) or {}).get("status") == "cancelled":
                    self.store.audit(job_id, "job_cancelled", {"job_id": job_id})
                    return self.store.get(job_id)  # type: ignore[return-value]
                fn = call.get("function", call)
                args = (fn.get("arguments") or {}) if isinstance(fn.get("arguments"), dict) else {}
                command = str(args.get("command", ""))
                if not command:
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                        command = str(args.get("command", ""))
                    except Exception: command = ""
                if self.grant_checker is not None and not self.grant_checker(job_id):
                    # Without a live job-scoped grant the shell is off-limits; the
                    # job fails loudly rather than quietly doing something nobody
                    # approved.
                    self.store.step(
                        job_id, int(job["steps"]) + 1, "action",
                        {"command": command, "error": "未获准执行命令（缺少 job.run_shell 授权）"},
                        "error", "workspace.shell",
                    )
                    self.store.set_status(
                        job_id, "failed",
                        error="未获准执行命令：这个 job 没有 job.run_shell 授权",
                    )
                    return self.store.get(job_id)  # type: ignore[return-value]
                if self.grant_checker is not None and not self.grant_checker(job_id):
                    result = {"ok": False, "error": "missing job workspace grant"}
                elif fn.get("name") in {"workspace_shell", "workspace.shell"}:
                    result = await workspace_shell(command, Path(job["workspace"]), self.shell_executor)
                    if isinstance(result, dict) and result.get("pgid"):
                        self._background.setdefault(job_id, []).append(int(result["pgid"]))
                elif fn.get("name") == "workspace_write":
                    result = await workspace_write(str(args.get("path", "")), str(args.get("content", "")), Path(job["workspace"]), bool(args.get("overwrite", False)))
                elif fn.get("name") == "workspace_read":
                    result = await workspace_read(str(args.get("path", "")), Path(job["workspace"]), int(args.get("max_chars", 8000)))
                elif fn.get("name") == "workspace_deliver":
                    result = self._deliver(job, str(args.get("path", "")), args.get("title"))
                elif fn.get("name") == "workspace_list":
                    result = await workspace_list(str(args.get("path", ".")), Path(job["workspace"]), int(args.get("depth", 1)))
                else:
                    result = {"ok": False, "error": f"unknown tool: {fn.get('name')}"}
                seq = int(job["steps"]) + 1
                ok = bool(result.get("ok")) and not result.get("error")
                kind = "observation" if ok else "action"
                self.store.step(job_id, seq, kind, result, "ok" if ok else "error", "workspace.shell")
                # The provider pairs tool results to calls by id and expects text
                # content; a dict here produced "tool calls and tool results do
                # not match" on the second round.
                call_id = str(call.get("id") or f"call-{seq}")
                call.setdefault("id", call_id)
                messages.extend([
                    {
                        "role": "assistant",
                        "content": (reply or {}).get("content") or "",
                        "tool_calls": [call],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": "workspace_shell",
                        "content": _serialize_content(result),
                    },
                ])
                self.store.save_trajectory(job_id, messages)
                await self._emit(job, seq, kind, result, "ok" if ok else "error")
                if not ok:
                    key = str(result.get("error") or "unknown failure")
                    failures[key] = failures.get(key, 0) + 1
                if not ok and failures.get(str(result.get("error") or "unknown failure"), 0) >= 3:
                    self.store.set_status(job_id, "failed", error=str(result.get("error") or "repeated command failure"))
                    return self.store.get(job_id)  # type: ignore[return-value]
                job = self.store.get(job_id) or job
                if job["status"] == "cancelled":
                    self.store.audit(job_id, "job_cancelled", {"job_id": job_id})
                    return job
        if (self.store.get(job_id) or {}).get("status") == "running":
            self.store.set_status(job_id, "failed", error="maximum steps reached")
        return self.store.get(job_id)  # type: ignore[return-value]
