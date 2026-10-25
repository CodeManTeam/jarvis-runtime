"""Running code the model wrote: useful, bounded, and never silently approved."""

import asyncio

import pytest

from app.codeexec import CodeFailed, CodeRefused, CodeTimedOut, run_python
from app.db import Database
from app.policy import CAPABILITY_MATRIX
from app.service import JarvisService


def run(code: str, **kwargs):
    return asyncio.run(run_python(code, **kwargs))


@pytest.fixture
def service(tmp_path):
    return JarvisService(
        Database(str(tmp_path / "code.db")), artifacts_root=tmp_path / "artifacts"
    )


def test_it_runs_and_returns_stdout():
    result = run("print(sum(range(10)))")
    assert "45" in result.stdout
    assert result.exit_code == 0


def test_it_cannot_see_the_service_environment():
    result = run("import os; print(sorted(os.environ))")
    for leaked in ("DEEPSEEK_API_KEY", "DEVICE_TOKEN", "HTTPS_PROXY"):
        assert leaked not in result.stdout


def test_it_runs_inside_its_own_directory():
    result = run("import os, pathlib; pathlib.Path('x.txt').write_text('hi'); print(sorted(os.listdir('.')))")
    assert "x.txt" in result.stdout


def test_a_non_zero_exit_is_a_failure_with_the_reason():
    with pytest.raises(CodeFailed) as caught:
        run("raise SystemExit('nope happened')")
    assert "退出码" in caught.value.detail


def test_a_python_error_surfaces_its_message():
    with pytest.raises(CodeFailed) as caught:
        run("1/0")
    assert "ZeroDivisionError" in caught.value.detail or "division" in caught.value.detail


def test_a_runaway_program_is_killed():
    with pytest.raises(CodeTimedOut) as caught:
        run("import time; time.sleep(10)", timeout=1)
    assert "超过" in caught.value.detail


def test_empty_code_is_refused():
    with pytest.raises(CodeRefused):
        run("   ")


def test_output_is_capped():
    result = run("print('x' * 9000)")
    assert len(result.stdout) <= 4000
    assert result.truncated is True


def test_it_is_per_action_by_default(service):
    """Arbitrary execution must never be reachable without an explicit approval."""
    assert CAPABILITY_MATRIX["code.run"]["default"] == "per_action"
    names = {schema["name"] for schema in service.tools.list_schemas()}
    assert "code.run_python" in names


def test_the_gate_blocks_it_without_a_grant(service):
    import app.policy as policy

    assert policy.capability_for_tool("code.run_python") == "code.run"
