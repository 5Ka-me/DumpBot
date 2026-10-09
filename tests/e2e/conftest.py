"""End-to-end harness: the real application against local fakes of the Telegram Bot API and chutes.ai.

The app is built exactly like ``python -m dumpbot`` builds it (``bot.app.build_app`` + ``__main__.poll``): long
polling, OwnerOnly, routers, capture, the job worker, processor, the chutes client, STT, sender and views. Only the
endpoints differ: ``AiohttpSession(api=TelegramAPIServer.from_base(<fake>))`` for Telegram and ``chutes_base_url`` /
``stt_url`` pointing at the fake chutes server, which also plays the local Whisper service (``/local/transcribe``;
``LOCAL_STT_URL`` is set only when a test module overrides the ``local_whisper`` fixture). A test module that
overrides the ``miniapp`` fixture also gets the Mini App's web server (``WEBAPP_ENABLED``, a free port on 127.0.0.1;
``E2E.api`` calls it with initData signed like Telegram does). Nothing leaves 127.0.0.1.

Time is frozen (``dumpbot.clock.utcnow`` is monkeypatched). The job worker runs in the background for real; the
scheduler loop is not started — tests call ``Scheduler.tick(now)`` directly, so reminders and digests fire exactly
when a test says so, without waiting.

Test files cannot import each other (``--import-mode=importlib``): helpers live on the ``E2E`` object.
Needs ``TEST_DATABASE_URL`` (skipped otherwise).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import itertools
import json
import re
import time as _time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from dumpbot import __main__ as entry
from dumpbot import clock
from dumpbot.ai import prompts
from dumpbot.bot.app import App, build_app, default_runners
from dumpbot.config import Settings
from dumpbot.context import AppContext
from dumpbot.db.models import Item, Job, Occurrence, RawMessage, Reminder
from dumpbot.web import auth as web_auth
from dumpbot.workers.scheduler import Scheduler

WARSAW = ZoneInfo("Europe/Warsaw")
OWNER = 4242
STRANGER = 777
BOT_ID = 123456
TOKEN = f"{BOT_ID}:TEST-e2e-token"
MINIAPP_URL = "https://dump.tail1234.ts.net"
PRIMARY = "google/gemma-4-31B-turbo-TEE"
FALLBACK = "Qwen/Qwen3-32B-TEE"
VISION_FALLBACK = "Qwen/Qwen3.6-27B-TEE"
WAIT_S = 20.0


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    """Europe/Warsaw wall time -> aware UTC."""
    return datetime(y, mo, d, h, mi, tzinfo=WARSAW).astimezone(UTC)


T0 = local(2026, 10, 6, 10, 12)  # Tue 2026-10-06 10:12 Europe/Warsaw: the frozen clock's start


async def wait_until(condition: Callable[[], Any], what: str, limit_s: float = WAIT_S) -> Any:
    """Poll ``condition`` (real time) until it returns something truthy."""
    deadline = _time.monotonic() + limit_s
    while _time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for: {what}")


# --- frozen clock ------------------------------------------------------------------------------------------------


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> datetime:
        self.now = now
        return now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


# --- fake Telegram Bot API ---------------------------------------------------------------------------------------

# Telegram's HTML parse mode: these tags only, these named entities only (https://core.telegram.org/bots/api#html-style).
_TG_TAGS = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "tg-spoiler", "span"}
    | {"tg-emoji", "blockquote"}
)
_TG_ENTITIES = frozenset({"lt", "gt", "amp", "quot"})
_TAG_OR_ENTITY = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)>|&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);|[<>&]")
MAX_TEXT_UTF16 = 4096
MAX_CALLBACK_BYTES = 64


def telegram_html_error(text: str) -> str | None:
    """The "can't parse entities" / length error Telegram would give for ``text`` in HTML mode, else None."""
    stack: list[str] = []
    for m in _TAG_OR_ENTITY.finditer(text):
        closing, tag, entity = m.group(1), m.group(2), m.group(4)
        if tag is not None:
            tag = tag.lower()
            if tag not in _TG_TAGS:
                return f'Unsupported start tag "{tag}" at byte offset {m.start()}'
            if not closing:
                stack.append(tag)
            elif not stack or stack.pop() != tag:
                return f'Unmatched end tag at byte offset {m.start()}, expected "</{stack[-1] if stack else ""}>"'
        elif entity is not None:
            if not entity.startswith("#") and entity not in _TG_ENTITIES:
                return f'Unsupported HTML entity "&{entity};"'
        else:
            return f"unescaped {m.group(0)!r} at byte offset {m.start()}"
    if stack:
        return f'Can\'t find end tag corresponding to start tag "{stack[-1]}"'
    plain = _plain(text)
    if not plain.strip():
        return "message text is empty"
    if len(plain.encode("utf-16-le")) // 2 > MAX_TEXT_UTF16:
        return "message is too long"
    return None


def keyboard_error(markup: Any) -> str | None:
    if not markup:
        return None
    for row in markup.get("inline_keyboard", []):
        for b in row:
            if not b.get("text"):
                return "BUTTON_TEXT_EMPTY"
            data = b.get("callback_data")
            if data is not None and not 1 <= len(data.encode()) <= MAX_CALLBACK_BYTES:
                return "BUTTON_DATA_INVALID"
    return None


@dataclass
class Call:
    method: str
    params: dict[str, Any]
    result: Any

    @property
    def message_id(self) -> int:
        return int(self.result["message_id"])

    @property
    def text(self) -> str:
        return str(self.params.get("text") or "")

    @property
    def buttons(self) -> list[tuple[str, str]]:
        markup = self.params.get("reply_markup") or {}
        return [(b["text"], b.get("callback_data", "")) for row in markup.get("inline_keyboard", []) for b in row]

    def button(self, prefix: str) -> str:
        """callback_data of the first button whose data starts with ``prefix``."""
        for _, data in self.buttons:
            if data.startswith(prefix):
                return data
        raise AssertionError(f"no button {prefix!r} in {self.buttons}")

    @property
    def reply_to(self) -> int | None:
        reply = self.params.get("reply_parameters") or {}
        return int(reply["message_id"]) if reply.get("message_id") is not None else None


class FakeTelegram:
    """The Bot API methods the bot uses. Updates are queued by tests and served by ``getUpdates`` (short long
    poll); every other call is recorded with its result. Unknown methods answer 404 and are listed in ``unknown``."""

    def __init__(self, now: Callable[[], datetime]) -> None:
        self._now = now
        self.updates: list[dict[str, Any]] = []
        self.calls: list[Call] = []
        self.unknown: list[str] = []
        self.rejected: list[tuple[str, str]] = []  # (method, Telegram's error) for invalid messages
        self.files: dict[str, tuple[str, bytes]] = {}  # file_id -> (file_path, bytes)
        self.downloads: list[str] = []
        self.bad_tokens = 0
        self._update_ids = itertools.count(1)
        self._bot_message_ids = itertools.count(9001)
        self._user_message_ids = itertools.count(101)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self._api)
        app.router.add_get("/file/bot{token}/{path:.+}", self._file)
        return app

    # -- scripting ------------------------------------------------------------------------------------------------

    def add_file(self, file_id: str, data: bytes, path: str) -> None:
        self.files[file_id] = (path, data)

    def push(self, **update: Any) -> int:
        update_id = next(self._update_ids)
        self.updates.append({"update_id": update_id, **update})
        return update_id

    def push_message(
        self,
        text: str | None = None,
        *,
        user_id: int = OWNER,
        date: datetime | None = None,
        reply_to: Call | None = None,
        **content: Any,
    ) -> int:
        """Queue a private message from ``user_id``; returns its message id."""
        message_id = next(self._user_message_ids)
        message: dict[str, Any] = {
            "message_id": message_id,
            "date": int((date or self._now()).timestamp()),
            "chat": {"id": user_id, "type": "private", "first_name": "Дима"},
            "from": _user(user_id),
            **content,
        }
        if text is not None:
            message["text"] = text
        if reply_to is not None:
            message["reply_to_message"] = self.bot_message(reply_to)
        self.push(message=message)
        return message_id

    def push_callback(self, message: Call, data: str, *, user_id: int = OWNER) -> int:
        """Queue a button press on a message the bot sent (``message`` = its sendMessage call)."""
        return self.push(
            callback_query={
                "id": f"cbq{len(self.updates)}",
                "from": _user(user_id),
                "chat_instance": "ci-e2e",
                "data": data,
                "message": self.bot_message(message),
            }
        )

    @staticmethod
    def bot_message(call: Call) -> dict[str, Any]:
        """The message object of a sent bot message, as Telegram embeds it into replies and callbacks."""
        return call.result

    # -- inspection -----------------------------------------------------------------------------------------------

    def of(self, method: str) -> list[Call]:
        return [c for c in self.calls if c.method == method]

    @property
    def sent(self) -> list[Call]:
        return self.of("sendMessage")

    def replies_to(self, message_id: int) -> list[Call]:
        return [c for c in self.sent if c.reply_to == message_id]

    def reactions(self, message_id: int | None = None) -> list[str]:
        out = []
        for c in self.of("setMessageReaction"):
            if message_id is None or int(c.params["message_id"]) == message_id:
                out.extend(r["emoji"] for r in c.params.get("reaction") or [])
        return out

    def edits_of(self, message_id: int) -> list[Call]:
        return [c for c in self.of("editMessageText") if int(c.params["message_id"]) == message_id]

    def toasts(self) -> list[str | None]:
        return [c.params.get("text") for c in self.of("answerCallbackQuery")]

    def chat_ids(self) -> set[int]:
        return {int(c.params["chat_id"]) for c in self.calls if c.params.get("chat_id") is not None}

    # -- server ---------------------------------------------------------------------------------------------------

    async def _api(self, request: web.Request) -> web.Response:
        if request.match_info["token"] != TOKEN:
            self.bad_tokens += 1
            return web.json_response({"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401)
        method = request.match_info["method"]
        params = {k: _loads(v) for k, v in (await request.post()).items()}
        if method == "getUpdates":
            return _ok(await self._get_updates(params))
        handler = getattr(self, f"_m_{method}", None)
        if handler is None:
            self.unknown.append(method)
            return web.json_response({"ok": False, "error_code": 404, "description": "Not Found"}, status=404)
        error = self._validate(method, params)
        if error is not None:
            self.rejected.append((method, error))
            return web.json_response(
                {"ok": False, "error_code": 400, "description": f"Bad Request: {error}"}, status=400
            )
        result = handler(params)
        if isinstance(result, web.Response):
            return result
        self.calls.append(Call(method, params, result))
        return _ok(result)

    @staticmethod
    def _validate(method: str, params: dict[str, Any]) -> str | None:
        if method in ("sendMessage", "editMessageText"):
            if params.get("parse_mode") != "HTML":
                return f"{method} without parse_mode=HTML"
            error = telegram_html_error(str(params.get("text") or ""))
            if error is not None:
                return f"can't parse entities: {error}"
        if method in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            return keyboard_error(params.get("reply_markup"))
        if method == "answerCallbackQuery" and len(str(params.get("text") or "")) > 200:
            return "MESSAGE_TOO_LONG"
        return None

    async def _get_updates(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        offset = int(params.get("offset") or 0)
        deadline = _time.monotonic() + min(float(params.get("timeout") or 0), 0.5)
        while True:
            ready = [u for u in self.updates if u["update_id"] >= offset]
            if ready or _time.monotonic() >= deadline:
                return ready
            await asyncio.sleep(0.02)

    async def _file(self, request: web.Request) -> web.Response:
        if request.match_info["token"] != TOKEN:
            self.bad_tokens += 1
            return web.Response(status=401)
        path = request.match_info["path"]
        self.downloads.append(path)
        for file_path, data in self.files.values():
            if file_path == path:
                return web.Response(body=data, content_type="application/octet-stream")
        return web.Response(status=404)

    def _message(self, params: dict[str, Any], message_id: int) -> dict[str, Any]:
        message: dict[str, Any] = {
            "message_id": message_id,
            "date": int(self._now().timestamp()),
            "chat": {"id": int(params["chat_id"]), "type": "private", "first_name": "Дима"},
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Dump", "username": "dump_e2e_bot"},
            "text": _plain(str(params.get("text") or "")),
        }
        if params.get("reply_markup"):
            message["reply_markup"] = params["reply_markup"]
        return message

    def _m_getMe(self, params: dict[str, Any]) -> Any:
        return {"id": BOT_ID, "is_bot": True, "first_name": "Dump", "username": "dump_e2e_bot"}

    def _m_sendMessage(self, params: dict[str, Any]) -> Any:
        return self._message(params, next(self._bot_message_ids))

    def _m_editMessageText(self, params: dict[str, Any]) -> Any:
        return self._message(params, int(params["message_id"]))

    def _m_editMessageReplyMarkup(self, params: dict[str, Any]) -> Any:
        return self._message({**params, "text": ""}, int(params["message_id"]))

    def _m_setMessageReaction(self, params: dict[str, Any]) -> Any:
        return True

    def _m_answerCallbackQuery(self, params: dict[str, Any]) -> Any:
        return True

    def _m_setMyCommands(self, params: dict[str, Any]) -> Any:
        return True

    def _m_setChatMenuButton(self, params: dict[str, Any]) -> Any:
        return True

    def _m_deleteWebhook(self, params: dict[str, Any]) -> Any:
        return True

    def _m_getFile(self, params: dict[str, Any]) -> Any:
        file_id = str(params["file_id"])
        if file_id not in self.files:
            return web.json_response(
                {"ok": False, "error_code": 400, "description": "Bad Request: invalid file_id"}, status=400
            )
        path, data = self.files[file_id]
        return {"file_id": file_id, "file_unique_id": f"u-{file_id}", "file_size": len(data), "file_path": path}


def _user(user_id: int) -> dict[str, Any]:
    return {"id": user_id, "is_bot": False, "first_name": "Дима", "language_code": "ru"}


def _ok(result: Any) -> web.Response:
    return web.json_response({"ok": True, "result": result})


def _loads(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "[{":
        return json.loads(value)
    return value


def _plain(html_text: str) -> str:
    """What Telegram shows (and echoes back) for an HTML message: tags removed, entities decoded."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


