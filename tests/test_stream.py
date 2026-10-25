import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main


@pytest.mark.asyncio
async def test_stream_rejects_missing_token(monkeypatch):
    monkeypatch.setattr(main, "settings", SimpleNamespace(device_token="secret"))
    with pytest.raises(HTTPException) as exc:
        await main.stream("")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_stream_emits_status_and_progress(monkeypatch):
    monkeypatch.setattr(main, "settings", SimpleNamespace(device_token="secret"))
    response = await main.stream("secret")
    generator = response.body_iterator
    main.event_bus.publish({"event": "job_status", "job_id": "j1", "status": "queued"})
    main.event_bus.publish({"event": "job_progress", "job_id": "j1", "seq": 1})
    first = await generator.__anext__()
    second = await generator.__anext__()
    text = (first + second).decode() if isinstance(first, bytes) else first + second
    assert "event: job_status" in text
    assert "event: job_progress" in text
    await generator.aclose()
