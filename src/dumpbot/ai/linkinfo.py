"""Page titles for links in a message (no AI): ``og:title``, else ``<title>``."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)

MAX_URLS = 5
MAX_TITLE_CHARS = 200
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DumpBot/1.0; +personal link saver)",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "ru,en;q=0.8",
}
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([a-zA-Z_:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_.:-]+)""", re.I)


async def fetch_titles(
    urls: list[str],
    *,
    http: aiohttp.ClientSession | None = None,
    timeout_s: float = 5.0,
    max_bytes: int = 262144,
) -> dict[str, str]:
    """``{url: title}`` for up to 5 http(s) URLs, reading at most ``max_bytes`` of each page.

    Failures are swallowed: a link without a title is simply missing from the result.
    """
    picked = _pick_urls(urls)
    if not picked:
        return {}
    session = http or aiohttp.ClientSession()
    try:
        titles = await asyncio.gather(*(_title_for(session, u, timeout_s, max_bytes) for u in picked))
    finally:
        if http is None:
            await session.close()
    return {u: t for u, t in zip(picked, titles, strict=True) if t}


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
        async with session.get(
            url,
            headers=_HEADERS,
            allow_redirects=True,
            max_redirects=5,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            if resp.status != 200:
                return None
            ctype = resp.headers.get("Content-Type", "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype:
                return None  # images, PDFs, archives: nothing to read
            body = await _read_capped(resp, max_bytes)
            charset = resp.charset
    except Exception as e:
        log.debug("link title fetch failed: %s", type(e).__name__)
        return None
    return parse_title(body, charset)


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
    raw = _og_title(text)
    if not raw:
        m = _TITLE_RE.search(text)
        raw = m.group(1) if m else None
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


def _og_title(text: str) -> str | None:
    for tag in _META_RE.findall(text):
        attrs = {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None) for m in _ATTR_RE.finditer(tag)}
        prop = (attrs.get("property") or attrs.get("name") or "").lower()
        if prop == "og:title" and attrs.get("content", "").strip():
            return attrs["content"]
    return None
