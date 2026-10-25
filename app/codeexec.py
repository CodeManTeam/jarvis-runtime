"""Run a short program the agent wrote.

This is deliberately *not* sold as a sandbox. It runs as the service user with a
stripped environment, its own temporary directory and a hard timeout, which stops
accidents, not attackers. Because Python can spawn subprocesses, this is
equivalent to shell access, so the capability defaults to per-action approval:
the user sees what will run before it runs.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 20.0
MAX_OUTPUT_CHARS = 4000
MAX_CODE_CHARS = 20000


class CodeRefused(RuntimeError):
    kind = "refused"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class CodeFailed(RuntimeError):
    kind = "failed"

    def __init__(self, detail: str, stderr: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.stderr = stderr


class CodeTimedOut(RuntimeError):
    kind = "timeout"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass
class CodeResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    truncated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
        }


def _clean_env(home: str) -> dict[str, str]:
    """Keep what Python needs to start; drop tokens, proxies and model keys."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "TMPDIR": home,
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }
    return env


async def run_python(
    code: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    workspace: Path | None = None,
) -> CodeResult:
    if not code or not code.strip():
        raise CodeRefused("没有可运行的代码。")
    if len(code) > MAX_CODE_CHARS:
        raise CodeRefused(f"代码太长（上限 {MAX_CODE_CHARS} 字符）。")

    directory = Path(workspace) if workspace else Path(tempfile.mkdtemp(prefix="jarvis-code-"))
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "run.py"
    script.write_text(code, encoding="utf-8")

    started = asyncio.get_event_loop().time()
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",  # isolated: ignore PYTHON* vars and the user site directory
            "-B",
            str(script),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(directory),
            env=_clean_env(str(directory)),
        )
    except OSError as exc:
        raise CodeFailed(f"无法启动解释器：{type(exc).__name__}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise CodeTimedOut(f"运行超过 {int(timeout)} 秒，已终止。") from exc

    duration_ms = int((asyncio.get_event_loop().time() - started) * 1000)
    out = (stdout or b"").decode("utf-8", "replace")
    err = (stderr or b"").decode("utf-8", "replace")
    truncated = len(out) > MAX_OUTPUT_CHARS or len(err) > MAX_OUTPUT_CHARS
    out = out[:MAX_OUTPUT_CHARS]
    err = err[:MAX_OUTPUT_CHARS]

    if process.returncode != 0:
        first_line = next((line for line in reversed(err.strip().splitlines()) if line.strip()), "")
        raise CodeFailed(
            f"代码运行失败（退出码 {process.returncode}）：{first_line[:300]}", stderr=err
        )

    return CodeResult(
        stdout=out,
        stderr=err,
        exit_code=process.returncode or 0,
        duration_ms=duration_ms,
        truncated=truncated,
    )
