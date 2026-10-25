"""Model routing with capability-aware fallback.

The rule this module must never break (DEVICE_OPS.md §4): degradation may only
remove capability. It never widens permission, so a degraded model can perform
fewer things — never more.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any, Callable

from .config import settings

current_event: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "degradation_event", default=None
)


# Consecutive failures before a route is considered degraded, and consecutive
# probe successes required before it is trusted again for new work.
FAILURES_BEFORE_DEGRADE = 2
PROBES_BEFORE_RECOVERY = 3


@dataclass(frozen=True)
class Endpoint:
    id: str
    model: str
    base_url: str
    timeout_seconds: float
    capabilities: tuple[str, ...]
    max_tokens: int = 2048
    is_fallback: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "capabilities": list(self.capabilities),
            "max_tokens": self.max_tokens,
            "is_fallback": self.is_fallback,
        }


@dataclass
class RouteState:
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    degraded: bool = False
    last_error: str | None = None


class Router:
    """Chooses an endpoint per capability and records degradations for audit."""

    def __init__(
        self,
        primary: Endpoint,
        fallback: Endpoint | None = None,
        *,
        on_degradation: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.on_degradation = on_degradation
        self.state = {primary.id: RouteState()}
        if fallback is not None:
            self.state[fallback.id] = RouteState()
        self.active_id = primary.id
        self._overrides: dict[str, str] = {}

    def load_overrides(self, values: dict[str, str]) -> None:
        self._overrides = dict(values)
        primary_model = self._overrides.get("primary.model")
        if primary_model:
            self.primary = Endpoint(**{**self.primary.__dict__, "model": primary_model})
        fallback_model = self._overrides.get("fallback.model")
        if fallback_model is not None:
            if fallback_model:
                base = self.fallback or Endpoint("fallback", fallback_model, self.primary.base_url, self.primary.timeout_seconds, ("plan", "tool_call"), 2048, True)
                self.fallback = Endpoint(**{**base.__dict__, "model": fallback_model})
                self.state.setdefault("fallback", RouteState())
            else:
                self.fallback = None
                self.active_id = self.primary.id
        timeout = self._overrides.get("timeout_seconds")
        if timeout:
            self.primary = Endpoint(**{**self.primary.__dict__, "timeout_seconds": float(timeout)})
        base_url = self._overrides.get("provider_base_url")
        if base_url:
            self.primary = Endpoint(**{**self.primary.__dict__, "base_url": base_url})

    # ---- selection -------------------------------------------------
    def active(self) -> Endpoint:
        if self.fallback is not None and self.active_id == self.fallback.id:
            return self.fallback
        return self.primary

    def choose(self, capability: str = "plan") -> Endpoint:
        """Return the endpoint to use for ``capability``.

        A degraded primary that no longer declares this capability hands over to
        the fallback; if neither declares it, the caller gets the primary and it
        will fail loudly rather than silently pretending to work.
        """
        current = self.active()
        if capability in current.capabilities:
            return current
        if self.fallback is not None and capability in self.fallback.capabilities:
            self._switch(self.fallback, capability, reason="capability_missing")
            return self.fallback
        return current

    def _switch(self, endpoint: Endpoint, capability: str, *, reason: str) -> None:
        previous = self.active()
        if previous.id == endpoint.id:
            return
        self.active_id = endpoint.id
        gained = sorted(set(previous.capabilities) - set(endpoint.capabilities))
        self._emit({
            "stage": "model",
            "from": previous.model,
            "to": endpoint.model,
            "capability_loss": gained,
            "recovered": False,
            "reason": reason,
            "capability": capability,
        })

    # ---- health ----------------------------------------------------
    def record_failure(self, endpoint: Endpoint, error: str) -> None:
        state = self.state[endpoint.id]
        state.consecutive_failures += 1
        state.consecutive_successes = 0
        state.last_error = error[:200]
        if (
            endpoint.id == self.primary.id
            and self.fallback is not None
            and state.consecutive_failures >= FAILURES_BEFORE_DEGRADE
            and not state.degraded
        ):
            state.degraded = True
            self._switch(self.fallback, "plan", reason="primary_failing")
        elif (
            endpoint.is_fallback
            and state.consecutive_failures >= FAILURES_BEFORE_DEGRADE
            and not state.degraded
        ):
            # Both routes are unhealthy. Report it instead of pretending.
            state.degraded = True
            self._emit({
                "stage": "model",
                "from": self.primary.model,
                "to": endpoint.model,
                "capability_loss": sorted(
                    set(self.primary.capabilities) - set(endpoint.capabilities)
                ),
                "recovered": False,
                "reason": "fallback_failing",
                "capability": "plan",
            })

    def record_success(self, endpoint: Endpoint) -> None:
        state = self.state[endpoint.id]
        state.consecutive_successes += 1
        state.consecutive_failures = 0
        # The primary is only trusted for NEW work after repeated probes; the
        # current run deliberately keeps the endpoint it started with.
        if (
            endpoint.id == self.primary.id
            and state.degraded
            and state.consecutive_successes >= PROBES_BEFORE_RECOVERY
            and self.active_id != self.primary.id
        ):
            state.degraded = False
            self.active_id = self.primary.id
            self._emit({
                "stage": "model",
                "from": self.fallback.model if self.fallback else self.primary.model,
                "to": self.primary.model,
                "capability_loss": [],
                "recovered": True,
                "reason": "primary_probe_succeeded",
                "capability": "plan",
            })

    # ---- reporting -------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self.active().id,
            "degraded": any(state.degraded for state in self.state.values()),
            "routes": [
                {
                    **endpoint.describe(),
                    "state": {
                        "degraded": self.state[endpoint.id].degraded,
                        "consecutive_failures": self.state[endpoint.id].consecutive_failures,
                        "consecutive_successes": self.state[endpoint.id].consecutive_successes,
                        "last_error": self.state[endpoint.id].last_error,
                    },
                }
                for endpoint in (self.primary, self.fallback)
                if endpoint is not None
            ],
        }

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_degradation is not None:
            try:
                self.on_degradation(event)
            except Exception:  # noqa: BLE001 - audit must never break a request
                pass


def build_default_router() -> Router:
    primary = Endpoint(
        id="primary",
        model=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
        timeout_seconds=float(getattr(settings, "llm_timeout_seconds", 90)),
        capabilities=("plan", "tool_call", "vision", "complex_attachment"),
        max_tokens=4096,
    )
    fallback_model = getattr(settings, "fallback_model", "") or ""
    fallback = None
    if fallback_model:
        fallback = Endpoint(
            id="fallback",
            model=fallback_model,
            base_url=getattr(settings, "fallback_base_url", "") or settings.deepseek_base_url,
            timeout_seconds=float(getattr(settings, "fallback_timeout_seconds", 45)),
            # A lighter model keeps planning and tools but loses the heavy
            # capabilities; that loss is what the UI must report.
            capabilities=("plan", "tool_call"),
            max_tokens=2048,
            is_fallback=True,
        )
    return Router(primary, fallback)
