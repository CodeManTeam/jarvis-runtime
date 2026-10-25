from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Callable

import asyncio
import logging

import httpx

from .config import settings


class LLMError(RuntimeError):
    pass


usage_recorder: Callable[[int, int], None] | None = None
usage_reserver: Callable[[int, int], dict[str, Any]] | None = None
usage_settler: Callable[[dict[str, Any], int, int], None] | None = None
usage_releaser: Callable[[dict[str, Any]], None] | None = None


def set_usage_recorder(recorder: Callable[[int, int], None] | None) -> None:
    global usage_recorder
    usage_recorder = recorder


def set_usage_budget(
    reserver: Callable[[int, int], dict[str, Any]] | None = None,
    settler: Callable[[dict[str, Any], int, int], None] | None = None,
    releaser: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    global usage_reserver, usage_settler, usage_releaser
    usage_reserver = reserver
    usage_settler = settler
    usage_releaser = releaser


def _record_response_usage(
    data: dict[str, Any],
    reservation: dict[str, Any] | None = None,
) -> None:
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    if reservation is not None and usage_settler:
        usage_settler(reservation, input_tokens, output_tokens)
        return
    if usage_recorder:
        usage_recorder(input_tokens, output_tokens)


def _reserve_request(messages: list[dict[str, Any]], max_tokens: int) -> dict[str, Any] | None:
    if usage_reserver is None:
        return None
    estimated_input = max(1, len(json.dumps(messages, ensure_ascii=False, default=str)) // 4)
    return usage_reserver(estimated_input, max(0, int(max_tokens)))


def _release_request(reservation: dict[str, Any] | None) -> None:
    if reservation is not None and usage_releaser:
        usage_releaser(reservation)


def _uses_responses_api() -> bool:
    return getattr(settings, "llm_api_format", "chat_completions") in {
        "responses",
        "openai_responses",
    }


def _responses_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if not re.search(r"/v1$", base):
        base += "/v1"
    return base + "/responses"


def _responses_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray)):
        return [{"type": "input_text", "text": str(content or "")}]

    converted: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type in {"text", "input_text"}:
            converted.append({
                "type": "input_text",
                "text": str(item.get("text") or ""),
            })
        elif item_type in {"image_url", "input_image"}:
            image = item.get("image_url")
            if isinstance(image, Mapping):
                image_url = image.get("url")
                detail = image.get("detail")
            else:
                image_url = image
                detail = item.get("detail")
            if image_url:
                image_item: dict[str, Any] = {
                    "type": "input_image",
                    "image_url": str(image_url),
                }
                if detail:
                    image_item["detail"] = detail
                converted.append(image_item)
    return converted


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or ""),
                "output": str(message.get("content") or ""),
            })
            continue

        content = message.get("content")
        if content is not None or not message.get("tool_calls"):
            items.append({
                "role": role,
                "content": _responses_content(content),
            })

        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                arguments = function.get("arguments") or "{}"
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                items.append({
                    "type": "function_call",
                    "call_id": str(call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": arguments,
                })
    return items


def _responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for tool in tools:
        function = tool.get("function") or {}
        if tool.get("type") != "function" or not function.get("name"):
            continue
        converted.append({
            "type": "function",
            "name": function["name"],
            "description": function.get("description") or "",
            "parameters": function.get("parameters") or {
                "type": "object",
                "properties": {},
            },
            "strict": False,
        })
    return converted


def _responses_assistant_message(data: dict[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    normalized_calls: list[dict[str, Any]] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "output_text":
                    text_parts.append(str(part.get("text") or ""))
                elif part.get("type") == "refusal":
                    text_parts.append(str(part.get("refusal") or ""))
        elif item.get("type") == "function_call":
            arguments = item.get("arguments") or "{}"
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            normalized_calls.append({
                "id": str(item.get("call_id") or item.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(item.get("name") or ""),
                    "arguments": str(arguments),
                },
            })

    if not text_parts and isinstance(data.get("output_text"), str):
        text_parts.append(data["output_text"])
    return {
        "role": "assistant",
        "content": "".join(text_parts),
        "tool_calls": normalized_calls,
        "finish_reason": data.get("status"),
    }


def _response_text(data: dict[str, Any]) -> str:
    message = _responses_assistant_message(data)
    text = str(message["content"] or "")
    if not text and message["tool_calls"]:
        raise LLMError("LLM Responses call returned tools where text was required")
    return text


async def _complete_with_tools_once(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    max_tokens: int = 2048,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    """Call an OpenAI-compatible chat endpoint with MCP-style tool schemas.

    The returned message keeps the provider-neutral shape used by AgentLoop:
    ``content`` plus optional ``tool_calls`` entries containing ``id``, ``name``
    and JSON ``arguments``.
    """
    if not settings.deepseek_api_key:
        raise LLMError("AI API Key 尚未配置。")
    reservation = _reserve_request(messages, max_tokens)
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    if _uses_responses_api():
        payload = {
            "model": model,
            "input": _responses_input(messages),
            "tools": _responses_tools(tools),
            "tool_choice": "auto",
            "temperature": 0.1,
            "max_output_tokens": max_tokens,
            "stream": True,
        }
        url = _responses_url(settings.deepseek_base_url)
    else:
        url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
        if response.is_error:
            detail = response.text[:300].strip()
            raise LLMError(
                f"LLM tool call failed with {response.status_code}"
                + (f": {detail}" if detail else "")
            )
        data = _response_json(response)
        _record_response_usage(data, reservation)
        reservation = None
        if _uses_responses_api():
            return _responses_assistant_message(data)
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise LLMError("LLM tool response did not contain choices")
        message = choices[0].get("message") or {}
        if not isinstance(message, dict):
            raise LLMError("LLM tool response did not contain a message")
        normalized_calls: list[dict[str, Any]] = []
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            if not isinstance(function, dict) or not function.get("name"):
                continue
            arguments = function.get("arguments") or "{}"
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise LLMError(
                        f"LLM returned invalid arguments for tool {function['name']}"
                    ) from exc
            if not isinstance(arguments, dict):
                raise LLMError(f"Tool arguments for {function['name']} are not an object")
            normalized_calls.append({
                "id": str(call.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(function["name"]),
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            })
        return {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": normalized_calls,
            "finish_reason": choices[0].get("finish_reason"),
        }
    except BaseException:
        _release_request(reservation)
        raise


_router: Any = None


def set_router(router: Any) -> None:
    """Install the process-wide router (tests and the service both use this)."""
    global _router
    _router = router


def current_router() -> Any:
    global _router
    if _router is None:
        from .routing import build_default_router

        _router = build_default_router()
    return _router


logger = logging.getLogger(__name__)

RETRY_DELAY_SECONDS = 0.8

# Causes that a second identical request can plausibly fix. Quota and auth are
# deliberately absent: retrying them only delays the honest answer.
TRANSIENT_MARKERS = (
    "upstream_error",
    "upstream request failed",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "connection reset",
    "temporarily",
)


def is_transient(error: BaseException) -> bool:
    if isinstance(error, (httpx.TimeoutException, httpx.TransportError)):
        return True
    text = str(error).lower()
    return any(marker in text for marker in TRANSIENT_MARKERS)


async def complete_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    max_tokens: int = 2048,
    capability: str = "tool_call",
) -> dict[str, Any]:
    """Plan/tool call with capability-aware fallback.

    A transient upstream blip gets one immediate retry on the same endpoint,
    because surfacing "upstream_error" to the user for a two-second hiccup is
    noise. Fatal causes (quota, auth) are never retried — they will not heal.
    Beyond that, at most one attempt on a different endpoint, and every
    degradation is reported through the router so it reaches the audit trail.
    It can only remove capability, never add permission.
    """
    router = current_router()
    endpoint = router.choose(capability)
    try:
        _size = len(json.dumps(messages, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:  # noqa: BLE001 - logging must never break the call
        _size = -1
    logger.info(
        "llm request endpoint=%s model=%s messages=%d bytes=%d tools=%d",
        endpoint.id, endpoint.model, len(messages), _size, len(tools),
    )
    candidates = [endpoint]
    fallback = getattr(router, "fallback", None)
    if fallback is not None and fallback.id != endpoint.id:
        candidates.append(fallback)

    last_error: Exception | None = None
    for candidate in candidates:
        for attempt in range(2):
            try:
                result = await _complete_with_tools_once(
                    messages,
                    tools,
                    max_tokens=max_tokens,
                    model=candidate.model,
                    timeout=candidate.timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.TransportError, LLMError) as exc:
                if attempt == 0 and is_transient(exc):
                    logger.warning(
                        "transient llm failure, retrying once endpoint=%s error=%s",
                        candidate.id,
                        str(exc)[:160],
                    )
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue
                router.record_failure(candidate, str(exc))
                last_error = exc
                break
            router.record_success(candidate)
            return result
    raise last_error if last_error is not None else LLMError("LLM call failed")


def _response_json(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    text = response.text.strip()
    is_json_body = text.startswith("{") or text.startswith("[")
    if "json" not in content_type.lower() and not is_json_body:
        if "event-stream" in content_type.lower():
            return _parse_sse_response(text)
        raise LLMError(
            f"LLM returned non-JSON response ({response.status_code}, "
            f"{content_type or 'unknown content type'})"
        )
    try:
        value = json.loads(text)
    except ValueError as exc:
        if "event-stream" in content_type.lower():
            return _parse_sse_response(text)
        raise LLMError("LLM returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise LLMError("LLM response is not an object")
    return value


def _parse_sse_response(body: str) -> dict[str, Any]:
    last_payload: dict[str, Any] | None = None
    text_deltas: list[str] = []
    tool_calls: dict[str, dict[str, Any]] = {}
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line[5:].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise LLMError("LLM Responses stream contained invalid JSON") from exc
        if not isinstance(payload, dict):
            continue
        last_payload = payload
        event_type = str(payload.get("type") or "")
        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                text_deltas.append(delta)
        elif event_type in {
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }:
            call_id = str(payload.get("item_id") or payload.get("call_id") or "")
            if call_id:
                call = tool_calls.setdefault(call_id, {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": str(payload.get("name") or ""),
                    "arguments": "",
                })
                delta = payload.get("delta")
                if isinstance(delta, str):
                    call["arguments"] += delta
                if payload.get("name"):
                    call["name"] = str(payload["name"])
        if event_type == "response.failed":
            response_payload = payload.get("response")
            error = (
                response_payload.get("error")
                if isinstance(response_payload, dict)
                else None
            ) or payload.get("error")
            raise LLMError(f"LLM Responses stream failed: {error or 'unknown error'}")
        response = payload.get("response")
        if event_type == "response.completed" and isinstance(response, dict):
            return response
        if payload.get("object") == "response" and payload.get("status") in {
            "completed",
            "incomplete",
        }:
            return payload
    if last_payload:
        response = last_payload.get("response")
        if isinstance(response, dict):
            return response
        if last_payload.get("object") == "response":
            return last_payload
    if text_deltas or tool_calls:
        output: list[dict[str, Any]] = []
        if text_deltas:
            output.append({
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": "".join(text_deltas),
                }],
            })
        output.extend(tool_calls.values())
        return {
            "object": "response",
            "status": "completed",
            "output": output,
            "output_text": "".join(text_deltas),
        }
    raise LLMError("LLM Responses stream ended without a completed response")


async def chat(messages: list[dict[str, Any]]) -> str:
    if not settings.deepseek_api_key:
        return "AI API Key 尚未配置。请先在服务器的 .env 中设置 DEEPSEEK_API_KEY。"

    has_image = any(
        isinstance(message.get("content"), list)
        and any(item.get("type") == "image_url" for item in message["content"])
        for message in messages
    )
    base_url = (
        settings.vision_base_url
        if has_image and settings.vision_base_url
        else settings.deepseek_base_url
    )
    api_key = (
        settings.vision_api_key
        if has_image and settings.vision_api_key
        else settings.deepseek_api_key
    )
    model = (
        settings.vision_model
        if has_image and settings.vision_model
        else settings.deepseek_model
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if _uses_responses_api():
        url = _responses_url(base_url)
        payload = {
            "model": model,
            "input": _responses_input(messages),
            "temperature": 0.2,
            "max_output_tokens": 2048,
            "stream": True,
        }
    else:
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
        }
    reservation = _reserve_request(messages, 2048)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, headers=headers, json=payload)
        if response.is_error:
            raise LLMError(f"LLM request failed with {response.status_code}")
        data = _response_json(response)
        _record_response_usage(data, reservation)
        reservation = None
        if _uses_responses_api():
            return _response_text(data)
        return data["choices"][0]["message"]["content"]
    except BaseException:
        _release_request(reservation)
        raise


async def decide(
    messages: list[dict[str, Any]],
    schema: dict[str, Any],
) -> dict[str, Any]:
    """让模型完成理解和决策，代码只验证输出并执行安全动作。"""
    if not settings.deepseek_api_key:
        return {
            "action": "reply",
            "reply": "AI API Key 尚未配置。请先在服务器的 .env 中设置 DEEPSEEK_API_KEY。",
        }

    has_image = any(
        isinstance(message.get("content"), list)
        and any(item.get("type") == "image_url" for item in message["content"])
        for message in messages
    )
    base_url = (
        settings.vision_base_url
        if has_image and settings.vision_base_url
        else settings.deepseek_base_url
    )
    api_key = (
        settings.vision_api_key
        if has_image and settings.vision_api_key
        else settings.deepseek_api_key
    )
    model = (
        settings.vision_model
        if has_image and settings.vision_model
        else settings.deepseek_model
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if _uses_responses_api():
        url = _responses_url(base_url)
        payload = {
            "model": model,
            "input": _responses_input(messages),
            "temperature": 0.1,
            "max_output_tokens": 2048,
            "text": {"format": {"type": "json_object"}},
            "stream": True,
        }
    else:
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 2048,
            "response_format": {"type": "json_object"},
        }
    reservation = _reserve_request(messages, 2048)
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(url, headers=headers, json=payload)
        if response.is_error:
            raise LLMError(f"LLM decision failed with {response.status_code}")
        data = _response_json(response)
        _record_response_usage(data, reservation)
        reservation = None
        content = (
            _response_text(data)
            if _uses_responses_api()
            else str(data["choices"][0]["message"]["content"])
        )
        if "```" in content:
            content = content.replace("```json", "").replace("```", "").strip()
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                # A normal conversational answer is still a valid AI decision.
                return {
                    "action": "reply",
                    "reply": content,
                    **{
                        key: value
                        for key, value in schema.items()
                        if key not in {"action", "reply"}
                    },
                }
            try:
                result = json.loads(match.group(0))
            except json.JSONDecodeError as nested_exc:
                raise LLMError("LLM returned malformed JSON decision") from nested_exc
        if not isinstance(result, dict):
            raise LLMError("LLM decision is not an object")
        result = {
            str(key).strip().strip("<>").strip(): value
            for key, value in result.items()
        }
        for key, value in schema.items():
            if key not in result:
                result[key] = value
        return result
    except BaseException:
        _release_request(reservation)
        raise

