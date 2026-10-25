"""Static web page capability.

This is the honest half of "can you build me a web thing": Jarvis can produce a
real page and serve it from its own origin. It cannot deploy an application with
a backend, and the pack says so in its delivery hints rather than letting the
model promise it.
"""

from __future__ import annotations

from typing import Any

from ..artifacts import ArtifactError
from ..domain import PermissionLevel
from .base import CapabilityPack


def _create(args: dict[str, Any], service: Any) -> dict[str, Any]:
    try:
        return service.artifacts.create_html(
            str(args.get("title") or ""),
            str(args.get("html") or ""),
            event_id=args.get("event_id"),
        )
    except ArtifactError as exc:
        return {"ok": False, "error": exc.detail, "kind": exc.kind}


PACK = CapabilityPack(
    name="artifact.create_web_page",
    description=(
        "生成一个真正的静态网页并托管在 Jarvis 自己的域名下，返回可访问链接。"
        "只能做静态页面（HTML/CSS/JS）；没有后端、数据库、登录和表单提交。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "html": {"type": "string", "minLength": 1, "maxLength": 200000},
            "event_id": {"type": "string"},
        },
        "required": ["title", "html"],
        "additionalProperties": False,
    },
    handler=_create,
    capability="artifact.create",
    level=PermissionLevel.AUTO.value,
    levels=("auto", "once", "deny"),
    timeout_seconds=30.0,
    produces="html",
    delivery=[
        "把链接给用户，并说明这是静态页面",
        "问要不要加导航、样式或多页",
        "需要后端/登录/表单提交时，明确说这部分还没做，并给出替代方案",
    ],
    tags=["artifact", "web"],
)
