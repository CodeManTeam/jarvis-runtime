"""Read the outside world, safely.

Before this pack the agent could *write* a web page but could not *read* one, so
"summarise this link" was impossible. Everything it knew came from its own
database.

`fetch_url` is read-only, but "read-only" is not "harmless": a URL from a chat
message is attacker-controlled input and this server sits on a private network.
So the rules are hard:

* only http/https
* the host must resolve to a public address (no loopback, private, link-local,
  reserved or multicast ranges), re-checked on every redirect hop
* the response is capped in bytes and in extracted characters
* binary payloads are refused with a clear reason instead of returning noise
"""

from __future__ import annotations

import codecs
import ipaddress
import os
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

MAX_BYTES = 512 * 1024
MAX_CHARS = 8000
TIMEOUT_SECONDS = 12.0
MAX_REDIRECTS = 4
ALLOWED_SCHEMES = ("http", "https")

# Clash/Surge/Quantumult and friends run in fake-ip mode: every domain resolves
# into 198.18.0.0/15 (RFC 2544 benchmarking space). That range is "reserved" to
# Python, so the SSRF guard rejected every domain on such a machine — the
# capability was unusable for anyone behind a proxy. Opting in is explicit,
# because the guard is doing its job; the operator is deciding the proxy handles
# the real resolution.
# Proxies use the benchmarking prefixes for both families: RFC 2544 for IPv4 and
# RFC 5180 for IPv6. Missing the IPv6 one meant the opt-in still refused, because
# getaddrinfo returns the AAAA record first on this machine.
FAKE_IP_NETS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("2001:2::/48"),
)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def allowed_extra_networks() -> list[ipaddress._BaseNetwork]:
    """Networks that are private by the letter of the library but trusted here."""
    networks: list[ipaddress._BaseNetwork] = []
    if _env_flag("WEB_FETCH_ALLOW_FAKE_IP"):
        networks.extend(FAKE_IP_NETS)
    for chunk in (os.getenv("WEB_FETCH_ALLOWED_NETS") or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            networks.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            continue
    return networks


def _fake_ip_hint(addresses: list[str]) -> str:
    if any(
        ipaddress.ip_address(address) in network
        for address in addresses
        for network in FAKE_IP_NETS
    ):
        return (
            "你的机器开着代理（Clash/Surge 等的 fake-ip 模式），域名被解析到 "
            "198.18.x.x 这个保留段，安全策略默认拒绝它。"
            "确认要走代理的话，在 .env 里设置 WEB_FETCH_ALLOW_FAKE_IP=true 即可。"
        )
    return ""


class FetchRefused(RuntimeError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _TextExtractor(HTMLParser):
    """Turn a page into readable text without pulling in a parser dependency."""

    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data.strip()
            return
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        joined = " ".join(self.parts)
        lines = [line.strip() for line in joined.split("\n")]
        return "\n".join(line for line in lines if line)


def _assert_public(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise FetchRefused(f"只支持 http/https，收到的是 {parsed.scheme or '(空)'}。")
    host = parsed.hostname
    if not host:
        raise FetchRefused("这个链接没有主机名。")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise FetchRefused(f"域名解析失败：{host}") from exc
    allowed = allowed_extra_networks()
    addresses = [info[4][0] for info in infos]
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if any(ip in network for network in allowed):
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            # A link from a chat message must not reach the host's own network.
            hint = _fake_ip_hint(addresses)
            raise FetchRefused(
                f"{host} 指向内网地址（{address}），已拒绝访问。{hint}"
            )


def extract(url: str) -> dict[str, Any]:
    """Fetch and read one URL. Raises FetchRefused; never fakes a success."""
    if not url or not url.strip():
        raise FetchRefused("没有给链接。")

    target = url.strip()
    current = target
    status = 0
    headers: dict[str, str] = {}
    body = b""
    final_url = current

    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": "Jarvis/2.0 (+personal assistant)"},
        ) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                _assert_public(current)
                with client.stream("GET", current) as response:
                    status = response.status_code
                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchRefused("对方要求跳转，但没给目标地址。")
                        current = urljoin(current, location)
                        continue
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    final_url = str(response.url)
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_BYTES:
                            break
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    break
            else:
                raise FetchRefused(f"跳转超过 {MAX_REDIRECTS} 次，停下了。")
    except FetchRefused:
        raise
    except httpx.HTTPError as exc:
        raise FetchRefused(f"读取失败：{type(exc).__name__}") from exc

    if status >= 400:
        raise FetchRefused(f"对方返回 {status}，没能读到内容。")

    content_type = headers.get("content-type", "")
    lowered = content_type.lower()
    charset = "utf-8"
    if "charset=" in lowered:
        charset = lowered.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    # A server may advertise anything ("gb2312-x", "utf8-bom"). errors="replace"
    # only covers bad bytes, not a bad codec name, so validate before decoding.
    try:
        codecs.lookup(charset)
    except LookupError:
        charset = "utf-8"

    if "text/html" in lowered or "application/xhtml" in lowered:
        parser = _TextExtractor()
        parser.feed(body.decode(charset, "replace"))
        text = parser.text()
        title = parser.title.strip()
    elif "json" in lowered or lowered.startswith("text/") or "xml" in lowered:
        text = body.decode(charset, "replace")
        title = ""
    else:
        raise FetchRefused(f"这是 {content_type or '未知类型'}，我读不了二进制内容。")

    truncated = len(text) > MAX_CHARS
    return {
        "url": final_url,
        "status": status,
        "content_type": content_type,
        "title": title,
        "chars": len(text),
        "truncated": truncated,
        "text": text[:MAX_CHARS],
    }
