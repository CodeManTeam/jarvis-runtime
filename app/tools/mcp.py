from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any, Protocol, runtime_checkable

from .definitions import ToolDefinition, ToolResult
from .registry import ToolRegistry
from .runtime import ToolRuntime


@runtime_checkable
class ExternalMCPClient(Protocol):
    """Minimal async seam for a future MCP transport implementation.

    An adapter can implement this protocol for stdio, Streamable HTTP, or
    another transport without making the core runtime depend on an MCP SDK.
    """

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return MCP tool schemas."""

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        """Call a remote MCP tool and return its result."""


class MCPToolSource:
    """Adapter that exposes an external MCP client through the local runtime."""

    def __init__(self, client: ExternalMCPClient) -> None:
        self.client = client

    async def refresh(self, registry: ToolRegistry) -> list[str]:
        """Register remote schemas as local proxy tools.

        Remote tools are represented locally with a small async handler. The
        client remains responsible for transport, serialization, and remote
        error semantics.
        """

        registered: list[str] = []
        for schema in await self.client.list_tools():
            name = str(schema["name"])
            description = str(schema.get("description", ""))
            input_schema = schema.get("inputSchema", {"type": "object"})
            permissions = frozenset(schema.get("permissions", ()))
            risk = schema.get("risk", "observe")

            async def handler(
                arguments: dict[str, Any],
                *,
                tool_name: str = name,
            ) -> Any:
                return await self.client.call_tool(tool_name, arguments)

            registry.register(
                ToolDefinition(
                    name=name,
                    description=description,
                    input_schema=input_schema,
                    permissions=permissions,
                    risk=risk,
                    handler=handler,
                ),
                replace=True,
            )
            registered.append(name)
        return registered

    async def call(
        self,
        runtime: ToolRuntime,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        permissions: set[str] | None = None,
    ) -> ToolResult:
        return await runtime.execute(
            name,
            arguments,
            permissions=permissions,
        )
