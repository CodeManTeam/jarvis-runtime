"""Provider-neutral multi-round assistant tool-calling loop.

The module deliberately knows nothing about a particular LLM SDK.  Adapters only
need to return an assistant message containing ``tool_calls`` and execute calls
by name.  Tool failures are represented as tool messages so the model can
recover or explain the failure in the same conversation.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence


Message = dict[str, Any]
ToolSchema = dict[str, Any]
LLMCallback = Callable[
    [list[Message], list[ToolSchema]],
    Awaitable[Any] | Any,
]
ToolExecutorCallback = Callable[
    [str, dict[str, Any]],
    Awaitable[Any] | Any,
]

LoopStatus = Literal[
    "completed",
    "llm_error",
    "llm_timeout",
    "max_rounds",
]


@dataclass(frozen=True)
class ToolCall:
    """The normalized subset of an assistant tool call used by the loop."""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LoopResult:
    """Structured result returned by :class:`AgentLoop`."""

    status: LoopStatus
    final_text: str | None
    messages: list[Message]
    rounds: int
    tool_calls: int
    error: str | None = None
    tool_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.status == "completed"


def _request_bytes(conversation: Sequence[Mapping[str, Any]]) -> int:
    try:
        return len(json.dumps(list(conversation), ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _shrink(conversation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim the oldest tool output until the request fits the budget.

    Tool output is the only part that can be safely shortened: dropping user
    messages would change the request's meaning.
    """
    if _request_bytes(conversation) <= MAX_REQUEST_BYTES:
        return conversation
    for message in conversation:
        if _request_bytes(conversation) <= MAX_REQUEST_BYTES:
            break
        if message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        if len(content) > 800:
            message["content"] = content[:800] + SHRUNK_NOTE
    return conversation


COMPACT_AT = 18000
KEEP_RECENT = 6


def compact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold old complete tool-call turns into a local deterministic summary."""
    if _request_bytes(messages) <= COMPACT_AT:
        return messages
    if len(messages) <= KEEP_RECENT + 2:
        return messages
    prefix = messages[:2]
    recent_start = max(2, len(messages) - KEEP_RECENT)
    while recent_start > 2 and messages[recent_start].get("role") == "tool":
        recent_start -= 1
    middle = messages[2:recent_start]
    recent = messages[recent_start:]
    summaries: list[str] = []
    removed = 0
    i = 0
    while i < len(middle):
        message = middle[i]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            for call in message.get("tool_calls") or []:
                fn = call.get("function", call)
                summaries.append(f"{fn.get('name', 'tool')} {fn.get('arguments', '')}")
            removed += 1
            i += 1
            while i < len(middle) and middle[i].get("role") == "tool":
                summaries.append(str(middle[i].get("content", ""))[:240].splitlines()[0])
                removed += 1
                i += 1
            continue
        if message.get("role") not in {"system", "user"}:
            summaries.append(str(message.get("content", ""))[:240].splitlines()[0])
            removed += 1
        i += 1
    if not removed:
        return messages
    summary = {"role": "user", "content": f"[Earlier steps summary] {removed} messages; " + " | ".join(summaries)}
    return prefix + [summary] + recent


class AgentLoop:
    """Run an assistant/tool conversation until a final assistant response.

    ``llm`` receives the current message list and the advertised tool schemas.
    It may return either a mapping/object shaped like an assistant message or a
    provider response containing ``choices[0].message``.  The latter is only a
    structural convenience; no provider SDK is imported or required.

    ``tool_executor`` receives ``(tool_name, parsed_arguments)``.  It may be
    synchronous or asynchronous.  A failed tool call is appended as a tool
    message and the model gets an opportunity to recover.  LLM failures and
    timeouts terminate the loop with an explicit status.
    """

    def __init__(
        self,
        *,
        llm: LLMCallback,
        tool_executor: ToolExecutorCallback,
        max_rounds: int = 8,
        llm_timeout: float | None = 90.0,
        tool_timeout: float | None = 30.0,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if llm_timeout is not None and llm_timeout <= 0:
            raise ValueError("llm_timeout must be greater than zero or None")
        if tool_timeout is not None and tool_timeout <= 0:
            raise ValueError("tool_timeout must be greater than zero or None")

        self._llm = llm
        self._tool_executor = tool_executor
        self.max_rounds = max_rounds
        self.llm_timeout = llm_timeout
        self.tool_timeout = tool_timeout

    async def run(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> LoopResult:
        """Execute the loop without mutating the caller's messages or schemas."""

        conversation = [deepcopy(dict(message)) for message in messages]
        tool_schemas = [deepcopy(dict(tool)) for tool in tools]
        total_tool_calls = 0
        tool_errors: list[dict[str, Any]] = []

        for round_number in range(1, self.max_rounds + 1):
            try:
                raw_response = await self._invoke(
                    self._llm,
                    _shrink(deepcopy(conversation)),
                    deepcopy(tool_schemas),
                    timeout=self.llm_timeout,
                )
            except asyncio.TimeoutError:
                return LoopResult(
                    status="llm_timeout",
                    final_text=None,
                    messages=conversation,
                    rounds=round_number,
                    tool_calls=total_tool_calls,
                    error="LLM callback timed out",
                    tool_errors=tool_errors,
                )
            except Exception as exc:
                return LoopResult(
                    status="llm_error",
                    final_text=None,
                    messages=conversation,
                    rounds=round_number,
                    tool_calls=total_tool_calls,
                    error=_error_text(exc),
                    tool_errors=tool_errors,
                )

            try:
                assistant = _normalize_assistant_message(raw_response)
                calls = _parse_tool_calls(assistant.get("tool_calls"))
            except Exception as exc:
                return LoopResult(
                    status="llm_error",
                    final_text=None,
                    messages=conversation,
                    rounds=round_number,
                    tool_calls=total_tool_calls,
                    error=_error_text(exc),
                    tool_errors=tool_errors,
                )
            conversation.append(assistant)
            if not calls:
                return LoopResult(
                    status="completed",
                    final_text=_content_to_text(assistant.get("content")),
                    messages=conversation,
                    rounds=round_number,
                    tool_calls=total_tool_calls,
                    tool_errors=tool_errors,
                )

            for call in calls:
                total_tool_calls += 1
                try:
                    value = await self._invoke(
                        self._tool_executor,
                        call.name,
                        call.arguments,
                        timeout=self.tool_timeout,
                    )
                    tool_message = _tool_message(call, value)
                except asyncio.TimeoutError:
                    error = f"tool '{call.name}' timed out"
                    tool_errors.append(
                        {"call_id": call.call_id, "name": call.name, "error": error}
                    )
                    tool_message = _tool_message(call, {"error": error})
                except Exception as exc:
                    error = _error_text(exc)
                    tool_errors.append(
                        {"call_id": call.call_id, "name": call.name, "error": error}
                    )
                    tool_message = _tool_message(
                        call,
                        {"error": f"tool '{call.name}' failed: {error}"},
                    )
                conversation.append(tool_message)

        return LoopResult(
            status="max_rounds",
            final_text=None,
            messages=conversation,
            rounds=self.max_rounds,
            tool_calls=total_tool_calls,
            error=f"maximum tool loop rounds reached ({self.max_rounds})",
            tool_errors=tool_errors,
        )

    @staticmethod
    async def _invoke(
        callback: Callable[..., Any],
        *args: Any,
        timeout: float | None,
    ) -> Any:
        result = callback(*args)
        if inspect.isawaitable(result):
            if timeout is None:
                return await result
            return await asyncio.wait_for(result, timeout=timeout)
        return result


