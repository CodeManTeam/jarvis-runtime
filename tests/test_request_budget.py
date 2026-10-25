"""A request that grows past ~30KB kills the provider's stream; keep it under."""

from app.tool_loop import MAX_REQUEST_BYTES, SHRUNK_NOTE, _request_bytes, _shrink


def conversation_with(size_per_tool: int, tools: int) -> list[dict]:
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for index in range(tools):
        messages.append({
            "role": "tool",
            "tool_call_id": f"c{index}",
            "name": "event.list_recent",
            "content": "x" * size_per_tool,
        })
    return messages


def test_a_small_conversation_is_left_alone():
    messages = conversation_with(100, 2)
    before = _request_bytes(messages)
    _shrink(messages)
    assert _request_bytes(messages) == before
    assert SHRUNK_NOTE not in messages[-1]["content"]


def test_a_large_conversation_is_trimmed_under_the_budget():
    messages = conversation_with(8000, 6)
    assert _request_bytes(messages) > MAX_REQUEST_BYTES
    _shrink(messages)
    assert _request_bytes(messages) <= MAX_REQUEST_BYTES + 500
    assert any(SHRUNK_NOTE in m["content"] for m in messages if m["role"] == "tool")


def test_user_messages_are_never_shortened():
    messages = conversation_with(8000, 6)
    messages.append({"role": "user", "content": "keep me exactly as written"})
    _shrink(messages)
    assert messages[-1]["content"] == "keep me exactly as written"
