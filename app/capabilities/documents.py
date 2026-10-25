"""Document handling capabilities: the inbound half of the material gap."""

from __future__ import annotations

from typing import Any

from ..domain import PermissionLevel
from ..uploads import UploadRefused
from .base import CapabilityPack


def _list(args: dict[str, Any], service: Any) -> dict[str, Any]:
    return {"uploads": service.uploads.list(int(args.get("limit") or 5))}


def _read(args: dict[str, Any], service: Any) -> dict[str, Any]:
    try:
        return service.uploads.read_text(str(args.get("id") or ""))
    except UploadRefused as exc:
        return {"ok": False, "error": exc.detail, "kind": "refused"}


PACKS = [
    CapabilityPack(
        name="document.list_uploads",
        description="列出用户传上来的文件（文件名、大小、可读字符数）。",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            "additionalProperties": False,
        },
        handler=_list,
        capability="document.read",
        level=PermissionLevel.AUTO.value,
        levels=("auto", "once", "deny"),
        timeout_seconds=10.0,
        tags=["document", "read"],
    ),
    CapabilityPack(
        name="document.read_upload",
        description=(
            "读一个用户传来的文件的正文（PDF/Word/Markdown/文本）。"
            "先用 document.list_uploads 拿到编号。读不了时会明确说明原因。"
        ),
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 64}},
            "required": ["id"],
            "additionalProperties": False,
        },
        handler=_read,
        capability="document.read",
        level=PermissionLevel.AUTO.value,
        levels=("auto", "once", "deny"),
        timeout_seconds=20.0,
        tags=["document", "read"],
    ),
]
