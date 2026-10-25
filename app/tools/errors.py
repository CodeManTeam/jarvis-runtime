from __future__ import annotations


class ToolError(Exception):
    """Base error for registry and runtime failures."""


class ToolRegistrationError(ToolError):
    """Raised when a tool cannot be registered."""


class ToolNotFoundError(ToolError):
    """Raised when a requested tool is not registered."""
