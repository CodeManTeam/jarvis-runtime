from __future__ import annotations

import asyncio
import unittest

from .definitions import ToolDefinition, ToolRisk
from .registry import ToolRegistry
from .runtime import ToolRuntime


def make_echo_tool() -> ToolDefinition:
    return ToolDefinition(
        name="echo",
        description="Return the supplied text.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
            "additionalProperties": False,
        },
        permissions=frozenset({"messages.read"}),
        risk=ToolRisk.OBSERVE,
        handler=lambda args: {"text": args["text"]},
    )


class ToolRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_lists_mcp_style_schema_and_executes(self) -> None:
        registry = ToolRegistry([make_echo_tool()])
        runtime = ToolRuntime(registry)

        self.assertEqual(
            registry.list_schemas(),
            [
                {
                    "name": "echo",
                    "description": "Return the supplied text.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string", "minLength": 1}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    "permissions": ["messages.read"],
                    "risk": "observe",
                }
            ],
        )
        result = await runtime.execute(
            "echo",
            {"text": "hello"},
            permissions={"messages.read"},
        )
        self.assertEqual(
            result.to_dict(),
            {
                "success": True,
                "tool_name": "echo",
                "output": {"text": "hello"},
                "error": None,
            },
        )

    async def test_runtime_returns_structured_validation_and_permission_errors(
        self,
    ) -> None:
        runtime = ToolRuntime(ToolRegistry([make_echo_tool()]))

        invalid = await runtime.execute("echo", {}, permissions={"messages.read"})
        denied = await runtime.execute("echo", {"text": "hello"})

        self.assertEqual(invalid.error["code"], "invalid_arguments")
        self.assertEqual(invalid.error["details"]["path"], "$")
        self.assertEqual(denied.error["code"], "permission_denied")
        self.assertEqual(denied.error["details"]["missing_permissions"], ["messages.read"])

    async def test_runtime_applies_timeout_to_async_handlers(self) -> None:
        async def slow_handler(_: dict[str, object]) -> str:
            await asyncio.sleep(0.05)
            return "done"

        registry = ToolRegistry(
            [
                ToolDefinition(
                    name="slow",
                    description="Slow tool",
                    input_schema={"type": "object"},
                    handler=slow_handler,
                    timeout_seconds=0.01,
                )
            ]
        )

        result = await ToolRuntime(registry).execute("slow")

        self.assertFalse(result.success)
        self.assertEqual(result.error["code"], "timeout")
