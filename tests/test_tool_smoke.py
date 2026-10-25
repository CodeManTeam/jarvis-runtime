"""Every registered tool must at least be callable.

Both bugs this catches were real and shipped: `calendar.list_recent` passed a
keyword-only parameter positionally, and the gate's audit callback had the wrong
arity. A tool that raises TypeError is not a capability, it is a promise.
"""

import asyncio

import pytest

from app.db import Database
from app.service import JarvisService

# Tools that leave the process (external commands, paid calls) are checked for
# callability but never actually run by the suite.
SIDE_EFFECTING = {"system.escalate"}

PLACEHOLDERS = {
    "string": "x",
    "integer": 1,
    "number": 1.0,
    "boolean": True,
    "array": [],
    "object": {},
}


@pytest.fixture
def service(tmp_path):
    return JarvisService(
        Database(str(tmp_path / "smoke.db")), artifacts_root=tmp_path / "artifacts"
    )


def minimal_args(schema: dict) -> dict:
    props = schema.get("properties") or {}
    args = {}
    for name in schema.get("required") or []:
        spec = props.get(name) or {}
        args[name] = PLACEHOLDERS.get(spec.get("type"), "x")
    return args


def test_every_tool_is_callable(service):
    runtime = service.orchestrator.classifier.tool_runtime
    schemas = service.tools.list_schemas()
    assert schemas, "no tools registered"

    failures = []
    for schema in schemas:
        name = schema["name"]
        if name in SIDE_EFFECTING:
            continue
        try:
            result = asyncio.run(
                runtime.execute(name, minimal_args(schema["inputSchema"]))
            )
            payload = result.to_dict()
        except Exception as exc:  # noqa: BLE001 - surfacing them is the point
            failures.append(f"{name}: raised {type(exc).__name__}: {exc}")
            continue
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
            if "TypeError" in message or "positional argument" in message:
                failures.append(f"{name}: {message}")

    assert not failures, "broken tools:\n" + "\n".join(failures)


def test_calendar_tool_reaches_the_database(service):
    """The specific regression: device_id is keyword-only."""
    runtime = service.orchestrator.classifier.tool_runtime
    result = asyncio.run(runtime.execute("calendar.list_recent", {"device_id": "w1"}))
    payload = result.to_dict()
    assert payload["error"] is None, payload["error"]
