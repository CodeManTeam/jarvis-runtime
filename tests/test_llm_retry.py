"""A two-second upstream hiccup must not become "this query did not complete"."""

import asyncio

import httpx

from app import llm
from app.llm import LLMError, is_transient
from app.routing import Endpoint


def test_upstream_errors_are_transient():
    assert is_transient(LLMError("LLM Responses stream failed: {'code': 'upstream_error'}"))
    assert is_transient(httpx.ReadTimeout("timed out"))
    assert is_transient(httpx.ConnectError("connection reset"))


def test_quota_and_auth_are_not_retried():
    # Retrying these only delays the honest answer.
    assert not is_transient(LLMError("402 Insufficient account balance"))
    assert not is_transient(LLMError("401 unauthorized"))
    assert not is_transient(LLMError("invalid api key"))


class _Router:
    def __init__(self):
        self.primary = Endpoint(
            "primary", "m1", "https://example.invalid", 5.0, ("tool_call",), 512
        )
        self.fallback = None
        self.failures = []
        self.successes = []

    def choose(self, capability="tool_call"):
        return self.primary

    def record_failure(self, endpoint, reason):
        self.failures.append((endpoint.id, reason))

    def record_success(self, endpoint):
        self.successes.append(endpoint.id)


def run_once(monkeypatch, outcomes):
    """Drive complete_with_tools with a scripted sequence; never re-raises."""
    calls = []

    async def fake_once(messages, tools, *, max_tokens=0, model="", timeout=0):
        calls.append(model)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(llm, "_complete_with_tools_once", fake_once)
    router = _Router()
    previous = llm.set_router(router)
    result = None
    error = None
    try:
        result = asyncio.run(llm.complete_with_tools([], []))
    except Exception as exc:  # noqa: BLE001 - the caller asserts on it
        error = exc
    finally:
        llm.set_router(previous)
    return result, error, calls, router


def test_a_transient_failure_is_retried_and_succeeds(monkeypatch):
    result, error, calls, router = run_once(
        monkeypatch, [LLMError("upstream_error"), {"content": "ok"}]
    )
    assert error is None
    assert result == {"content": "ok"}
    assert len(calls) == 2, "one retry on the same endpoint"
    assert router.successes == ["primary"]
    assert router.failures == [], "a retried success is not a degradation"


def test_a_fatal_failure_is_not_retried(monkeypatch):
    _, error, calls, router = run_once(
        monkeypatch, [LLMError("402 Insufficient account balance")]
    )
    assert isinstance(error, LLMError)
    assert len(calls) == 1, "quota errors are surfaced immediately"
    assert router.failures, "the failure is recorded for the route table"


def test_two_transient_failures_give_up_honestly(monkeypatch):
    _, error, calls, _router = run_once(
        monkeypatch, [LLMError("upstream_error"), LLMError("upstream_error")]
    )
    assert isinstance(error, LLMError)
    assert len(calls) == 2, "exactly one retry, then the truth"
