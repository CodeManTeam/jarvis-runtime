"""Tool results are capped: a 35KB dump broke the provider's streaming endpoint."""

from app.tool_loop import MAX_TOOL_RESULT_CHARS, _serialize_content


def test_a_large_result_is_truncated_with_a_reason():
    payload = {"events": [{"content": "x" * 400} for _ in range(80)]}
    text = _serialize_content(payload)
    assert len(text) <= MAX_TOOL_RESULT_CHARS + 80
    assert "truncated" in text
    assert "narrow the query" in text


def test_a_small_result_is_untouched():
    text = _serialize_content({"ok": True, "value": 3})
    assert text == '{"ok": true, "value": 3}'
    assert "truncated" not in text


def test_non_json_values_still_serialize():
    assert "object at" in _serialize_content(object())


def test_the_cap_is_documented_next_to_the_constant():
    # The number exists because of a real outage; the comment must stay.
    import inspect
    import app.tool_loop as module

    source = inspect.getsource(module)
    index = source.index("MAX_TOOL_RESULT_CHARS =")
    assert "upstream_error" in source[max(0, index - 300):index]
