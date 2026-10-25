from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable, Mapping
from typing import Any

from .definitions import ToolResult
from .errors import ToolNotFoundError
from .registry import ToolRegistry
from .validation import SchemaValidationError, validate_input


class ToolRuntime:
    """Validate, authorize, and execute registered tools."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        default_timeout_seconds: float = 10.0,
    ) -> None:
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be greater than zero")
        self.registry = registry
        self.default_timeout_seconds = default_timeout_seconds

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        permissions: Iterable[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        try:
            tool = self.registry.get(name)
        except ToolNotFoundError as exc:
            return ToolResult.fail(name, "tool_not_found", str(exc))

        try:
            payload = dict(arguments or {})
        except (TypeError, ValueError) as exc:
            return ToolResult.fail(
                name,
                "invalid_arguments",
                "tool arguments must be an object",
                details={"exception": exc.__class__.__name__},
            )
        try:
            validate_input(payload, tool.input_schema)
        except SchemaValidationError as exc:
            return ToolResult.fail(
                name,
                "invalid_arguments",
                str(exc),
                details={"path": exc.path},
            )

        granted = set(permissions or ())
        missing = sorted(tool.permissions - granted)
        if missing:
            return ToolResult.fail(
                name,
                "permission_denied",
                "missing required tool permissions",
                details={"missing_permissions": missing},
            )

        timeout = (
            tool.timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if timeout <= 0:
            return ToolResult.fail(name, "invalid_timeout", "timeout must be greater than zero")

        try:
            result = await asyncio.wait_for(
                self._invoke(tool.handler, payload),
                timeout=timeout,
            )
            return ToolResult.ok(name, result)
        except asyncio.TimeoutError:
            return ToolResult.fail(
                name,
                "timeout",
                f"tool execution exceeded {timeout:g} seconds",
            )
        except Exception as exc:
            return ToolResult.fail(
                name,
                "execution_error",
                str(exc) or exc.__class__.__name__,
                details={"exception": exc.__class__.__name__},
            )

    @staticmethod
    async def _invoke(handler: Any, payload: dict[str, Any]) -> Any:
        if inspect.iscoroutinefunction(handler):
            return await handler(payload)
        result = await asyncio.to_thread(handler, payload)
        if inspect.isawaitable(result):
            return await result
        return result