def _normalize_assistant_message(response: Any) -> Message:
    """Extract a provider-neutral assistant message from common response shapes."""

    value = _as_mapping(response)
    choices = value.get("choices")
    if isinstance(choices, Sequence) and not isinstance(choices, (str, bytes)):
        if not choices:
            raise ValueError("LLM response contained no choices")
        choice = _as_mapping(choices[0])
        value = _as_mapping(choice.get("message", choice))

    message: Message = {"role": "assistant"}
    if "content" in value:
        message["content"] = value["content"]
    if "tool_calls" in value:
        message["tool_calls"] = deepcopy(value["tool_calls"])
    if "refusal" in value:
        message["refusal"] = value["refusal"]
    if not any(key in message for key in ("content", "tool_calls", "refusal")):
        raise ValueError("LLM response is not an assistant message")
    return message


def _parse_tool_calls(raw_calls: Any) -> list[ToolCall]:
    if raw_calls is None:
        return []
    if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
        raise ValueError("assistant tool_calls must be a list")

    calls: list[ToolCall] = []
    for index, raw_call in enumerate(raw_calls):
        value = _as_mapping(raw_call)
        function = _as_mapping(value.get("function", value))
        name = str(function.get("name") or "").strip()
        if not name:
            raise ValueError(f"tool call {index} has no name")

        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"tool call '{name}' has invalid JSON arguments"
                ) from exc
        if not isinstance(arguments, Mapping):
            raise ValueError(f"tool call '{name}' arguments must be an object")

        call_id = str(value.get("id") or f"tool-call-{index + 1}")
        calls.append(
            ToolCall(
                call_id=call_id,
                name=name,
                arguments=dict(arguments),
            )
        )
    return calls


def _tool_message(call: ToolCall, value: Any) -> Message:
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "name": call.name,
        "content": _serialize_content(value),
    }


# A tool may legitimately return tens of kilobytes (20 events, 20 tasks). Feeding
# that back verbatim blew up the provider's streaming endpoint with an opaque
# upstream_error, so the loop caps what it echoes back and says it did.
MAX_TOOL_RESULT_CHARS = 6000

# The provider's streaming endpoint fails with an opaque upstream_error once a
# request grows past roughly 30KB (observed at 41KB, fine at 27KB). Budget the
# whole conversation, not just one tool result.
MAX_REQUEST_BYTES = 26000
SHRUNK_NOTE = "...[trimmed to fit the request budget]"


def _serialize_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        omitted = len(text) - MAX_TOOL_RESULT_CHARS
        text = (
            text[:MAX_TOOL_RESULT_CHARS]
            + f"...[truncated {omitted} chars; narrow the query for more]"
        )
    return text


def _content_to_text(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    return _serialize_content(content)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    result = {
        name: getattr(value, name)
        for name in ("role", "content", "tool_calls", "function", "name", "arguments", "id", "choices")
        if hasattr(value, name)
    }
    if result:
        return result
    raise ValueError("callback response must be a mapping or message-like object")


def _error_text(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
