"""End to end: the real app (polling, OwnerOnly, capture, job worker, processor, scheduler, callbacks) against a
local fake Telegram Bot API server and a local fake chutes.ai server. Nothing leaves 127.0.0.1."""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import Callable
from typing import Any

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select

from dumpbot.bot.app import build_app
from dumpbot.db.models import Item, Occurrence, RawMessage

OWNER = 4242
STRANGER = 777
BOT_ID = 123456
TOKEN = f"{BOT_ID}:TEST-token"

EXTRACTION = {
    "intent": "save",
    "items": [
        {"kind": "task", "title": "Купить молоко", "when": {"text": "завтра в 10", "day": "+1", "time": "10:00"}}
    ],
}


class FakeBotAPI:
    """Just enough of the Bot API for polling: queued updates out, every other call recorded."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sent_ids: list[int] = []  # message ids returned by sendMessage, in order
        self._update_ids = itertools.count(1)
        self._message_ids = itertools.count(8001)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self.handle)
        return app

    def push(self, **update: Any) -> None:
        self.updates.append({"update_id": next(self._update_ids), **update})

    def calls_of(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]

    async def handle(self, request: web.Request) -> web.Response:
        assert request.match_info["token"] == TOKEN
        method = request.match_info["method"]
        params = {k: _loads(v) for k, v in (await request.post()).items()}
        if method != "getUpdates":
            self.calls.append((method, params))
        return web.json_response({"ok": True, "result": await self._result(method, params)})

    async def _result(self, method: str, params: dict[str, Any]) -> Any:
        if method == "getMe":
            return {"id": BOT_ID, "is_bot": True, "first_name": "Dump", "username": "dump_test_bot"}
        if method == "getUpdates":
            offset = int(params.get("offset") or 0)
            for _ in range(10):  # short long-poll
                ready = [u for u in self.updates if u["update_id"] >= offset]
                if ready:
                    return ready
                await asyncio.sleep(0.05)
            return []
        if method in ("sendMessage", "editMessageText"):
            message_id = params.get("message_id") or next(self._message_ids)
            if method == "sendMessage":
                self.sent_ids.append(message_id)
            return {
                "message_id": int(message_id),
                "date": int(time.time()),
                "chat": {"id": int(params["chat_id"]), "type": "private"},
                "text": params["text"],
            }
        return True


def _loads(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "[{":
        return json.loads(value)
    return value


async def _chat_completions(request: web.Request) -> web.Response:
    body = await request.json()
    assert body["messages"]  # a real prompt was built
    content = json.dumps(EXTRACTION, ensure_ascii=False)
    return web.json_response(
        {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 900, "completion_tokens": 60}}
    )


async def _models(request: web.Request) -> web.Response:
    return web.json_response({"data": []})


def _user(user_id: int) -> dict[str, Any]:
    return {"id": user_id, "is_bot": False, "first_name": "Дима"}


def _message(message_id: int, text: str, user_id: int = OWNER) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": user_id, "type": "private"},
        "from": _user(user_id),
        "text": text,
    }


async def _until(condition: Callable[[], Any], limit_s: float = 20.0) -> Any:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def servers():
    telegram, chutes = FakeBotAPI(), web.Application()
    chutes.router.add_post("/v1/chat/completions", _chat_completions)
    chutes.router.add_get("/v1/models", _models)
    tg_server, ai_server = TestServer(telegram.app(), host="127.0.0.1"), TestServer(chutes, host="127.0.0.1")
    await tg_server.start_server()
    await ai_server.start_server()
    yield telegram, str(tg_server.make_url("")).rstrip("/"), str(ai_server.make_url("")).rstrip("/")
    await tg_server.close()
    await ai_server.close()


async def test_message_to_card_to_done(servers, engine, db_url, settings_factory, tmp_path) -> None:
    telegram, tg_url, ai_url = servers
    settings = settings_factory(
        database_url=db_url,
        chutes_base_url=f"{ai_url}/v1",
        heartbeat_file=str(tmp_path / "heartbeat"),
        scheduler_interval_s=0.5,
    )
    exits: list[int] = []
    session = AiohttpSession(api=TelegramAPIServer.from_base(tg_url))
    app = await build_app(settings, bot_session=session, exit_func=exits.append)
    polling = asyncio.create_task(
        app.dp.start_polling(
            app.bot, handle_signals=False, polling_timeout=1, allowed_updates=app.dp.resolve_used_update_types()
        )
    )
    try:
        telegram.push(message=_message(11, "привет", user_id=STRANGER))
        telegram.push(message=_message(12, "купить молоко завтра в 10"))

        # capture: 👀 -> worker: AI -> card + 👌
        card = await _until(lambda: telegram.calls_of("sendMessage"))
        card = card[0]
        assert "Купить молоко" in card["text"]
        assert card["reply_parameters"]["message_id"] == 12
        await _until(lambda: len(telegram.calls_of("setMessageReaction")) >= 2)
        emojis = [c["reaction"][0]["emoji"] for c in telegram.calls_of("setMessageReaction")]
        assert emojis[:2] == ["👀", "👌"]
        [commands] = telegram.calls_of("setMyCommands")
        assert commands["scope"] == {"type": "chat", "chat_id": OWNER}

        # the card has no [✅ Готово]; the same action sits in its ✏️ menu (i:done:<item id>)
        callbacks = [b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row]
        assert not any(cb.startswith(("o:done:", "i:done:")) for cb in callbacks)
        done = next(cb for cb in callbacks if cb.startswith("i:edit:")).replace("i:edit:", "i:done:")
        card_id = telegram.sent_ids[0]
        telegram.push(
            callback_query={
                "id": "q1",
                "from": _user(OWNER),
                "chat_instance": "ci",
                "data": done,
                "message": {
                    **_message(card_id, card["text"]),
                    "from": {"id": BOT_ID, "is_bot": True, "first_name": "D"},
                },
            }
        )
        edit = await _until(lambda: telegram.calls_of("editMessageText"))
        assert int(edit[0]["message_id"]) == card_id and "сделано" in edit[0]["text"]
        assert telegram.calls_of("answerCallbackQuery")[0]["text"] == "✅ Готово"
        await _until(lambda: (tmp_path / "heartbeat").exists())
    finally:
        await app.dp.stop_polling()
        await asyncio.wait_for(polling, 15)
        await app.cleanup()

    assert exits == []
    assert app.lifecycle.tasks == {}
    assert not any(params.get("chat_id") in (STRANGER, str(STRANGER)) for _, params in telegram.calls)
    async with app.ctx.sessionmaker() as s:  # the engine was disposed; a new connection is opened on demand
        [raw] = (await s.scalars(select(RawMessage))).all()
        [item] = (await s.scalars(select(Item))).all()
        [occ] = (await s.scalars(select(Occurrence))).all()
    assert (raw.message_id, raw.status) == (12, "done")
    assert (item.title, item.status, occ.status) == ("Купить молоко", "done", "done")
    await app.ctx.sessionmaker.kw["bind"].dispose()