# --- fake chutes.ai ----------------------------------------------------------------------------------------------


@dataclass
class ChatRequest:
    body: dict[str, Any]

    @property
    def model(self) -> str:
        return str(self.body["model"])

    @property
    def user_parts(self) -> list[dict[str, Any]]:
        """Parts of the prompt's user message (the first one: a validation retry appends more turns)."""
        content = next(m for m in self.body["messages"] if m["role"] == "user")["content"]
        return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)

    @property
    def user_text(self) -> str:
        return "\n".join(p["text"] for p in self.user_parts if p.get("type") == "text")

    @property
    def images(self) -> list[str]:
        return [p["image_url"]["url"] for p in self.user_parts if p.get("type") == "image_url"]

    @property
    def is_repair(self) -> bool:
        """A retry after invalid output: the model's answer and "Your JSON is invalid…" follow the prompt."""
        return len(self.body["messages"]) > 2

    @property
    def is_answer(self) -> bool:
        return self.body["messages"][0]["content"] == prompts.ANSWER_SYSTEM_PROMPT

    @property
    def is_split(self) -> bool:
        """The "split" call (🪄 Разбить на шаги / "разбей на шаги")."""
        return self.body["messages"][0]["content"] == prompts.SPLIT_SYSTEM_PROMPT

    @property
    def task(self) -> dict[str, Any]:
        """The TASK of a split prompt (always its last line)."""
        line = self.line("TASK ")
        return json.loads(line.removeprefix("TASK ")) if line else {}

    @property
    def msg(self) -> str:
        """The MSG block of an extraction prompt (always last)."""
        text = self.user_text
        marker = "\nMSG\n"
        return text.split(marker, 1)[1].strip() if marker in text else ""

    def line(self, prefix: str) -> str | None:
        for row in self.user_text.splitlines():
            if row.startswith(prefix):
                return row
        return None


