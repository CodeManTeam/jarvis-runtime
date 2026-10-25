from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .definitions import ToolDefinition
from .errors import ToolNotFoundError, ToolRegistrationError


class ToolRegistry:
    """In-process registry for built-in and future MCP-backed tools."""

    def __init__(self, tools: Iterable[ToolDefinition] | None = None) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: ToolDefinition, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ToolRegistrationError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        if name not in self._tools:
            raise ToolNotFoundError(f"tool not found: {name}")
        del self._tools[name]

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"tool not found: {name}") from exc

    def list_schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
