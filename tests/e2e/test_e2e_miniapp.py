"""End-to-end: the Telegram Mini App (docs/features/iteration-2.md §D) inside the whole application.

The bot starts its web server next to polling and sets the "📱 Свалка" menu button; the app's requests (initData
signed like Telegram signs it) go through the real API, services, job worker and the fake chutes; picture previews come
from the fake Telegram through the bot's own getFile + download. Nothing from the app shows up in the chat.

Harness: conftest.py (``e2e`` with the ``miniapp`` fixture on); the clock starts on Tue 2026-10-06 10:12 Warsaw.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from dumpbot.db.models import RawMessage

CALL_BANK = "завтра в 10 позвонить в банк"
BANK_TASK = {"kind": "task", "title": "Позвонить в банк", "when": {"text": "завтра в 10", "day": "+1", "time": "10:00"}}
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-meme" * 8


@pytest.fixture
def miniapp() -> bool:
    return True


async def until_processed(e2e: Any, raw_id: int) -> dict[str, Any]:
    async def done() -> dict[str, Any] | None:
        status, data = await e2e.api("GET", f"/api/inbox/{raw_id}")
        assert status == 200, data
        return None if data["pending"] else data

    return await e2e.wait_db(done, f"inbox message {raw_id} processed")


async def test_menu_button_opens_the_app(e2e: Any) -> None:
    [call] = e2e.tg.of("setChatMenuButton")
    assert int(call.params["chat_id"]) == 4242
    assert call.params["menu_button"] == {
        "type": "web_app",
        "text": "📱 Свалка",
        "web_app": {"url": "https://dump.tail1234.ts.net"},
    }
    status, page = await e2e.api("GET", "/")
    assert status == 200 and b"<title>" in page


async def test_inbox_to_card_to_done_without_the_chat(e2e: Any) -> None:
    e2e.ai.script(CALL_BANK, {"intent": "save", "items": [BANK_TASK]})
    status, accepted = await e2e.api("POST", "/api/inbox", {"text": CALL_BANK})
    assert status == 202 and accepted["pending"] is True

    data = await until_processed(e2e, accepted["raw_id"])
    [row] = data["items"]
    assert row["title"] == "Позвонить в банк" and row["when_text"] == "завтра 10:00"
    [req] = e2e.ai.extraction_requests()
    assert req.msg == CALL_BANK

    status, item = await e2e.api("GET", f"/api/items/{row['id']}")
    assert item["when_text"] == "🔔 завтра 10:00" and item["source"]["from_app"] is True

    status, answer = await e2e.api("POST", f"/api/items/{row['id']}/done")
    assert status == 200 and answer["message"] == "✅ Готово" and answer["item"]["status"] == "done"
    status, today = await e2e.api("GET", "/api/today")
    assert today["done"][0]["id"] == row["id"]

    raw = (await e2e.all(RawMessage))[0]
    assert raw.source == "app" and raw.message_id < 0
    assert e2e.tg.sent == [] and e2e.tg.reactions() == []  # the chat saw nothing of it


async def test_picture_preview_comes_from_telegram(e2e: Any) -> None:
    e2e.tg.add_file("meme-big", JPEG, "photos/meme.jpg")
    e2e.ai.script("кот", {"intent": "save", "items": [{"kind": "meme", "title": "Кот и коробка"}]})
    photo = [{"file_id": "meme-big", "file_unique_id": "mb", "width": 640, "height": 480, "file_size": len(JPEG)}]
    message_id = e2e.tg.push_message(photo=photo, caption="кот")
    await e2e.reaction(message_id, "👌")
    await e2e.idle()

    status, feed = await e2e.api("GET", "/api/feed")
    [row] = feed["rows"]
    assert row["title"] == "Кот и коробка" and row["images"] == 1
    status, item = await e2e.api("GET", f"/api/items/{row['id']}")
    [image] = item["images"]
    assert image["width"] == 640 and image["mime_type"] == "image/jpeg"

    downloads = len(e2e.tg.downloads)
    status, data = await e2e.api("GET", image["url"])
    assert status == 200 and data == JPEG
    status, again = await e2e.api("GET", image["url"])
    assert again == JPEG and len(e2e.tg.downloads) == downloads + 1  # the second one came from the cache


async def test_ask_and_settings(e2e: Any) -> None:
    e2e.ai.script(CALL_BANK, {"intent": "save", "items": [BANK_TASK]})
    status, accepted = await e2e.api("POST", "/api/inbox", {"text": CALL_BANK})
    await until_processed(e2e, accepted["raw_id"])
    e2e.ai.script("что у меня завтра?", {"intent": "query", "query": {"range": "tomorrow", "answer": "summary"}})
    e2e.ai.answer = "Завтра в 10 — позвонить в банк."
    status, answer = await e2e.api("POST", "/api/ask", {"question": "что у меня завтра?"})
    assert status == 200 and answer["answer"] == "Завтра в 10 — позвонить в банк."
    assert [r["title"] for r in answer["rows"]] == ["Позвонить в банк"]

    status, settings = await e2e.api("PATCH", "/api/settings", {"soon_days": 3, "slot_evening": "20:00"})
    assert status == 200 and settings["prefs"]["soon_days"] == 3 and settings["prefs"]["slot_evening"] == "20:00"
    status, usage = await e2e.api("GET", "/api/usage")
    assert usage["calls"] == 3 and {p["purpose"] for p in usage["by_purpose"]} == {"extract", "answer"}
    assert json.dumps(usage)  # plain JSON
    assert e2e.tg.sent == []
