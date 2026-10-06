"""linkinfo safety: only public addresses are contacted (also through redirects and DNS), and parsing stays
linear on crafted pages (it runs on the event loop shared with polling and the scheduler)."""

from __future__ import annotations

import socket
import time
from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from dumpbot.ai import linkinfo
from dumpbot.ai.linkinfo import PublicResolver, address_allowed, fetch_titles, parse_title


@pytest.fixture
async def lan_admin() -> AsyncIterator[tuple[str, list[str]]]:
    """A "router admin" page on another loopback address (127.0.0.2): (url, hits)."""
    hits: list[str] = []

    async def admin(request: web.Request) -> web.Response:
        hits.append(request.path)
        return web.Response(text="<title>Router admin</title>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/admin", admin)
    server = TestServer(app, host="127.0.0.2")
    try:
        await server.start_server()
    except OSError:  # pragma: no cover - platforms without the whole 127/8 on loopback
        pytest.skip("127.0.0.2 is not available")
    try:
        yield str(server.make_url("/admin")), hits
    finally:
        await server.close()


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("93.184.216.34", True),
        ("2606:4700::1111", True),
        ("::ffff:8.8.8.8", True),
        ("127.0.0.1", False),
        ("::1", False),
        ("10.0.0.1", False),
        ("192.168.1.1", False),
        ("172.16.0.1", False),
        ("169.254.169.254", False),  # cloud metadata
        ("100.64.0.1", False),  # CGNAT
        ("fd00::1", False),  # ULA
        ("fe80::1%eth0", False),
        ("::ffff:127.0.0.1", False),
        ("0.0.0.0", False),
        ("not-an-ip", False),
    ],
)
def test_address_allowed(host: str, allowed: bool):
    assert address_allowed(host) is allowed


async def test_loopback_and_lan_urls_are_not_fetched(fake, lan_admin):
    page = fake.page("p", "<title>Local</title>")
    admin, hits = lan_admin
    assert await fetch_titles([page, admin, "http://192.168.1.1/", "http://[::1]/", "http://localhost:1/"]) == {}
    assert fake.page_hits == [] and hits == []


async def test_redirect_to_a_lan_address_is_not_followed(fake, lan_admin, monkeypatch):
    monkeypatch.setattr(linkinfo, "address_allowed", lambda host: host == "127.0.0.1")  # "public" for this test
    admin, hits = lan_admin
    hop = fake.page("hop", "", status=302, headers={"Location": admin})
    assert await fetch_titles([hop]) == {}
    assert fake.page_hits == ["hop"]
    assert hits == []


async def test_redirects_to_allowed_hosts_are_followed(fake, monkeypatch):
    monkeypatch.setattr(linkinfo, "address_allowed", lambda host: True)
    fake.page("target", "<title>Moved here</title>")
    hop2 = fake.page("hop2", "", status=301, headers={"Location": "/pages/target"})  # relative
    hop1 = fake.page("hop1", "", status=307, headers={"Location": hop2})
    loop = fake.page("loop", "", status=302, headers={"Location": "/pages/loop"})
    assert await fetch_titles([hop1, loop]) == {hop1: "Moved here"}
    assert fake.page_hits.count("loop") == linkinfo.MAX_REDIRECTS + 1


async def test_resolver_keeps_only_public_addresses(monkeypatch):
    resolver = PublicResolver()

    def answer(*hosts: str):
        async def resolve(host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET):
            return [
                {"hostname": host, "host": h, "port": port, "family": family, "proto": 0, "flags": 0} for h in hosts
            ]

        return resolve

    try:
        monkeypatch.setattr(resolver._inner, "resolve", answer("127.0.0.1", "93.184.216.34"))
        assert [r["host"] for r in await resolver.resolve("mixed.example", 80)] == ["93.184.216.34"]
        monkeypatch.setattr(resolver._inner, "resolve", answer("10.1.2.3", "::1"))  # DNS rebinding to the LAN
        with pytest.raises(OSError):
            await resolver.resolve("rebind.example", 80)
    finally:
        await resolver.close()


@pytest.mark.parametrize(
    "body",
    [
        "<title>" * 10_000,  # unclosed titles: the old regex rescanned to the end from each of them
        "<meta " * 10_000,  # unclosed meta tags
        "<meta property=og:title " + "a" * 60_000 + ">",  # one huge tag
        "<meta content=x og:title " + "a1" * 2_000 + ">" + "<meta og:title " * 8_000 + ">",
    ],
    ids=["unclosed-titles", "unclosed-metas", "huge-meta", "nested-metas"],
)
def test_parse_title_is_linear_on_crafted_pages(body: str):
    started = time.perf_counter()
    assert parse_title(body.encode()) is None
    assert time.perf_counter() - started < 0.5


def test_parse_title_still_reads_normal_pages():
    page = '<html><head><meta charset="utf-8"><meta property="og:title" content="OG"><title>T</title></head>'
    assert parse_title(page.encode()) == "OG"
    assert parse_title(b"<head><title>Plain</title>") == "Plain"
    assert parse_title(b"<title>unclosed <title>still unclosed") is None
    assert parse_title(b'<meta name="og:title" content="Named"/>') == "Named"
    assert parse_title(b"<metadata og:title content=no><title>Not meta</title>") == "Not meta"
