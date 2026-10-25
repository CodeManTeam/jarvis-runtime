"""The extension point: adding a file adds a capability, nothing else is edited."""

import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.capabilities import REGISTRY, CapabilityPack, delivery_hints
from app.db import Database
from app.policy import CAPABILITY_MATRIX
from app.service import JarvisService


@pytest.fixture
def service(tmp_path):
    return JarvisService(
        Database(str(tmp_path / "cap.db")),
        artifacts_root=tmp_path / "artifacts",
    )


def test_packs_are_discovered_from_the_package():
    names = {pack.name for pack in REGISTRY.packs()}
    assert "artifact.create_pptx" in names
    assert "artifact.create_web_page" in names


def test_a_pack_registers_its_permission_entry(service):
    # Nobody edited policy.py to add this.
    assert "artifact.create" in CAPABILITY_MATRIX
    assert CAPABILITY_MATRIX["artifact.create"]["default"] == "auto"


def test_every_pack_reaches_the_tool_registry(service):
    names = {tool["name"] for tool in service.tools.list_schemas()}
    assert "artifact.create_pptx" in names
    assert "artifact.create_web_page" in names
    # Core plumbing is still there.
    assert "system.escalate" in names


def test_a_new_pack_needs_only_a_declaration():
    """A pack with a handler becomes a tool with no other wiring."""
    calls = []

    def handler(args, service):
        calls.append(args)
        return {"ok": True}

    pack = CapabilityPack(
        name="test.echo",
        description="echo for tests",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        capability="test.echo",
        produces=None,
    )
    registry = type(REGISTRY)()
    registry.register(pack)
    assert "test.echo" in CAPABILITY_MATRIX
    tools = registry.tools(service=None)
    assert [tool.name for tool in tools] == ["test.echo"]
    assert tools[0].handler({"x": 1}) == {"ok": True}
    assert calls == [{"x": 1}]
    assert CAPABILITY_MATRIX["test.echo"]["default"] == "auto"


def test_pptx_pack_writes_a_real_presentation(service):
    result = service.artifacts.create_pptx(
        "周三会议",
        [
            {"title": "背景", "subtitle": "10 月 28 日"},
            {"title": "要点", "bullets": ["进度", "风险", "下一步"]},
        ],
        event_id="e1",
    )
    assert result["bytes"] > 0
    assert result["path"].startswith("/files/")
    stored = service.artifacts.root / result["path"].rsplit("/", 1)[-1]
    assert stored.exists()
    # A real pptx is a zip with this exact member.
    with zipfile.ZipFile(stored) as archive:
        assert "ppt/presentation.xml" in archive.namelist()


def test_web_page_pack_writes_html_and_is_served_from_its_own_path(service):
    result = service.artifacts.create_html(
        "会议一页", "<html><body>hello</body></html>", event_id="e2"
    )
    assert result["path"].endswith(".html")
    stored = service.artifacts.root / result["path"].rsplit("/", 1)[-1]
    assert "hello" in stored.read_text(encoding="utf-8")


def test_delivery_hints_come_from_the_packs():
    hints = delivery_hints()
    assert "pptx" in hints and any("链接" in item for item in hints["pptx"])
    assert "html" in hints


def test_pptx_rejects_empty_input(service):
    from app.artifacts import ArtifactError

    with pytest.raises(ArtifactError):
        service.artifacts.create_pptx("", [{"title": "x"}])
    with pytest.raises(ArtifactError):
        service.artifacts.create_pptx("标题", [])


def test_files_are_served_with_sandbox_csp(service):
    from app.artifacts import ArtifactStaticFiles

    result = service.artifacts.create_html("safe", "<p>safe</p>")
    app = FastAPI()
    app.mount("/files", ArtifactStaticFiles(
        directory=str(service.artifacts.root), check_dir=False
    ))
    client = TestClient(app)
    response = client.get(result["path"])
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == "sandbox"
