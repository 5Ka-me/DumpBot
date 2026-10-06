"""Page titles for links in a message (no AI): ``og:title``, else ``<title>``.

Links come from saved content, including hidden ``text_link`` entities of forwarded posts, so fetching is limited
to public addresses: loopback, private/LAN, link-local, CGNAT and ULA hosts are never contacted, neither directly
nor through a redirect (redirects are followed by hand and every hop is checked; hostnames are checked on the
address actually connected to, so DNS rebinding cannot slip past). Parsing is linear in the page size: a crafted
page cannot block the event loop.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

log = logging.getLogger(__name__)

MAX_URLS = 5
MAX_REDIRECTS = 5
MAX_TITLE_CHARS = 200
MAX_TAG_CHARS = 4096  # a <meta> tag longer than this is not an og:title worth reading
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DumpBot/1.0; +personal link saver)",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "ru,en;q=0.8",
}
# Literal patterns only (no backtracking): each search is linear.
_TITLE_OPEN_RE = re.compile(r"<title", re.I)
_TITLE_CLOSE_RE = re.compile(r"</title", re.I)
_META_OPEN_RE = re.compile(r"<meta\b", re.I)
# Attribute names start where a name cannot continue (lookbehind) and never backtrack (possessive), so scanning a
# tag is linear too.
_ATTR_RE = re.compile(r"""(?<![\w:.-])([a-zA-Z_:.-]++)\s*+=\s*+(?:"([^"]*+)"|'([^']*+)'|([^\s"'>]++))""")
_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_.:-]+)""", re.I)


async def fetch_titles(
    urls: list[str],
    *,
    http: aiohttp.ClientSession | None = None,
    timeout_s: float = 5.0,
    max_bytes: int = 262144,
) -> dict[str, str]:
    """``{url: title}`` for up to 5 http(s) URLs, reading at most ``max_bytes`` of each page.

    Failures are swallowed: a link without a title is simply missing from the result. With ``http`` given,
    IP-literal hosts are still checked, but hostnames are resolved by that session's connector (use
    ``public_connector()`` for the same protection).
    """
    picked = _pick_urls(urls)
    if not picked:
        return {}
    session = http or aiohttp.ClientSession(connector=public_connector())
    try:
        titles = await asyncio.gather(*(_title_for(session, u, timeout_s, max_bytes) for u in picked))
    finally:
        if http is None:
            await session.close()
    return {u: t for u, t in zip(picked, titles, strict=True) if t}


def public_connector() -> aiohttp.TCPConnector:
    """A connector that only connects to public addresses (see ``PublicResolver``)."""
    return aiohttp.TCPConnector(resolver=PublicResolver())


class PublicResolver(AbstractResolver):
    """aiohttp's default resolver, keeping only public addresses; a host with none of them fails to resolve."""

    def __init__(self) -> None:
        self._inner = aiohttp.DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        found = await self._inner.resolve(host, port, family)
        public = [r for r in found if address_allowed(r["host"])]
        if not public:
            raise OSError(f"{host} has no public address")
        return public

    async def close(self) -> None:
        await self._inner.close()


def address_allowed(host: str) -> bool:
    """True for a globally routable IP address (not loopback, private, link-local, CGNAT, ULA, multicast…)."""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_global  # "fe80::1%eth0": scope id dropped
    except ValueError:
        return False


def _target_allowed(url: str) -> bool:
    """http(s) URL whose host is a hostname (checked when resolved) or a public IP literal."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True  # a name: PublicResolver checks the addresses it resolves to
    return address_allowed(host)


def _pick_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    for url in urls:
        url = (url or "").strip()
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme.lower() in ("http", "https") and parts.netloc and url not in out:
            out.append(url)
        if len(out) >= MAX_URLS:
            break
    return out


async def _title_for(session: aiohttp.ClientSession, url: str, timeout_s: float, max_bytes: int) -> str | None:
    try:
        async with asyncio.timeout(timeout_s):
            fetched = await _fetch(session, url, timeout_s, max_bytes)
    except Exception as e:
        log.debug("link title fetch failed: %s", type(e).__name__)
        return None
    return parse_title(*fetched) if fetched is not None else None


async def _fetch(
    session: aiohttp.ClientSession, url: str, timeout_s: float, max_bytes: int
) -> tuple[bytes, str | None] | None:
    """(body, charset) of an HTML page, following up to 5 redirects; None when there is nothing to read."""
    for _ in range(MAX_REDIRECTS + 1):
        if not _target_allowed(url):
            log.debug("link title: refusing a non-public address")
            return None
        async with session.get(
            url, headers=_HEADERS, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as resp:
            location = resp.headers.get("Location")
            if resp.status in _REDIRECT_STATUSES and location:
                url = urljoin(url, location)
                continue
            if resp.status != 200:
                return None
            ctype = resp.headers.get("Content-Type", "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype:
                return None  # images, PDFs, archives: nothing to read
            return await _read_capped(resp, max_bytes), resp.charset
    return None  # too many redirects


async def _read_capped(resp: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.content.iter_chunked(16384):
        chunk = chunk[: max_bytes - size]
        chunks.append(chunk)
        size += len(chunk)
        if size >= max_bytes:
            break
    return b"".join(chunks)


def parse_title(body: bytes, charset: str | None = None) -> str | None:
    """Title from HTML bytes: ``og:title`` first (usually without the site name), then ``<title>``."""
    text = _decode(body, charset)
    raw = _og_title(text) or _html_title(text)
    if not raw:
        return None
    title = " ".join(html.unescape(raw).split())
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1].rstrip() + "…"
    return title or None


def _decode(body: bytes, charset: str | None) -> str:
    if not charset:
        m = _CHARSET_RE.search(body[:4096])
        charset = m.group(1).decode("ascii", errors="ignore") if m else None
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _html_title(text: str) -> str | None:
    """Text of the first ``<title …>`` up to ``</title``: one forward scan, however many unclosed tags there are."""
    start = _TITLE_OPEN_RE.search(text)
    if start is None:
        return None
    gt = text.find(">", start.end())
    if gt == -1:
        return None
    end = _TITLE_CLOSE_RE.search(text, gt + 1)
    return text[gt + 1 : end.start()] if end is not None else None


def _og_title(text: str) -> str | None:
    pos = 0
    while (m := _META_OPEN_RE.search(text, pos)) is not None:
        end = text.find(">", m.end())
        if end == -1:
            return None
        pos = end + 1  # tags are not nested: continue after this one
        tag = text[m.start() : pos]
        if len(tag) > MAX_TAG_CHARS or "og:title" not in tag.lower():
            continue
        attrs = {a.group(1).lower(): next(g for g in a.groups()[1:] if g is not None) for a in _ATTR_RE.finditer(tag)}
        prop = (attrs.get("property") or attrs.get("name") or "").lower()
        if prop == "og:title" and attrs.get("content", "").strip():
            return attrs["content"]
    return None
