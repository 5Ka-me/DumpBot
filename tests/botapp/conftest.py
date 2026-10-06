"""Fixtures for the aiogram application tests (dispatcher, middlewares, routers, lifecycle).

Updates are fed straight into the real dispatcher (``dp.feed_update``); the bot talks to ``FakeTelegram``, an
aiogram session that records every API method and answers with plausible results (no network). DB tests use the
Postgres fixtures from ``tests/conftest.py`` (``TEST_DATABASE_URL``) with a pooled engine.

Test files cannot import each other (``--import-mode=importlib``), so helpers are exposed as fixtures.
Reference time: Tue 2026-10-06 10:00 Europe/Warsaw (UTC+2), frozen via ``dumpbot.clock.utcnow``.
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetMe,
    SendMessage,
    SetMessageReaction,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    PhotoSize,
    Update,
    User,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from dumpbot import clock
from dumpbot.ai.schemas import ExtractedItem, Extraction, When
from dumpbot.bot.app import build_bot, build_dispatcher
from dumpbot.config import Settings
from dumpbot.context import AppContext
from dumpbot.db.models import BotMessage, Job, RawMessage
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import CreatedItem
from dumpbot.services import items

WARSAW = ZoneInfo("Europe/Warsaw")
OWNER = 4242
STRANGER = 777
BOT_ID = 123456
TOKEN = f"{BOT_ID}:TEST-token"
T0 = datetime(2026, 10, 6, 10, 0, tzinfo=WARSAW).astimezone(UTC)


# --- fake Telegram API -------------------------------------------------------------------------------------------


class FakeTelegram(BaseSession):
    """aiogram session that records requests and returns plausible results; ``fail`` queues errors per method."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.errors: dict[type, list[Exception]] = defaultdict(list)
        self._ids = itertools.count(5001)
        self.closed = False

    def fail(self, method: type, *errors: Exception) -> None:
        self.errors[method].extend(errors)

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109  (aiogram's BaseSession signature)
    ) -> Any:
        self.requests.append(method)
        if self.errors[type(method)]:
            raise self.errors[type(method)].pop(0)
        result = self._result(method)
        response = self.check_response(bot, method, 200, json.dumps({"ok": True, "result": result}))
        return response.result

    def _result(self, method: TelegramMethod[Any]) -> Any:
        if isinstance(method, GetMe):
            return {"id": BOT_ID, "is_bot": True, "first_name": "Dump", "username": "dump_test_bot"}
        if isinstance(method, SendMessage):
            return self._message(next(self._ids), method.chat_id, method.text)
        if isinstance(method, EditMessageText):
            return self._message(method.message_id, method.chat_id, method.text)
        return True

    @staticmethod
    def _message(message_id: Any, chat_id: Any, text: str) -> dict[str, Any]:
        return {
            "message_id": message_id,
            "date": int(clock.utcnow().timestamp()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Dump"},
            "text": text,
        }

    async def close(self) -> None:
        self.closed = True

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:  # pragma: no cover
        yield b""

    # -- inspection ---------------------------------------------------------------------------------------------

    def of[M](self, cls: type[M]) -> list[M]:
        return [m for m in self.requests if isinstance(m, cls)]

    @property
    def sent(self) -> list[SendMessage]:
        return self.of(SendMessage)

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]

    @property
    def edits(self) -> list[EditMessageText]:
        return self.of(EditMessageText)

    @property
    def reactions(self) -> list[tuple[int, str]]:
        return [(m.message_id, m.reaction[0].emoji) for m in self.of(SetMessageReaction)]  # type: ignore[union-attr]

    @property
    def toasts(self) -> list[str | None]:
        return [m.text for m in self.of(AnswerCallbackQuery)]

    def clear(self) -> None:
        self.requests.clear()


def buttons(markup: InlineKeyboardMarkup | None) -> list[tuple[str, str]]:
    """(text, callback_data) of every button, row by row."""
    if markup is None:
        return []
    return [(b.text, b.callback_data or "") for row in markup.inline_keyboard for b in row]


# --- frozen clock ------------------------------------------------------------------------------------------------


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    fc = FrozenClock(T0)
    monkeypatch.setattr(clock, "utcnow", fc)
    return fc


# --- DB ----------------------------------------------------------------------------------------------------------


