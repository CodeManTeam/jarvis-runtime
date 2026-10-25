from app.tool_loop import COMPACT_AT, compact


def test_compaction_keeps_tool_pairs_and_recent_turn():
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "goal"}]
    for i in range(8):
        messages += [
            {"role": "assistant", "content": "", "tool_calls": [{"id": str(i), "function": {"name": "workspace_read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": str(i), "name": "workspace_read", "content": "x" * 3000},
        ]
    messages += [{"role": "assistant", "content": "last", "tool_calls": [{"id": "last", "function": {"name": "workspace_read", "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": "last", "name": "workspace_read", "content": "final"}]
    result = compact(messages)
    assert len(str(result)) < len(str(messages))
    assert any(m.get("content", "").startswith("[Earlier steps summary]") for m in result if m["role"] == "user")
    calls = {c["id"] for m in result if m["role"] == "assistant" for c in m.get("tool_calls", [])}
    assert all(m.get("tool_call_id") in calls for m in result if m["role"] == "tool")
    assert result[-1]["tool_call_id"] == "last"


def test_compaction_noop_under_threshold():
    messages = [{"role": "user", "content": "small"}]
    assert compact(messages) == messages
