"""The web server around the API: picture previews from Telegram (fake bot) with the in-memory LRU, the frontend
build with the index.html fallback, JSON errors for unknown API paths and for crashes (without their text)."""

from __future__ import annotations

import logging

import aiohttp
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetFile

from dumpbot.web import api_lists
from dumpbot.web.files import ImageCache

# --- picture previews ----------------------------------------------------------------------------------------------


@pytest.fixture
async def album(add_raw, make_items):
    """An item saved from an album: a photo, a PNG sent as a file and a PDF (not a picture)."""
    first = await add_raw(
        message_id=60, content_type="photo", file_id="ph", file_unique_id="u-ph", media_group_id="g", file_size=3
    )
    await add_raw(
        message_id=61, content_type="document", file_id="pdf", mime_type="application/pdf", media_group_id="g"
    )
    await add_raw(
        message_id=62, content_type="image_document", file_id="png", mime_type="image/PNG", media_group_id="g"
    )
    [created] = await make_items({"kind": "meme", "title": "Мемы"}, raw_id=first.id)
    return created


async def test_pictures_come_from_telegram_once(api, bot, album) -> None:
    bot.files = {"ph": b"\xff\xd8jpeg-bytes", "png": b"\x89PNG-bytes"}
    item_id = album.item.id

    first = await api.get(f"/api/files/{item_id}/0")
    assert first.status == 200 and first.data == b"\xff\xd8jpeg-bytes"
    assert first.headers["Content-Type"] == "image/jpeg"
    assert first.headers["Cache-Control"] == "private, max-age=86400"
    assert first.headers["X-Content-Type-Options"] == "nosniff"
    second = await api.get(f"/api/files/{item_id}/1")
    assert second.data == b"\x89PNG-bytes" and second.headers["Content-Type"] == "image/png"

    again = await api.get(f"/api/files/{item_id}/0")
    assert again.data == b"\xff\xd8jpeg-bytes"
    assert [c["file"] for c in bot.calls_of("download")] == ["ph", "png"]  # the repeat came from the cache

    for path in (f"/api/files/{item_id}/2", f"/api/files/{item_id}/x", "/api/files/999/0"):
        reply = await api.get(path)
        assert reply.status == 404 and reply.data["error"]["code"] == "not_found"


async def test_pictures_need_auth(api, album) -> None:
    assert (await api.get(f"/api/files/{album.item.id}/0", auth_header="")).status == 401


async def test_telegram_errors_of_a_preview(api, bot, album, caplog) -> None:
    item_id = album.item.id
    bot.fail("download", TelegramBadRequest(method=GetFile(file_id="ph"), message="Bad Request: file is too big"))
    reply = await api.get(f"/api/files/{item_id}/0")
    assert (reply.status, reply.data["error"]["code"]) == (404, "too_big")
    bot.fail(
        "download", TelegramNetworkError(method=GetFile(file_id="ph"), message="https://api.telegram.org/file/bot1:x")
    )
    with caplog.at_level(logging.INFO):
        reply = await api.get(f"/api/files/{item_id}/0")
    assert (reply.status, reply.data["error"]["code"]) == (502, "telegram_unavailable")
    assert "api.telegram.org" not in caplog.text
    reply = await api.get(f"/api/files/{item_id}/0")  # Telegram has no bytes for it
    assert reply.status == 404


async def test_a_step_has_no_pictures_of_its_own(api, album) -> None:
    steps = (await api.post(f"/api/items/{album.item.id}/steps", {"title": "Шаг"})).data["item"]["steps"]
    step = (await api.get(f"/api/items/{steps[0]['id']}")).data
    assert step["images"] == []


def _size(file_id: str, width: int, height: int, file_size: int) -> dict:
    return {
        "file_id": file_id,
        "file_unique_id": f"u-{file_id}",
        "width": width,
        "height": height,
        "file_size": file_size,
    }