@pytest.fixture
async def sessionmaker(engine: AsyncEngine, db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Pooled variant of the shared fixture (handlers open many short sessions; a new connection is slow).
    ``engine`` is still requested so its teardown truncates the tables."""
    pooled = create_async_engine(db_url, pool_size=5)
    yield async_sessionmaker(pooled, expire_on_commit=False)
    await pooled.dispose()


# --- app objects -------------------------------------------------------------------------------------------------


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"bot_token": TOKEN, "chutes_api_key": "test-key", "owner_id": OWNER}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    return make_settings


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def bot(tg: FakeTelegram) -> Bot:
    return build_bot(make_settings(), session=tg)


@pytest.fixture
def ctx(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession], frozen: FrozenClock) -> AppContext:
    return AppContext(settings=make_settings(), sessionmaker=sessionmaker, bot=bot, ai=None, stt=None)  # type: ignore[arg-type]


@pytest.fixture
def dp(ctx: AppContext) -> Dispatcher:
    return build_dispatcher(ctx)


@pytest.fixture
def feed(dp: Dispatcher, bot: Bot) -> Callable[[Update], Any]:
    async def run(update: Update) -> Any:
        return await dp.feed_update(bot, update)

    return run


# --- update builders ---------------------------------------------------------------------------------------------


def _user(user_id: int, *, is_bot: bool = False) -> User:
    return User(id=user_id, is_bot=is_bot, first_name="Dump" if is_bot else "Дима")


def _chat(chat_id: int, chat_type: str = "private") -> Chat:
    return Chat(id=chat_id, type=chat_type)


class Updates:
    """Builds Update objects with increasing update/message ids."""

    def __init__(self) -> None:
        self._update_ids = itertools.count(1)
        self._message_ids = itertools.count(101)

    def next_message_id(self) -> int:
        return next(self._message_ids)

    def message(
        self,
        text: str | None = None,
        *,
        from_id: int = OWNER,
        chat_id: int | None = None,
        chat_type: str = "private",
        message_id: int | None = None,
        reply_to: int | None = None,
        reply_to_bot: bool = True,
        photo: bool = False,
        caption: str | None = None,
        date: datetime | None = None,
        **extra: Any,
    ) -> Update:
        fields: dict[str, Any] = {
            "message_id": message_id or self.next_message_id(),
            "date": date or T0,
            "chat": _chat(chat_id if chat_id is not None else from_id, chat_type),
            "from_user": _user(from_id),
        }
        if text is not None:
            fields["text"] = text
            if text.startswith("/"):
                length = len(text.split(maxsplit=1)[0])
                fields["entities"] = [MessageEntity(type="bot_command", offset=0, length=length)]
        if photo:
            fields["photo"] = [PhotoSize(file_id="ph1", file_unique_id="u1", width=800, height=600)]
            fields["caption"] = caption
        if reply_to is not None:
            fields["reply_to_message"] = Message(
                message_id=reply_to,
                date=T0 - timedelta(minutes=5),
                chat=fields["chat"],
                from_user=_user(BOT_ID, is_bot=True) if reply_to_bot else _user(from_id),
                text="карточка",
            )
        fields.update(extra)
        return Update(update_id=next(self._update_ids), message=Message(**fields))

    def callback(
        self,
        data: CallbackData | str,
        *,
        message_id: int = 900,
        from_id: int = OWNER,
        text: str = "сообщение бота",
        date: datetime | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Update:
        packed = data if isinstance(data, str) else data.pack()
        message = Message(
            message_id=message_id,
            date=date or T0,
            chat=_chat(from_id),
            from_user=_user(BOT_ID, is_bot=True),
            text=text,
            reply_markup=reply_markup,
        )
        query = CallbackQuery(
            id=f"cb{next(self._update_ids)}",
            from_user=_user(from_id),
            chat_instance="ci",
            data=packed,
            message=message,
        )
        return Update(update_id=next(self._update_ids), callback_query=query)


@pytest.fixture
def updates() -> Updates:
    return Updates()


# --- data helpers ------------------------------------------------------------------------------------------------


class Seed:
    """Creates items (through the real items service), bot-message records and raw messages."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def item(
        self,
        title: str = "Купить наушники",
        *,
        kind: str = "task",
        when: When | None = None,
        rrule: str | None = None,
        lead: list[int] | None = None,
        timed: bool = True,
    ) -> CreatedItem:
        if when is None and timed:
            when = When(text="завтра в 13", day="+1", time="13:00")
        extracted = ExtractedItem(kind=kind, title=title, when=when, rrule=rrule, lead=lead)  # type: ignore[arg-type]
        async with self.sessionmaker() as s:
            [created] = await items.create_items(
                s,
                raw=None,
                extraction=Extraction(items=[extracted]),
                source="text",
                source_text=title,
                source_meta=None,
                urls=[],
                prefs=Prefs(),
                now=T0,
                model=None,
                prompt_version=None,
            )
            await s.commit()
        return created

    async def bot_message(
        self,
        message_id: int,
        *,
        purpose: str = "card",
        item_id: int | None = None,
        occurrence_id: int | None = None,
        raw_message_id: int | None = None,
    ) -> None:
        async with self.sessionmaker() as s:
            s.add(
                BotMessage(
                    chat_id=OWNER,
                    message_id=message_id,
                    purpose=purpose,
                    item_id=item_id,
                    occurrence_id=occurrence_id,
                    raw_message_id=raw_message_id,
                )
            )
            await s.commit()

    async def raw(self, message_id: int = 50, *, text: str = "встреча", status: str = "done", **kw: Any) -> RawMessage:
        async with self.sessionmaker() as s:
            raw = RawMessage(
                chat_id=OWNER,
                message_id=message_id,
                content_type="text",
                text=text,
                tg_date=T0,
                status=status,
                **kw,
            )
            s.add(raw)
            await s.commit()
        return raw

    async def jobs(self) -> list[Job]:
        async with self.sessionmaker() as s:
            return list((await s.scalars(select(Job).order_by(Job.id))).all())

    async def raws(self) -> list[RawMessage]:
        async with self.sessionmaker() as s:
            return list((await s.scalars(select(RawMessage).order_by(RawMessage.id))).all())


@pytest.fixture
def seed(sessionmaker: async_sessionmaker[AsyncSession]) -> Seed:
    return Seed(sessionmaker)


@pytest.fixture
def get_buttons() -> Callable[[InlineKeyboardMarkup | None], list[tuple[str, str]]]:
    return buttons