Script = dict[str, Any] | Callable[[ChatRequest], dict[str, Any]]


@dataclass
class FakeChutes:
    """chat/completions answers with the extraction scripted for the prompt's MSG text (``script``), the steps
    scripted for a split prompt's TASK title (``script_split``), or a prose ``answer`` for answer_query prompts.
    ``outage`` (an HTTP status) makes every chat/STT call fail, ``stt_outage`` only the STT chute; ``down`` (models)
    makes their chat calls answer 503, ``slow`` (model -> seconds) delays their chat answers (until teardown at most).
    ``/local/transcribe`` is the local Whisper service (no key, ``local_transcripts``)."""

    scripts: dict[str, Script] = field(default_factory=dict)
    split_scripts: dict[str, Script] = field(default_factory=dict)
    pages: dict[str, str] = field(default_factory=dict)  # /pages/{name} -> HTML (for link titles)
    page_hits: list[str] = field(default_factory=list)
    transcripts: dict[bytes, str] = field(default_factory=dict)
    answer: str = "Сегодня ничего важного."
    outage: int | None = None
    stt_outage: int | None = None
    down: set[str] = field(default_factory=set)
    slow: dict[str, float] = field(default_factory=dict)
    released: asyncio.Event = field(default_factory=asyncio.Event)  # set on teardown: slow answers end at once
    local_transcripts: dict[bytes, str] = field(default_factory=dict)
    local_requests: list[dict[str, Any]] = field(default_factory=list)
    chat_requests: list[ChatRequest] = field(default_factory=list)
    stt_requests: list[dict[str, Any]] = field(default_factory=list)
    failed_calls: int = 0
    unscripted: list[str] = field(default_factory=list)
    bad_auth: int = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._chat)
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/stt/whisper", self._stt)
        app.router.add_post("/local/transcribe", self._local_stt)
        app.router.add_get("/pages/{name}", self._page)
        return app

    def script(self, msg: str, extraction: Script) -> None:
        """The extraction for prompts whose MSG is ``msg`` (a callable gets the request, e.g. to look at
        CLARIFICATION or REPLYING TO ITEM)."""
        self.scripts[msg.strip()] = extraction

    def script_split(self, title: str, answer: Script) -> None:
        """The answer (``{"steps": [...]}``) for split prompts whose TASK title is ``title``."""
        self.split_scripts[title.strip()] = answer

    def extraction_requests(self) -> list[ChatRequest]:
        return [r for r in self.chat_requests if not r.is_answer and not r.is_split]

    def split_requests(self) -> list[ChatRequest]:
        return [r for r in self.chat_requests if r.is_split]

    def _authorized(self, request: web.Request) -> bool:
        ok = request.headers.get("Authorization") == "Bearer test-chutes-key"
        self.bad_auth += not ok
        return ok

    async def _chat(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=401, text="Invalid token")
        req = ChatRequest(await request.json())
        self.chat_requests.append(req)
        if self.outage is not None or req.model in self.down:
            self.failed_calls += 1
            text = '{"detail":"No instances available (yet) for chute"}'
            return web.Response(status=self.outage or 503, text=text)
        if req.model in self.slow:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.released.wait(), self.slow[req.model])
        if req.is_answer:
            content = self.answer
        elif req.is_split:
            title = str(req.task.get("title") or "")
            split = self.split_scripts.get(title)
            if split is None:
                self.unscripted.append(f"split: {title}")
                split = {"steps": []}
            content = json.dumps(split(req) if callable(split) else split, ensure_ascii=False)
        else:
            script = self.scripts.get(req.msg)
            if script is None:
                self.unscripted.append(req.msg)
                script = {"intent": "save", "items": []}
            extraction = script(req) if callable(script) else script
            content = json.dumps(extraction, ensure_ascii=False)
        return web.json_response(
            {
                "id": f"chatcmpl-{len(self.chat_requests)}",
                "object": "chat.completion",
                "model": req.model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1500, "completion_tokens": 120, "total_tokens": 1620},
            }
        )

    async def _page(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        self.page_hits.append(name)
        if name not in self.pages:
            return web.Response(status=404)
        return web.Response(text=self.pages[name], content_type="text/html", charset="utf-8")

    async def _models(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {"id": PRIMARY, "pricing": {"prompt": 0.12, "completion": 0.37}},
                    {"id": FALLBACK, "pricing": {"prompt": 0.104, "completion": 0.416}},
                    {"id": VISION_FALLBACK, "pricing": {"prompt": 0.30, "completion": 2.00}},
                ],
            }
        )

    async def _stt(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=401, text="Invalid token")
        body = await request.json()
        self.stt_requests.append(body)
        outage = self.outage or self.stt_outage
        if outage is not None:
            self.failed_calls += 1
            return web.Response(status=outage, text="No instances available")
        audio = base64.b64decode(body["audio_b64"])
        return web.json_response({"text": self.transcripts.get(audio, "")})

    async def _local_stt(self, request: web.Request) -> web.Response:
        # The local service has no key: the chutes key must never be sent there.
        self.bad_auth += request.headers.get("Authorization") is not None
        body = await request.json()
        self.local_requests.append(body)
        audio = base64.b64decode(body["audio_b64"])
        return web.json_response(
            {
                "text": self.local_transcripts.get(audio, ""),
                "language": body.get("language") or "ru",
                "duration": 3.0,
                "processing_ms": 850,
                "model": "small",
            }
        )