async def test_list_thumbnails_are_small_sizes_telegram_already_has(api, bot, add_raw, make_items) -> None:
    """``?size=thumb`` (a 52 px row thumbnail): the smallest photo size with a side of at least 160 px, the thumbnail
    Telegram made for a picture sent as a file, the picture itself when there is none."""
    sizes = [_size("ph-s", 90, 68, 1_000), _size("ph-m", 320, 240, 9_000), _size("ph", 1280, 960, 150_000)]
    first = await add_raw(
        message_id=70,
        content_type="photo",
        file_id="ph",
        file_unique_id="u-ph",
        media_group_id="t",
        width=1280,
        height=960,
        raw_json={"photo": sizes},
    )
    await add_raw(
        message_id=71,
        content_type="image_document",
        file_id="png",
        file_unique_id="u-png",
        mime_type="image/png",
        media_group_id="t",
        raw_json={"document": {"file_id": "png", "thumbnail": _size("png-t", 320, 180, 7_000)}},
    )
    await add_raw(
        message_id=72,
        content_type="image_document",
        file_id="gif",
        file_unique_id="u-gif",
        mime_type="image/gif",
        media_group_id="t",
        raw_json={"document": {"file_id": "gif"}},
    )
    [created] = await make_items({"kind": "meme", "title": "Скриншоты"}, raw_id=first.id)
    bot.files = {"ph": b"big", "ph-m": b"m", "png": b"png", "png-t": b"png-thumb", "gif": b"gif"}
    path = f"/api/files/{created.item.id}"

    photo = await api.get(f"{path}/0?size=thumb")
    assert (photo.status, photo.data, photo.headers["Content-Type"]) == (200, b"m", "image/jpeg")
    assert photo.headers["Cache-Control"] == "private, max-age=86400"
    file_thumb = await api.get(f"{path}/1?size=thumb")
    assert (file_thumb.data, file_thumb.headers["Content-Type"]) == (b"png-thumb", "image/jpeg")
    no_thumb = await api.get(f"{path}/2?size=thumb")
    assert (no_thumb.data, no_thumb.headers["Content-Type"]) == (b"gif", "image/gif")
    assert (await api.get(f"{path}/0")).data == b"big"  # the card's preview stays the full picture
    assert [c["file"] for c in bot.calls_of("download")] == ["ph-m", "png-t", "gif", "ph"]
    reply = await api.get(f"{path}/0?size=huge")
    assert (reply.status, reply.data["error"]["field"]) == (400, "size")


def test_image_cache_drops_the_least_recently_used() -> None:
    cache = ImageCache(max_bytes=10, item_max_bytes=6)
    cache.put("a", b"aaaa", "image/png")
    cache.put("b", b"bbbb", "image/png")
    assert cache.get("a") == (b"aaaa", "image/png")  # "a" is now the most recent
    cache.put("c", b"cccc", "image/jpeg")
    assert cache.get("b") is None and cache.get("a") is not None and cache.get("c") is not None
    assert cache.size == 8 and len(cache) == 2
    cache.put("big", b"x" * 7, "image/png")  # bigger than one entry may be: served, not cached
    assert cache.get("big") is None and cache.size == 8
    cache.put("a", b"aa", "image/png")  # replaced
    assert cache.size == 6


# --- frontend ------------------------------------------------------------------------------------------------------


async def test_frontend_files_and_fallback(api) -> None:
    index = await api.get("/", auth_header="")
    assert index.status == 200 and b"<title>" in index.data and index.headers["Cache-Control"] == "no-cache"
    route = await api.get("/items/12", auth_header="")  # the app's own route
    assert route.status == 200 and route.data == index.data
    asset = await api.get("/assets/app-1a2b.js", auth_header="")
    assert asset.status == 200 and asset.data == b"console.log('app')"
    assert asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert (await api.get("/favicon.svg", auth_header="")).status == 200
    assert (await api.get("/assets/missing.js", auth_header="")).status == 404


@pytest.mark.parametrize(
    "path", ["/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt", "/..%5csecret.txt"]
)
async def test_no_files_outside_the_build(api, path: str) -> None:
    async with aiohttp.ClientSession() as http:
        url = f"http://127.0.0.1:{api.client.server.port}{path}"
        async with http.get(url) as response:
            body = await response.read()
    assert b"outside the build" not in body


async def test_without_a_build_only_the_api_answers(ctx, frozen) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from dumpbot.web.server import build_web_app

    client = TestClient(TestServer(build_web_app(ctx, static_dir=None), host="127.0.0.1"))
    await client.start_server()
    try:
        async with client.get("/") as response:
            assert response.status == 404
    finally:
        await client.close()


async def test_unknown_api_paths_are_json(api) -> None:
    reply = await api.get("/api/nothing")
    assert (reply.status, reply.data["error"]["code"]) == (404, "not_found")
    reply = await api.call("PUT", "/api/settings", {})
    assert (reply.status, reply.data["error"]["code"]) == (405, "method_not_allowed")


async def test_a_crash_is_500_without_its_text(api, monkeypatch, caplog) -> None:
    secret = "купить подарок Ане"

    async def boom(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(api_lists.query, "open_tasks", boom)
    with caplog.at_level(logging.ERROR):
        reply = await api.get("/api/tasks")
    assert (reply.status, reply.data["error"]["code"]) == (500, "internal")
    assert secret not in str(reply.data) and secret not in caplog.text
    assert "GET /api/tasks failed: RuntimeError" in caplog.text
