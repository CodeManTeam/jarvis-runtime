"""Capability packs: the extension point for "Jarvis can do one more thing".

Adding a capability means adding a module in this package. Nothing in the
service, the prompt or the permission matrix is edited by hand:

* the tool schema is served from the pack,
* the permission entry is registered into the matrix at import time,
* the model's capability list is generated from whatever is registered.

A pack declares what it needs and what it produces, so the agent can talk about
delivery ("here is the link, want it elsewhere?") without hardcoded knowledge.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable

from ..domain import PermissionLevel
from ..policy import register_capability, register_tool_capability
from ..tools import ToolDefinition

Handler = Callable[[dict[str, Any], Any], Any]

# base.py is a module inside the package, so its own name ends with ".base".
PACKAGE = __name__.rsplit(".", 1)[0]


@dataclass
class CapabilityPack:
    """One declared ability.

    name          tool name the model sees, e.g. "artifact.create_pptx"
    capability    permission key, e.g. "artifact.create"
    level         default permission level for that capability
    produces      what the user gets back ("pptx", "html", None)
    delivery      suggested next steps to offer after producing something
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    capability: str | None = None
    level: str = PermissionLevel.AUTO.value
    levels: tuple[str, ...] = ("auto", "once", "deny")
    scope: str = "none"
    timeout_seconds: float = 30.0
    produces: str | None = None
    delivery: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def tool(self, service: Any) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            handler=lambda args: self.handler(args, service),
            timeout_seconds=self.timeout_seconds,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "capability": self.capability,
            "level": self.level,
            "produces": self.produces,
            "delivery": list(self.delivery),
            "tags": list(self.tags),
        }


class CapabilityRegistry:
    def __init__(self) -> None:
        self._packs: dict[str, CapabilityPack] = {}
        self.loaded = False

    def register(self, pack: CapabilityPack) -> None:
        if pack.name in self._packs:
            raise ValueError(f"duplicate capability: {pack.name}")
        self._packs[pack.name] = pack
        if pack.capability:
            register_capability(
                pack.capability,
                default=pack.level,
                levels=pack.levels,
                scope=pack.scope,
            )
            # Without this the pack's tool would never be gated: the static
            # TOOL_CAPABILITY map only knows hand-written tools.
            register_tool_capability(pack.name, pack.capability)
        self.loaded = True

    def packs(self) -> list[CapabilityPack]:
        self.discover()
        return list(self._packs.values())

    def tools(self, service: Any) -> list[ToolDefinition]:
        return [pack.tool(service) for pack in self.packs()]

    def describe(self) -> list[dict[str, Any]]:
        return [pack.describe() for pack in self.packs()]

    def discover(self) -> None:
        """Import every module in this package that exposes PACK or PACKS."""
        if self.loaded:
            return
        self.loaded = True
        # base.py lives inside the package, so its own directory is the
        # package path; __path__ is not defined for a plain module.
        for info in pkgutil.iter_modules([os.path.dirname(__file__)]):
            if info.name in {"base"} or info.name.startswith("_"):
                continue
            module = importlib.import_module(f"{PACKAGE}.{info.name}")
            single = getattr(module, "PACK", None)
            many = getattr(module, "PACKS", None)
            for pack in ([single] if single else []) + list(many or []):
                if isinstance(pack, CapabilityPack):
                    self.register(pack)


REGISTRY = CapabilityRegistry()


def capability_tools(service: Any) -> list[ToolDefinition]:
    return REGISTRY.tools(service)


def capability_descriptions() -> list[dict[str, Any]]:
    return REGISTRY.describe()


def delivery_hints() -> dict[str, list[str]]:
    """What to offer the user after producing each kind of artifact."""
    hints: dict[str, list[str]] = {}
    for pack in REGISTRY.packs():
        if pack.produces:
            hints.setdefault(pack.produces, []).extend(pack.delivery)
    return hints