# --- the running application -------------------------------------------------------------------------------------


def worker_only(ctx: AppContext) -> dict[str, Any]:
    """The production runners minus the scheduler loop (tests drive ``Scheduler.tick`` themselves)."""
    runners = default_runners(ctx)
    return {"jobs": runners["jobs"]}


@dataclass
class E2E:
    app: App
    tg: FakeTelegram
    ai: FakeChutes
    clock: FrozenClock
    scheduler: Scheduler
    exits: list[int]
    ai_url: str = ""
    t0: datetime = T0
    stranger: int = STRANGER

    @property
    def web_url(self) -> str:
        """Base URL of the Mini App's web server (only with the ``miniapp`` fixture on)."""
        server = self.app.lifecycle.web
        assert server is not None and server.bound_port, "the Mini App's web server is not running"
        return f"http://127.0.0.1:{server.bound_port}"

    def init_data(self, user_id: int = OWNER) -> str:
        """initData of the Mini App as Telegram signs it for this bot."""
        fields = {
            "query_id": "AAH0dGVzdA",
            "user": json.dumps({"id": user_id, "first_name": "Дима"}, ensure_ascii=False),
            "auth_date": str(int(self.clock().timestamp())),
        }
        return urlencode({**fields, "hash": web_auth.sign(fields, TOKEN)})

    async def api(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """A request to the Mini App's API as the owner: (status, JSON or bytes)."""
        headers = {"Authorization": f"tma {self.init_data()}"}
        async with (
            aiohttp.ClientSession() as http,
            http.request(method, f"{self.web_url}{path}", json=body, headers=headers) as response,
        ):
            if response.content_type == "application/json":
                return response.status, await response.json()
            return response.status, await response.read()

    @property
    def ctx(self) -> AppContext:
        return self.app.ctx

    async def wait(self, condition: Callable[[], Any], what: str, limit_s: float = WAIT_S) -> Any:
        return await wait_until(condition, what, limit_s)

    async def wait_db(self, condition: Callable[[], Awaitable[Any]], what: str, limit_s: float = WAIT_S) -> Any:
        """Like ``wait`` for an async condition (a DB query)."""
        deadline = _time.monotonic() + limit_s
        while _time.monotonic() < deadline:
            result = await condition()
            if result:
                return result
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for: {what}")

    async def tick(self, now: datetime) -> None:
        """Move the clock to ``now`` and run one scheduler tick there."""
        self.clock.set(now)
        await self.scheduler.tick(now)

    def wake_jobs(self) -> None:
        self.ctx.jobs_wakeup.set()

    async def card_for(self, message_id: int) -> Call:
        """The (first) card the bot sent in reply to ``message_id``."""
        cards = await wait_until(lambda: self.tg.replies_to(message_id), f"a reply to message {message_id}")
        return cards[0]

    async def reaction(self, message_id: int, emoji: str) -> None:
        await wait_until(lambda: emoji in self.tg.reactions(message_id), f"{emoji} on message {message_id}")

    async def idle(self) -> None:
        """Wait until no job is due (queued with run_after <= now, or running)."""

        async def busy() -> bool:
            async with self.ctx.sessionmaker() as s:
                jobs = (await s.scalars(select(Job))).all()
            now = self.clock()
            return any(j.status == "running" or (j.status == "queued" and j.run_after <= now) for j in jobs)

        deadline = _time.monotonic() + WAIT_S
        while await busy():
            if _time.monotonic() > deadline:
                raise AssertionError("jobs still running")
            await asyncio.sleep(0.05)

    # -- DB helpers -----------------------------------------------------------------------------------------------

    async def all(self, model: type) -> list[Any]:
        async with self.ctx.sessionmaker() as s:
            return list((await s.scalars(select(model).order_by(model.id))).all())

    async def raw(self, message_id: int) -> RawMessage:
        async with self.ctx.sessionmaker() as s:
            return (await s.scalars(select(RawMessage).where(RawMessage.message_id == message_id))).one()

    async def item(self, title: str) -> Item:
        async with self.ctx.sessionmaker() as s:
            return (await s.scalars(select(Item).where(Item.title == title))).one()

    async def occurrences(self, item_id: int) -> list[Occurrence]:
        async with self.ctx.sessionmaker() as s:
            stmt = select(Occurrence).where(Occurrence.item_id == item_id).order_by(Occurrence.id)
            return list((await s.scalars(stmt)).all())

    async def reminders(self, item_id: int) -> list[Reminder]:
        async with self.ctx.sessionmaker() as s:
            stmt = (
                select(Reminder)
                .join(Occurrence, Occurrence.id == Reminder.occurrence_id)
                .where(Occurrence.item_id == item_id)
                .order_by(Reminder.id)
            )
            return list((await s.scalars(stmt)).all())


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    fc = FrozenClock(T0)
    monkeypatch.setattr(clock, "utcnow", fc)
    return fc


def make_settings(
    db_url: str,
    tg_url: str,
    ai_url: str,
    heartbeat: Path,
    *,
    local_whisper: bool = False,
    webapp: Path | None = None,
    llm: dict[str, Any] | None = None,
) -> Settings:
    miniapp: dict[str, Any] = {}
    if webapp is not None:
        miniapp = {
            "webapp_enabled": True,
            "webapp_host": "127.0.0.1",
            "webapp_port": 0,  # a free port
            "webapp_url": MINIAPP_URL,
            "webapp_static_dir": str(webapp),
        }
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        bot_token=TOKEN,
        owner_id=OWNER,
        database_url=db_url,
        chutes_api_key="test-chutes-key",
        chutes_base_url=f"{ai_url}/v1",
        llm_model=PRIMARY,
        llm_fallback_model=FALLBACK,
        vision_fallback_model=VISION_FALLBACK,
        stt_url=f"{ai_url}/stt/whisper",
        local_stt_url=f"{ai_url}/local" if local_whisper else "",
        default_tz="Europe/Warsaw",
        heartbeat_file=str(heartbeat),
        scheduler_interval_s=30.0,
        **miniapp,
        **(llm or {}),
    )


