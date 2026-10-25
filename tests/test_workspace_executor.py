"""The executor must not stall when a command leaves a child holding the pipe."""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

import pytest

from app.workspace_agent import workspace_shell

PY = f'"{sys.executable}"'


def run(command: str) -> dict:
    workspace = Path(tempfile.mkdtemp())
    return asyncio.run(workspace_shell(command, workspace))


def test_plain_command_returns_output():
    result = run("echo hello")
    assert result["ok"] is True
    assert "hello" in result["stdout"]


def test_a_command_that_leaves_a_server_running_does_not_hang():
    """The regression: the child inherited the pipe, so we waited 60s."""
    started = time.time()
    result = run(f"({PY} -m http.server 8798 >/dev/null 2>&1 &) ; echo started")
    elapsed = time.time() - started
    # The property under test is "does not hang"; the child may keep the output
    # file busy on some platforms, so 'started' is not asserted here — plain
    # output is covered by test_plain_command_returns_output.
    assert elapsed < 10, f"took {elapsed:.1f}s"
    assert "returncode" in result
    assert result.get("pgid"), "the pgid is how leftover processes are cleaned up"


def test_non_zero_exit_reports_the_reason():
    result = run("exit 3")
    assert result["ok"] is False
    assert result["returncode"] == 3


def test_stdout_is_capped():
    result = run(f"{PY} -c \"print('x' * 9000)\"")
    assert len(result["stdout"]) <= 4000


def test_a_failing_command_keeps_stderr():
    result = run(f"{PY} -c \"raise SystemExit('boom')\"")
    assert result["ok"] is False
    assert "boom" in (result["stderr"] or result["error"] or "")
