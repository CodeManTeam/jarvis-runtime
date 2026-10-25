from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping


ToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class ToolRisk(str, Enum):
    """Coarse risk levels used by the runtime's permission boundary."""

    OBSERVE = "observe"
    PROPOSE = "propose"
    EXECUTE = "execute"


@dataclass(frozen=True)
class ToolDefinition:
    """A tool definition exposed to an LLM or an MCP-compatible client."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    permissions: frozenset[str] = field(default_factory=frozenset)
    risk: ToolRisk | str = ToolRisk.OBSERVE
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("tool name must not be empty")
        if not callable(self.handler):
            raise TypeError("tool handler must be callable")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not isinstance(self.input_schema, dict):
            raise TypeError("input_schema must be a JSON Schema object")
        object.__setattr__(self, "permissions", frozenset(self.permissions))
        object.__setattr__(self, "risk", ToolRisk(self.risk))

    def schema(self) -> dict[str, Any]:
        """Return the MCP/LLM-facing schema without the Python handler."""

        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "permissions": sorted(self.permissions),
            "risk": self.risk.value,
        }


@dataclass(frozen=True)
class ToolResult:
    """Stable result envelope for both successful and failed tool calls."""

    success: bool
    tool_name: str
    output: Any = None
    error: dict[str, Any] | None = None

    @classmethod
    def ok(cls, tool_name: str, output: Any = None) -> "ToolResult":
        return cls(success=True, tool_name=tool_name, output=output)

    @classmethod
    def fail(
        cls,
        tool_name: str,
        code: str,
        message: str,
        *,
        details: Any = None,
    ) -> "ToolResult":
        error: dict[str, Any] = {"code": code, "message": message}
        if details is not None:
            error["details"] = details
        return cls(success=False, tool_name=tool_name, error=error)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "success": self.success,
            "tool_name": self.tool_name,
            "output": self.output,
            "error": self.error,
        }
        return result


def is_awaitable(value: Any) -> bool:
    return inspect.isawaitable(value)
