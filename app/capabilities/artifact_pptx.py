"""PPTX capability: written once, discovered by the runtime, nothing wired by hand."""

from __future__ import annotations

from typing import Any

from ..artifacts import ArtifactError
from ..domain import PermissionLevel
from .base import CapabilityPack

SLIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 120},
        "subtitle": {"type": "string", "maxLength": 200},
        "bullets": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 200},
        },
    },
    "required": ["title"],
    "additionalProperties": False,
}


def _create(args: dict[str, Any], service: Any) -> dict[str, Any]:
    try:
        return service.artifacts.create_pptx(
            str(args.get("title") or ""),
            args.get("slides") or [],
            event_id=args.get("event_id"),
        )
    except ArtifactError as exc:
        # Failure keeps its own words; the agent must not turn this into "done".
        return {"ok": False, "error": exc.detail, "kind": exc.kind}


PACK = CapabilityPack(
    name="artifact.create_pptx",
    description=(
        "把标题与要点生成一份真正的 .pptx 文件，返回可直接打开的链接。"
        "生成后把链接给用户，并主动问交付方式（链接够不够、要不要改页数或格式）。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "slides": {"type": "array", "maxItems": 30, "items": SLIDE_SCHEMA},
            "event_id": {"type": "string"},
        },
        "required": ["title", "slides"],
        "additionalProperties": False,
    },
    handler=_create,
    capability="artifact.create",
    level=PermissionLevel.AUTO.value,
    levels=("auto", "once", "deny"),
    timeout_seconds=60.0,
    produces="pptx",
    delivery=[
        "把链接直接给用户，并确认能不能打开",
        "问要不要改页数、补一页摘要或换顺序",
        "问是否需要别的格式（PDF / 网页版）",
    ],
    tags=["artifact", "office"],
)
