"""Code execution capability: the agent writes a program, the user approves it."""

from __future__ import annotations

from typing import Any

from ..codeexec import CodeFailed, CodeRefused, CodeTimedOut, run_python
from ..domain import PermissionLevel
from .base import CapabilityPack


async def _run(args: dict[str, Any], _service: Any) -> dict[str, Any]:
    try:
        result = await run_python(
            str(args.get("code") or ""),
            timeout=float(args.get("timeout_seconds") or 20),
        )
    except CodeTimedOut as exc:
        return {"ok": False, "error": exc.detail, "kind": exc.kind}
    except CodeFailed as exc:
        # Keep stderr: the model needs it to fix its own program.
        return {"ok": False, "error": exc.detail, "kind": exc.kind, "stderr": exc.stderr}
    except CodeRefused as exc:
        return {"ok": False, "error": exc.detail, "kind": exc.kind}
    return result.as_dict()


PACK = CapabilityPack(
    name="code.run_python",
    description=(
        "写一段 Python 代码并运行，返回 stdout / stderr。适合取数、转换格式、一次性计算。"
        "它在服务账号下真实执行（能调用 subprocess，等同命令行能力），所以默认每次都要你确认。"
        "临时目录里可以写文件；不能保证隔离，不要放敏感凭据。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "minLength": 1, "maxLength": 20000},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
        },
        "required": ["code"],
        "additionalProperties": False,
    },
    handler=_run,
    capability="code.run",
    level=PermissionLevel.PER_ACTION.value,
    levels=("auto", "once", "per_action", "deny"),
    timeout_seconds=70.0,
    produces=None,
    tags=["code", "exec"],
)
