import asyncio
from pathlib import Path

from app.workspace_agent import workspace_list, workspace_read, workspace_write


def test_workspace_files_round_trip_and_list(tmp_path):
    assert asyncio.run(workspace_write("a/b.txt", "hello", tmp_path))["ok"]
    assert asyncio.run(workspace_read("a/b.txt", tmp_path))["content"] == "hello"
    listed = asyncio.run(workspace_list(".", tmp_path, 2))
    assert any(item["path"] == "a/b.txt" for item in listed["items"])


def test_workspace_files_reject_escape_and_overwrite(tmp_path):
    assert not asyncio.run(workspace_write("../x.txt", "x", tmp_path))["ok"]
    assert asyncio.run(workspace_write("x.txt", "one", tmp_path))["ok"]
    refused = asyncio.run(workspace_write("x.txt", "two", tmp_path))
    assert not refused["ok"] and "overwrite" in refused["error"]
    assert asyncio.run(workspace_write("x.txt", "two", tmp_path, True))["ok"]


def test_workspace_read_truncates(tmp_path):
    (tmp_path / "x.txt").write_text("abcdef", encoding="utf-8")
    result = asyncio.run(workspace_read("x.txt", tmp_path, 3))
    assert result["truncated"] is True
    assert result["content"].startswith("abc")
    assert "truncated" in result["content"]
