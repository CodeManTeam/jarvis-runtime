"""MCP-style tools for the Jarvis runtime.

The package is intentionally dependency-free so it can be used by the API
process and by future local or remote MCP adapters.
"""

from .definitions import ToolDefinition, ToolResult, ToolRisk
from .errors import ToolError, ToolNotFoundError, ToolRegistrationError
from .mcp import ExternalMCPClient, MCPToolSource
from .registry import ToolRegistry
from .runtime import ToolRuntime

__all__ = [
    "ExternalMCPClient",
    "MCPToolSource",
    "ToolDefinition",
    "ToolError",
    "ToolNotFoundError",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolResult",
    "ToolRisk",
    "ToolRuntime",
]
