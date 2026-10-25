"""Capability packs live in this package; see base.py for the contract."""

from .base import (  # noqa: F401
    REGISTRY,
    CapabilityPack,
    CapabilityRegistry,
    capability_descriptions,
    capability_tools,
    delivery_hints,
)

# Register at import time: lazy discovery made the tool→capability map
# depend on whether some service happened to be constructed first.
REGISTRY.discover()

__all__ = [
    "REGISTRY",
    "CapabilityPack",
    "CapabilityRegistry",
    "capability_descriptions",
    "capability_tools",
    "delivery_hints",
]