@pytest.fixture
def local_whisper() -> bool:
    """Whether LOCAL_STT_URL points at the fake local Whisper; a test module overrides this fixture to turn it on."""
    return False


@pytest.fixture
def llm_settings() -> dict[str, Any]:
    """Extra LLM settings (e.g. a short hedge delay); a test module overrides this fixture to set them."""
    return {}


@pytest.fixture
def miniapp() -> bool:
    """Whether the Mini App's web server runs; a test module overrides this fixture to turn it on."""
    return False


@pytest.fixture
async def e2e(
    engine: AsyncEngine,
    db_url: str,
    frozen: FrozenClock,
    tmp_path: Path,
    local_whisper: bool,
    miniapp: bool,
    llm_settings: dict[str, Any],
) -> AsyncIterator[E2E]:
    """Fake servers up, the app polling them; on teardown polling stops and the app shuts down cleanly.

    ``engine`` (tests/conftest.py) is requested so the tables are truncated after the test.
    """
    tg, ai = FakeTelegram(frozen), FakeChutes()
    tg_server = TestServer(tg.app(), host="127.0.0.1")
    ai_server = TestServer(ai.app(), host="127.0.0.1")
    await tg_server.start_server()
    await ai_server.start_server()
    tg_url = str(tg_server.make_url("")).rstrip("/")
    ai_url = str(ai_server.make_url("")).rstrip("/")

    webapp: Path | None = None
    if miniapp:
        webapp = tmp_path / "webapp"
        webapp.mkdir()
        (webapp / "index.html").write_text("<!doctype html><title>Свалка</title>", encoding="utf-8")
    settings = make_settings(
        db_url, tg_url, ai_url, tmp_path / "heartbeat", local_whisper=local_whisper, webapp=webapp, llm=llm_settings
    )
    exits: list[int] = []
    bot_session = AiohttpSession(api=TelegramAPIServer.from_base(tg_url))
    app = await build_app(settings, bot_session=bot_session, runners=worker_only, exit_func=exits.append)
    polling = asyncio.create_task(entry.poll(app, handle_signals=False, polling_timeout=1))
    env = E2E(app=app, tg=tg, ai=ai, clock=frozen, scheduler=Scheduler(app.ctx), exits=exits, ai_url=ai_url)
    try:
        await wait_until(lambda: tg.of("setMyCommands") or polling.done(), "startup")
        assert not polling.done(), "polling stopped during startup"
        await wait_until(lambda: "jobs" in app.lifecycle.tasks, "background job worker")
        if miniapp:
            await wait_until(lambda: app.lifecycle.web is not None, "the Mini App's web server")
        yield env
    finally:
        ai.released.set()
        try:
            await app.dp.stop_polling()
        except RuntimeError:  # polling already stopped (a failed startup)
            pass
        await asyncio.wait_for(polling, 20)
        await app.cleanup()
        await tg_server.close()
        await ai_server.close()

    # The shutdown must be clean and nothing unexpected may have happened along the way.
    assert exits == [], "a background task crashed"
    assert app.lifecycle.tasks == {}
    assert tg.unknown == [], f"unexpected Bot API methods: {tg.unknown}"
    assert tg.rejected == [], f"Telegram would reject: {tg.rejected}"
    assert ai.unscripted == [], f"unscripted prompts: {ai.unscripted}"
    assert tg.bad_tokens == 0 and ai.bad_auth == 0
