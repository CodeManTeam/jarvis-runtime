"""Escalation: let the agent write and run a small script when no tool exists.

This is the answer to "the assistant should simply do it, or say plainly why it
cannot". It runs the Codex CLI non-interactively inside a sandbox and reports
what happened — including failures, in the failure's own words.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_OUTPUT_CHARS = 4000


class EscalateError(RuntimeError):
    """A failed escalation. `kind` is stable; `detail` is for humans."""

    kind = "failed"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class EscalateUnavailable(EscalateError):
    kind = "unavailable"


class EscalateTimedOut(EscalateError):
    kind = "timeout"


class EscalateRefused(EscalateError):
    kind = "refused"


class EscalateNeedsPermission(EscalateError):
    kind = "needs_permission"


@dataclass
class EscalationResult:
    ok: bool
    output: str
    exit_code: int
    duration_ms: int
    sandbox: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "sandbox": self.sandbox,
        }


def codex_available() -> bool:
    return shutil.which(os.getenv("JARVIS_CODEX_BIN", "codex")) is not None


def _resolve_binary(name: str) -> list[str]:
    """Resolve the launcher the way a shell would.

    On Windows the CLI is a .cmd shim, and CreateProcess does not apply PATHEXT,
    so a bare "codex" fails with FileNotFoundError even though it is on PATH.
    Launching it through cmd is what both a shell and a user would do.
    """
    resolved = shutil.which(name)
    if resolved is None:
        return [name]
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", resolved]
    return [resolved]


def build_command(task: str, *, write: bool) -> list[str]:
    """Codex argv for one non-interactive escalation.

    `approval_policy=never` is safe precisely because the sandbox decides what is
    possible: a read-only sandbox cannot write no matter what the model asks for.
    """
    binary = os.getenv("JARVIS_CODEX_BIN", "codex")
    return _resolve_binary(binary) + [
        "exec",
        "--skip-git-repo-check",
        "-c", "approval_policy=\"never\"",
        "-c", f"sandbox_mode=\"{'workspace-write' if write else 'read-only'}\"",
        "-c", "model_reasoning_effort=\"medium\"",
        "-",
    ]


def _classify(stderr: str, code: int) -> EscalateError:
    text = (stderr or "").lower()
    if "insufficient account balance" in text or "402" in text:
        return EscalateUnavailable("上游模型额度不足，这次没有完成。")
    if "401" in text or "403" in text or "unauthorized" in text:
        return EscalateUnavailable("外部执行通道鉴权失败，这次没有完成。")
    if "not found" in text or "no such file" in text:
        return EscalateUnavailable("找不到 codex 可执行文件，无法执行。")
    return EscalateError(f"外部执行失败（退出码 {code}）。")


async def run(
    task: str,
    *,
    write: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    command: list[str] | None = None,
) -> EscalationResult:
    """Run one escalation. Raises EscalateError subclasses; never fabricates success."""
    if not task.strip():
        raise EscalateRefused("没有可执行的任务描述。")
    argv = command or build_command(task, write=write)
    workdir = tempfile.mkdtemp(prefix="jarvis-escalate-")
    started = asyncio.get_event_loop().time()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
        )
    except FileNotFoundError as exc:
        raise EscalateUnavailable("找不到 codex 可执行文件，无法执行。") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(task.encode("utf-8")), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise EscalateTimedOut(
            f"外部执行超过 {int(timeout)} 秒，已终止，这次没有完成。"
        ) from exc

    duration_ms = int((asyncio.get_event_loop().time() - started) * 1000)
    out = (stdout or b"").decode("utf-8", "replace").strip()
    err = (stderr or b"").decode("utf-8", "replace").strip()
    if process.returncode != 0:
        raise _classify(err or out, process.returncode)
    if not out:
        raise EscalateError("外部执行没有返回内容，这次没有完成。")
    return EscalationResult(
        ok=True,
        output=out[:MAX_OUTPUT_CHARS],
        exit_code=process.returncode or 0,
        duration_ms=duration_ms,
        sandbox="workspace-write" if write else "read-only",
    )
