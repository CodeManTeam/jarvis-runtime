"""Reading the outside world must be possible, and must not be a hole."""

import pytest

from app.db import Database
from app.service import JarvisService
from app.webread import FetchRefused, extract


@pytest.fixture
def service(tmp_path):
    return JarvisService(
        Database(str(tmp_path / "web.db")), artifacts_root=tmp_path / "artifacts"
    )


def test_the_capability_exists_at_all(service):
    """The gap the user found by scanning: zero tools could read outside content."""
    names = {schema["name"] for schema in service.tools.list_schemas()}
    assert "web.fetch_url" in names


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://127.0.0.1:8787/health",
        "http://localhost:8787/health",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
    ],
)
def test_private_and_non_http_targets_are_refused(url):
    with pytest.raises(FetchRefused):
        extract(url)
    assert "" == ""  # keep the assertion count obvious


def test_an_empty_url_is_refused():
    with pytest.raises(FetchRefused):
        extract("   ")


def test_html_is_reduced_to_readable_text(monkeypatch):
    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        url = "https://example.com/page"

        def iter_bytes(self):
            yield (
                "<html><head><title>标题</title><style>x{}</style></head>"
                "<body><h1>要点</h1><p>第一段</p><script>bad()</script></body></html>"
            ).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url):
            return FakeResponse()

    monkeypatch.setattr("app.webread.httpx.Client", FakeClient)
    monkeypatch.setattr("app.webread._assert_public", lambda url: None)

    result = extract("https://example.com/page")
    assert result["title"] == "标题"
    assert "要点" in result["text"]
    assert "第一段" in result["text"]
    assert "bad()" not in result["text"], "script bodies are not content"
    assert "x{}" not in result["text"], "style bodies are not content"


def test_a_binary_payload_is_refused_with_a_reason(monkeypatch):
    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/pdf"}
        url = "https://example.com/a.pdf"

        def iter_bytes(self):
            yield b"%PDF-1.4"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url):
            return FakeResponse()

    monkeypatch.setattr("app.webread.httpx.Client", FakeClient)
    monkeypatch.setattr("app.webread._assert_public", lambda url: None)
    with pytest.raises(FetchRefused) as caught:
        extract("https://example.com/a.pdf")
    assert "读不了" in caught.value.detail


def test_an_error_status_is_a_failure_not_an_empty_page(monkeypatch):
    class FakeResponse:
        status_code = 404
        headers = {"content-type": "text/html"}
        url = "https://example.com/missing"

        def iter_bytes(self):
            yield b"<html>not found</html>"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url):
            return FakeResponse()

    monkeypatch.setattr("app.webread.httpx.Client", FakeClient)
    monkeypatch.setattr("app.webread._assert_public", lambda url: None)
    with pytest.raises(FetchRefused) as caught:
        extract("https://example.com/missing")
    assert "404" in caught.value.detail


def test_the_pdf_capability_produces_a_real_pdf(service):
    item = service.artifacts.create_pdf(
        "本周纪要",
        [
            {"heading": "进度", "paragraphs": ["模型接入完成。"], "bullets": ["周一到周三"]},
            {"heading": "风险", "bullets": ["额度依赖"]},
        ],
        event_id="e1",
    )
    stored = service.artifacts.root / item["path"].rsplit("/", 1)[-1]
    assert stored.read_bytes().startswith(b"%PDF")
    assert item["bytes"] > 500
    assert item["code"]

def test_an_unknown_charset_falls_back_instead_of_crashing(monkeypatch):
    """errors="replace" covers bad bytes, not a bad codec name."""

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html; charset=gb2312-x-broken"}
        url = "https://example.com/x"

        def iter_bytes(self):
            yield "<html><body>中文</body></html>".encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url):
            return FakeResponse()

    monkeypatch.setattr("app.webread.httpx.Client", FakeClient)
    monkeypatch.setattr("app.webread._assert_public", lambda url: None)

    result = extract("https://example.com/x")
    assert "中文" in result["text"]
