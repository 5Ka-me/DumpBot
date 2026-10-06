"""Picture previews for the Mini App: ``GET /api/files/{item_id}/{n}`` (docs/features/miniapp-api.md).

Files are never stored (only Telegram's ``file_id``), so the bytes come from Telegram (getFile + download) and are
kept in an in-memory LRU of at most ``CACHE_BYTES``. Only pictures of the message the item came from are served
(``query.item_images``: photos and JPEG / PNG / WebP / GIF files), with their own content type and ``nosniff``. The
app loads them with fetch (the Authorization header) into blob URLs. Errors never log the file URL (it contains the
bot token).
"""

from __future__ import annotations

import io
import logging
from collections import OrderedDict
from typing import Final

import aiohttp
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiohttp import web

from dumpbot.db.models import Item
from dumpbot.services import query
from dumpbot.web.http import ApiError, ctx_of, path_id

log = logging.getLogger(__name__)

CACHE_BYTES: Final = 50 * 1024 * 1024
ITEM_MAX_BYTES: Final = 10 * 1024 * 1024  # bigger pictures are served, not cached
DOWNLOAD_MAX_BYTES: Final = 20 * 1024 * 1024  # the Bot API's getFile limit
MAX_AGE_S: Final = 86400

GONE: Final = "Картинки нет"
TOO_BIG: Final = "Картинка больше 20 МБ — Telegram не отдаёт её боту"
TELEGRAM_DOWN: Final = "Telegram сейчас не отдаёт картинку — попробуй позже"


class ImageCache:
    """Picture bytes and their content type by Telegram file id, least recently used dropped first."""

    def __init__(self, max_bytes: int = CACHE_BYTES, item_max_bytes: int = ITEM_MAX_BYTES) -> None:
        self.max_bytes = max_bytes
        self.item_max_bytes = item_max_bytes
        self.size = 0
        self._entries: OrderedDict[str, tuple[bytes, str]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str) -> tuple[bytes, str] | None:
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def put(self, key: str, data: bytes, content_type: str) -> None:
        if len(data) > self.item_max_bytes:
            return
        old = self._entries.pop(key, None)
        if old is not None:
            self.size -= len(old[0])
        self._entries[key] = (data, content_type)
        self.size += len(data)
        while self.size > self.max_bytes and self._entries:
            _, (dropped, _) = self._entries.popitem(last=False)
            self.size -= len(dropped)


IMAGES: Final = web.AppKey("images", ImageCache)


def routes() -> list[web.RouteDef]:
    return [web.get("/api/files/{id}/{n}", picture)]


async def picture(request: web.Request) -> web.StreamResponse:
    """Picture ``n`` (0-based, ``ItemDetail.images``) of the item's message."""
    item_id = path_id(request)
    n = request.match_info.get("n", "")
    if not n.isdecimal() or len(n) > 3:
        raise ApiError(404, "not_found", GONE)
    ctx = ctx_of(request)
    async with ctx.sessionmaker() as session:
        item = await session.get(Item, item_id)
        pictures = await query.item_images(session, item) if item is not None else []
    if int(n) >= len(pictures):
        raise ApiError(404, "not_found", GONE)
    pic = pictures[int(n)]
    content_type = "image/jpeg" if pic.content_type == "photo" else (pic.mime_type or "image/jpeg").lower()
    cache = request.app[IMAGES]
    key = pic.file_unique_id or pic.file_id or ""
    cached = cache.get(key)
    if cached is None:
        if pic.file_size is not None and pic.file_size > DOWNLOAD_MAX_BYTES:
            raise ApiError(404, "too_big", TOO_BIG)
        data = await _download(request, pic.file_id or "")
        cache.put(key, data, content_type)
        cached = (data, content_type)
    return web.Response(
        body=cached[0],
        content_type=cached[1],
        headers={"Cache-Control": f"private, max-age={MAX_AGE_S}", "X-Content-Type-Options": "nosniff"},
    )


async def _download(request: web.Request, file_id: str) -> bytes:
    """The file's bytes from Telegram (404 when Telegram no longer has it, 502 when it cannot be reached)."""
    try:
        buf = await ctx_of(request).bot.download(file_id)
    except TelegramBadRequest as e:
        log.info("picture preview: Telegram refused the file (%s)", type(e).__name__)
        too_big = "too big" in (e.message or "").lower()
        raise ApiError(404, "too_big" if too_big else "not_found", TOO_BIG if too_big else GONE) from None
    except (TelegramAPIError, aiohttp.ClientError, TimeoutError, OSError) as e:
        log.warning("picture preview: download failed (%s)", type(e).__name__)
        raise ApiError(502, "telegram_unavailable", TELEGRAM_DOWN) from None
    data = buf.getvalue() if isinstance(buf, io.BytesIO) else (buf.read() if buf is not None else b"")
    if not data:
        raise ApiError(404, "not_found", GONE)
    return bytes(data)
