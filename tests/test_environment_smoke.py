"""Real-environment checks: does this capability actually work on this machine?

Unit tests prove the logic; these prove the machine. They skip loudly when the
environment cannot support the check, because a silently passing test that never
exercised the network is worse than no test.
"""

import socket

import pytest

from app.webread import (
    FAKE_IP_NETS,
    FetchRefused,
    _assert_public,
    allowed_extra_networks,
    extract,
)


def resolves_to_fake_ip(host: str = "example.com") -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    import ipaddress

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in network for network in FAKE_IP_NETS):
            return True
    return False


@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    monkeypatch.delenv("WEB_FETCH_ALLOW_FAKE_IP", raising=False)
    monkeypatch.delenv("WEB_FETCH_ALLOWED_NETS", raising=False)


def test_the_local_dns_situation_is_reported():
    """Not an assertion about the machine, a record of it in the test output."""
    fake = resolves_to_fake_ip()
    print(f"fake-ip environment: {fake}")
    assert isinstance(fake, bool)


def test_fake_ip_is_refused_by_default(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.233", port or 0))]

    monkeypatch.setattr("app.webread.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FetchRefused) as caught:
        _assert_public("https://example.com")
    detail = caught.value.detail
    assert "198.18" in detail
    assert "WEB_FETCH_ALLOW_FAKE_IP" in detail, "the message must say how to fix it"


def test_the_opt_in_allows_it(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.233", port or 0))]

    monkeypatch.setattr("app.webread.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setenv("WEB_FETCH_ALLOW_FAKE_IP", "true")
    _assert_public("https://example.com")  # must not raise


def test_the_ipv6_benchmark_range_is_part_of_fake_ip():
    # Missing this made the opt-in still refuse, because AAAA came back first.
    networks = [str(network) for network in FAKE_IP_NETS]
    assert "198.18.0.0/15" in networks
    assert "2001:2::/48" in networks


def test_extra_networks_can_be_configured(monkeypatch):
    monkeypatch.setenv("WEB_FETCH_ALLOWED_NETS", "203.0.113.0/24, bogus, 10.9.0.0/16")
    networks = [str(network) for network in allowed_extra_networks()]
    assert "203.0.113.0/24" in networks
    assert "10.9.0.0/16" in networks
    assert len(networks) == 2, "an unparsable entry is ignored, not fatal"


def test_private_networks_stay_refused_even_with_extra_networks(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.9", port or 0))]

    monkeypatch.setattr("app.webread.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setenv("WEB_FETCH_ALLOWED_NETS", "203.0.113.0/24")
    with pytest.raises(FetchRefused):
        _assert_public("https://intranet.example")


def test_a_real_page_can_be_read_or_the_environment_says_why_not():
    if resolves_to_fake_ip():
        pytest.skip("本机代理使用 fake-ip；设置 WEB_FETCH_ALLOW_FAKE_IP=true 后可验证")
    try:
        result = extract("https://example.com")
    except FetchRefused as exc:
        pytest.skip(f"这台机器访问不到外网：{exc.detail[:80]}")
    assert result["status"] == 200
    assert result["chars"] > 0
