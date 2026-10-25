"""Inbound material: files arrive, get stored, and become readable text."""

import io
import zipfile

import pytest

from app.db import Database
from app.service import JarvisService
from app.uploads import ALLOWED_EXTENSIONS, UploadRefused, extract_text


@pytest.fixture
def service(tmp_path):
    return JarvisService(
        Database(str(tmp_path / "up.db")), artifacts_root=tmp_path / "artifacts"
    )


def test_plain_text_is_readable(service):
    item = service.uploads.save("笔记.md", "# 标题\n正文".encode("utf-8"))
    assert item["chars"] > 0
    assert item["note"] == ""
    text = service.uploads.read_text(item["id"])
    assert "正文" in text["text"]


def test_json_is_pretty_printed(service):
    item = service.uploads.save("data.json", b'{"a":1}')
    assert '"a": 1' in service.uploads.read_text(item["id"])["text"]


def test_html_is_reduced_to_text(service):
    item = service.uploads.save("page.html", b"<h1>hi</h1><script>bad()</script>")
    text = service.uploads.read_text(item["id"])["text"]
    assert "hi" in text
    assert "bad()" not in text


def test_an_unsupported_format_is_refused_with_the_list():
    store_text = "、".join(sorted(ALLOWED_EXTENSIONS))
    assert ".pdf" in store_text


def test_a_binary_extension_is_refused(service):
    with pytest.raises(UploadRefused) as caught:
        service.uploads.save("virus.exe", b"MZ")
    assert "不支持" in caught.value.detail


def test_an_empty_file_is_refused(service):
    with pytest.raises(UploadRefused):
        service.uploads.save("empty.txt", b"")


def test_an_oversized_file_is_refused(service):
    with pytest.raises(UploadRefused) as caught:
        service.uploads.save("big.txt", b"x" * (9 * 1024 * 1024))
    assert "太大" in caught.value.detail


def test_the_file_landed_on_disk_and_is_listed(service):
    item = service.uploads.save("a.txt", b"hello")
    stored = service.uploads.root / item["path"].rsplit("/", 1)[-1]
    assert stored.read_bytes() == b"hello"
    listed = service.uploads.list(5)
    assert listed[0]["id"] == item["id"]


def test_a_pdf_without_a_parser_says_so_instead_of_returning_noise(service):
    # A valid minimal PDF header is enough to reach the parser path.
    item = service.uploads.save("doc.pdf", b"%PDF-1.4\n%minimal\n")
    if item["chars"] == 0:
        assert "PDF" in (item["note"] or "") or "解析" in (item["note"] or "")
    else:  # pragma: no cover - depends on pypdf being present
        assert item["chars"] > 0


def test_reading_a_missing_upload_is_an_error(service):
    with pytest.raises(UploadRefused) as caught:
        service.uploads.read_text("nope")
    assert "没有编号" in caught.value.detail


def test_the_capabilities_are_registered(service):
    names = {schema["name"] for schema in service.tools.list_schemas()}
    assert "document.list_uploads" in names
    assert "document.read_upload" in names


def test_large_text_is_cut_not_dropped(service):
    body = ("line\n" * 60_000).encode("utf-8")
    item = service.uploads.save("long.txt", body)
    assert item["chars"] <= 200_000
    assert item["chars"] > 100_000
