"""Fetch a URL capability: the missing read side of the network."""

from __future__ import annotations

from typing import Any

from ..domain import PermissionLevel
from ..webread import FetchRefused, extract
from .base import CapabilityPack


def _fetch(args: dict[str, Any], _service: Any) -> dict[str, Any]:
    try:
        return extract(str(args.get("url") or ""))
    except FetchRefused as exc:
        # A refused fetch is a result the user must see, not a silent empty page.
        return {"ok": False, "error": exc.detail, "kind": "refused"}


PACK = CapabilityPack(
    name="web.fetch_url",
    description=(
        "读取一个 http/https 链接的正文并返回纯文本。只能读文本类内容（HTML / JSON / "
        "纯文本），二进制（PDF、图片）读不了。内网地址会被拒绝。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 4, "maxLength": 2048},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    handler=_fetch,
    capability="web.fetch",
    level=PermissionLevel.AUTO.value,
    levels=("auto", "once", "deny"),
    timeout_seconds=20.0,
    produces=None,
    tags=["web", "read"],
)
