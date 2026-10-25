import asyncio
import json
from types import SimpleNamespace

import httpx

import app.llm as llm


class FakeAsyncClient:
    def __init__(self, response_data, captured):
        self.response_data = response_data
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, headers, json):
        self.captured.update(url=url, headers=headers, payload=json)
        request = httpx.Request("POST", url)
        return httpx.Response(200, json=self.response_data, request=request)


def _settings():
    return SimpleNamespace(
        llm_api_format="responses",
        deepseek_api_key="test-key",
        deepseek_base_url="https://aicyy.xyz/",
        deepseek_model="deepseek-test",
        vision_base_url="",
        vision_api_key="",
        vision_model="",
    )


def test_complete_with_tools_maps_responses_function_call(monkeypatch):
    captured = {}
    response_data = {
        "status": "completed",
        "output": [{
            "type": "function_call",
            "call_id": "call_123",
            "name": "lookup",
            "arguments": "{\"query\":\"weather\"}",
        }],
        "usage": {"input_tokens": 18, "output_tokens": 7},
    }
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(
        llm.httpx,
        "AsyncClient",
        lambda **_kwargs: FakeAsyncClient(response_data, captured),
    )

    result = asyncio.run(llm.complete_with_tools(
        [
            {"role": "system", "content": "Use tools when useful."},
            {"role": "user", "content": "Check the weather."},
        ],
        [{
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look up weather",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            },
        }],
    ))

    assert captured["url"] == "https://aicyy.xyz/v1/responses"
    assert captured["payload"]["input"][0]["role"] == "system"
    assert captured["payload"]["tools"][0]["name"] == "lookup"
    assert result["tool_calls"][0]["id"] == "call_123"
    assert json.loads(result["tool_calls"][0]["function"]["arguments"]) == {
        "query": "weather",
    }


def test_decide_uses_responses_json_format(monkeypatch):
    captured = {}
    response_data = {
        "status": "completed",
        "output": [{
            "type": "message",
            "content": [{
                "type": "output_text",
                "text": "{\"action\":\"ignore\",\"reply\":\"\"}",
            }],
        }],
        "usage": {"input_tokens": 11, "output_tokens": 5},
    }
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(
        llm.httpx,
        "AsyncClient",
        lambda **_kwargs: FakeAsyncClient(response_data, captured),
    )

    result = asyncio.run(llm.decide(
        [{"role": "user", "content": "Background conversation."}],
        {"action": "reply", "reply": ""},
    ))

    assert captured["url"] == "https://aicyy.xyz/v1/responses"
    assert captured["payload"]["text"] == {
        "format": {"type": "json_object"},
    }
    assert result["action"] == "ignore"


def test_responses_input_maps_image_and_function_output():
    result = llm._responses_input([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "{\"ok\":true}"},
    ])

    assert result[0]["content"][0] == {
        "type": "input_text",
        "text": "Describe this.",
    }
    assert result[0]["content"][1]["type"] == "input_image"
    assert result[1]["type"] == "function_call"
    assert result[2] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "{\"ok\":true}",
    }


def test_response_json_accepts_json_with_event_stream_content_type():
    body = {
        "object": "response",
        "status": "completed",
        "output": [],
    }
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=json.dumps(body).encode(),
        request=httpx.Request("POST", "[redacted]"),
    )

    assert llm._response_json(response) == body


def test_response_json_reads_responses_completed_sse_event():
    body = {
        "object": "response",
        "status": "completed",
        "output": [],
    }
    event = {
        "type": "response.completed",
        "response": body,
    }
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(
            "event: response.completed\r\n"
            f"data: {json.dumps(event)}\r\n\r\n"
            "data: [DONE]\r\n\r\n"
        ).encode(),
        request=httpx.Request("POST", "[redacted]"),
    )

    assert llm._response_json(response) == body


def test_response_json_uses_completed_response_after_text_deltas():
    body = {
        "object": "response",
        "status": "completed",
        "output_text": "流式回复完成",
        "output": [],
    }
    events = [
        {
            "type": "response.output_text.delta",
            "delta": "流式",
        },
        {
            "type": "response.completed",
            "response": body,
        },
    ]
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(
            f"data: {json.dumps(event, ensure_ascii=False)}\r\n\r\n"
            for event in events
        ).encode(),
        request=httpx.Request("POST", "[redacted]"),
    )

    assert llm._response_json(response) == body


def test_response_json_accumulates_delta_only_stream():
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(
            'data: {"type":"response.output_text.delta","delta":"流式"}\r\n\r\n'
            'data: {"type":"response.output_text.delta","delta":"回复"}\r\n\r\n'
            "data: [DONE]\r\n\r\n"
        ).encode(),
        request=httpx.Request("POST", "[redacted]"),
    )

    parsed = llm._response_json(response)
    assert parsed["output_text"] == "流式回复"
