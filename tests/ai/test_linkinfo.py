from __future__ import annotations

import socket

import aiohttp
import pytest

from dumpbot.ai import linkinfo
from dumpbot.ai.linkinfo import fetch_titles, parse_title


@pytest.fixture(autouse=True)
def local_pages_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake pages are served from 127.0.0.1, which fetch_titles never contacts otherwise
    (see test_linkinfo_safety.py)."""
    monkeypatch.setattr(linkinfo, "address_allowed", lambda host: True)


async def test_title_and_og_title(fake):
    plain = fake.page("plain", "<html><head><title>  Hello &amp; welcome\n to   the site </title></head></html>")
    og = fake.page(
        "og",
        "<html><head><title>Site | Article</title>"
        "<meta content=\"Статья про &quot;asyncio&quot;\" property='og:title'></head></html>",
    )
    named = fake.page("named", '<meta name="og:title" content="By name"><title>ignored</title>')

    titles = await fetch_titles([plain, og, named])

    assert titles == {plain: "Hello & welcome to the site", og: 'Статья про "asyncio"', named: "By name"}


async def test_empty_og_title_falls_back_to_title(fake):
    url = fake.page("p", '<meta property="og:title" content="  "><title>Real title</title>')
    assert await fetch_titles([url]) == {url: "Real title"}


async def test_long_title_is_capped_at_200_chars(fake):
    url = fake.page("long", "<title>" + "Очень длинный заголовок " * 30 + "</title>")
    title = (await fetch_titles([url]))[url]
    assert len(title) == 200
    assert title.endswith("…")


async def test_charset_from_header_and_meta(fake):
    header = fake.page("cp1251", "<title>Привет, мир</title>", charset="windows-1251")
    meta = fake.page(
        "meta", '<meta charset="windows-1251"><title>Заметка</title>'.encode("cp1251"), content_type="text/html"
    )
    assert await fetch_titles([header, meta]) == {header: "Привет, мир", meta: "Заметка"}


async def test_size_cap(fake):
    late = fake.page("late", "<html><head>" + "<!-- padding -->" * 200 + "<title>Too late</title></head></html>")
    early = fake.page("early", "<title>Early</title>" + "x" * 1_000_000)
    assert await fetch_titles([late, early], max_bytes=1000) == {early: "Early"}


async def test_non_http_urls_are_skipped_and_at_most_five_fetched(fake):
    pages = [fake.page(f"p{i}", f"<title>Page {i}</title>") for i in range(7)]
    urls = ["ftp://example.com/f", "mailto:a@b.c", "javascript:alert(1)", "tg://resolve?domain=x", "http://", pages[0]]
    urls += pages  # duplicates of pages[0] are fetched once

    titles = await fetch_titles(urls)

    assert titles == {pages[i]: f"Page {i}" for i in range(5)}
    assert sorted(fake.page_hits) == [f"p{i}" for i in range(5)]


async def test_errors_are_swallowed(fake):
    good = fake.page("good", "<title>Good</title>")
    missing = fake.url("missing")  # 404 with a <title>: error pages are not titles
    image = fake.page("img", b"\x89PNG <title>no</title>", content_type="image/png")
    slow = fake.page("slow", "<title>Slow</title>", delay=2.0)
    no_title = fake.page("none", "<html><body>no title here</body></html>")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        refused = f"http://127.0.0.1:{sock.getsockname()[1]}/x"

    titles = await fetch_titles([good, missing, image, slow, no_title], timeout_s=0.5)
    assert titles == {good: "Good"}
    assert await fetch_titles([refused], timeout_s=3.0) == {}


async def test_nothing_to_fetch():
    assert await fetch_titles([]) == {}
    assert await fetch_titles(["not a url", "file:///etc/passwd"]) == {}


async def test_given_session_is_reused_and_left_open(fake):
    url = fake.page("p", "<title>Shared</title>")
    async with aiohttp.ClientSession() as http:
        assert await fetch_titles([url], http=http) == {url: "Shared"}
        assert not http.closed


def test_parse_title_directly():
    assert parse_title(b"<TITLE lang='ru'>A &lt;b&gt;</TITLE>") == "A <b>"
    assert parse_title("<title>Ёлка</title>".encode("cp1251"), "windows-1251") == "Ёлка"
    assert parse_title(b"<title>x</title>", "no-such-charset") == "x"
    assert parse_title(b"<title>   </title>") is None
    assert parse_title(b"") is None
