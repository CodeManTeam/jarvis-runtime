"""PDF capability, built on reportlab with a CJK font.

The user proved this works by hand before asking for it, so the pack only wraps
what reportlab already does: a title page plus headed sections.
"""

from __future__ import annotations

from typing import Any

from ..artifacts import ArtifactError
from ..domain import PermissionLevel
from .base import CapabilityPack

SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "heading": {"type": "string", "maxLength": 120},
        "paragraphs": {
            "type": "array",
            "maxItems": 40,
            "items": {"type": "string", "maxLength": 2000},
        },
        "bullets": {
            "type": "array",
            "maxItems": 40,
            "items": {"type": "string", "maxLength": 500},
        },
    },
    "required": ["heading"],
    "additionalProperties": False,
}


def _create(args: dict[str, Any], service: Any) -> dict[str, Any]:
    try:
        return service.artifacts.create_pdf(
            str(args.get("title") or ""),
            args.get("sections") or [],
            event_id=args.get("event_id"),
        )
    except ArtifactError as exc:
        return {"ok": False, "error": exc.detail, "kind": exc.kind}


PACK = CapabilityPack(
    name="artifact.create_pdf",
    description=(
        "把标题与小节生成一份真正的 PDF（支持中文），返回可打开的链接。"
        "适合周报、纪要、一页说明的长文版本。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "sections": {"type": "array", "maxItems": 40, "items": SECTION_SCHEMA},
            "event_id": {"type": "string"},
        },
        "required": ["title", "sections"],
        "additionalProperties": False,
    },
    handler=_create,
    capability="artifact.create",
    level=PermissionLevel.AUTO.value,
    levels=("auto", "once", "deny"),
    timeout_seconds=60.0,
    produces="pdf",
    delivery=[
        "把链接给用户，并确认能不能打开",
        "问要不要改成周报/日报的固定版式",
        "需要打印时提示这是 A4 版式",
    ],
    tags=["artifact", "pdf"],
)
