"""Slash commands: /start /help /today /tasks /someday /recent /search /ask /recurring /settings /usage /cancel.

This router runs first, so commands (``/cancel`` in particular) work in the middle of an input dialog. Any other
``/word`` gets a short hint instead of being saved as a note.

``/search`` and ``/ask`` picked from the command menu arrive without text: they answer with a prompt that asks for
a reply (``ForceReply``); the capture router turns the reply into the search / the question (``sender.PURPOSE_*``),
so the search text is never saved as a note.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Final

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import BotCommand, ForceReply, Message

from dumpbot import clock
from dumpbot.bot import lists, sender, views
from dumpbot.bot.cbdata import LIST_TASKS, LIST_TODAY
from dumpbot.bot.routers import common
from dumpbot.services import ingest, query

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.context import AppContext
    from dumpbot.domain.prefs import Prefs

log = logging.getLogger(__name__)

# (command, description) for set_my_commands, in menu order.
BOT_COMMANDS: Final[tuple[tuple[str, str], ...]] = (
    ("today", "План на сегодня"),
    ("tasks", "Открытые задачи"),
    ("someday", "Когда-нибудь (без срока)"),
    ("recent", "Последние записи"),
    ("search", "Поиск: /search текст"),
    ("ask", "Вопрос по сохранённому"),
    ("recurring", "Повторяющиеся задачи"),
    ("settings", "Настройки"),
    ("usage", "Расходы на ИИ"),
    ("cancel", "Отменить ввод"),
    ("help", "Справка"),
    ("start", "Начало"),
)

START_TEXT: Final = "👋 Привет! Присылай что угодно — разберу, сохраню и напомню."
SEARCH_HINT: Final = "🔎 Напиши в ответ на это сообщение, что найти. Или сразу: <code>/search наушники</code>"
ASK_HINT: Final = "❓ Напиши вопрос в ответ на это сообщение. Или сразу: <code>/ask что у меня завтра?</code>"
UNKNOWN_COMMAND: Final = "🤷 Не знаю такой команды. Список команд — /help"
CANCELLED: Final = "Отменено."
SOMEDAY_EMPTY: Final = "Пусто. Напиши, например, «когда-нибудь выучить испанский» — запись попадёт сюда."
NOTHING_TO_CANCEL: Final = "Нечего отменять."

LIST_LIMIT: Final = 30
_COMMAND_RE: Final = re.compile(r"^/[A-Za-z0-9_]+(@\w+)?(\s|$)")


def bot_commands() -> list[BotCommand]:
    return [BotCommand(command=c, description=d) for c, d in BOT_COMMANDS]


def looks_like_command(message: Message) -> bool:
    """``/word`` at the start of a text (what Telegram marks as a bot command)."""
    return bool(message.text and _COMMAND_RE.match(message.text))


async def is_command(message: Message) -> bool:
    """Filter form of ``looks_like_command`` (async: aiogram runs sync filters in a thread)."""
    return looks_like_command(message)


def make_router() -> Router:
    router = Router(name="commands")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(cmd_start, Command("start"))
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_today, Command("today"))
    router.message.register(cmd_tasks, Command("tasks"))
    router.message.register(cmd_someday, Command("someday"))
    router.message.register(cmd_recent, Command("recent"))
    router.message.register(cmd_search, Command("search"))
    router.message.register(cmd_ask, Command("ask"))
    router.message.register(cmd_recurring, Command("recurring"))
    router.message.register(cmd_settings, Command("settings"))
    router.message.register(cmd_usage, Command("usage"))
    router.message.register(cmd_unknown, is_command)
    return router


# --- handlers ------------------------------------------------------------------------------------------------------


async def cmd_start(message: Message, ctx: AppContext) -> None:
    await sender.send(ctx, f"{START_TEXT}\n\n{views.help_text()}", purpose="help")


async def cmd_help(message: Message, ctx: AppContext) -> None:
    await sender.send(ctx, views.help_text(), purpose="help")


async def cmd_cancel(message: Message, ctx: AppContext, state: FSMContext) -> None:
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        await session.commit()
    left = await common.restore_prompt(ctx, state, prefs, clock.utcnow())
    await message.answer(CANCELLED if left else NOTHING_TO_CANCEL)


async def cmd_today(message: Message, ctx: AppContext) -> None:
    """Today's plan, its open lines numbered with [✅ N] (``bot.lists``)."""

    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> views.View:
        return await lists.render(session, LIST_TODAY, None, prefs, now)

    await _send_list(ctx, load)


async def cmd_tasks(message: Message, ctx: AppContext) -> None:
    """The open tasks, numbered with [✅ N] (``bot.lists``)."""

    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> views.View:
        return await lists.render(session, LIST_TASKS, None, prefs, now)

    await _send_list(ctx, load)


async def cmd_someday(message: Message, ctx: AppContext) -> None:
    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> views.View:
        rows = await query.someday_items(session, limit=LIST_LIMIT)
        return views.rows_list("🌙 Когда-нибудь", rows, prefs, now, empty=SOMEDAY_EMPTY)

    await _send_list(ctx, load)


async def cmd_recent(message: Message, ctx: AppContext) -> None:
    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> views.View:
        rows = await query.recent(session)
        return views.rows_list("🕘 Последние записи", rows, prefs, now, empty="Пока ничего не сохранено.")

    await _send_list(ctx, load)


async def cmd_search(message: Message, ctx: AppContext) -> None:
    text = common.command_args(message)
    if not text:
        await sender.send(
            ctx, SEARCH_HINT, ForceReply(input_field_placeholder="что найти"), purpose=sender.PURPOSE_SEARCH
        )
        return
    await send_search(ctx, text)


async def send_search(ctx: AppContext, text: str) -> None:
    """Search results for ``text`` (``/search text``, or a reply to the /search prompt)."""

    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> views.View:
        rows = await query.search(session, text)
        return views.rows_list(f"🔎 Поиск: {text}", rows, prefs, now, empty="Ничего не нашёл 🤷")

    await _send_list(ctx, load)


async def cmd_recurring(message: Message, ctx: AppContext) -> None:
    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> views.View:
        return common.recurring_view(await query.recurring(session), prefs, now)

    await _send_list(ctx, load, purpose=common.PURPOSE_RECURRING)


async def cmd_settings(message: Message, ctx: AppContext) -> None:
    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> views.View:
        return views.settings_menu(prefs)

    await _send_list(ctx, load, purpose="settings")


async def cmd_usage(message: Message, ctx: AppContext) -> None:
    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> views.View:
        return views.usage_text(await query.usage_summary(session, prefs, now)), None

    await _send_list(ctx, load, purpose="usage")


async def cmd_ask(message: Message, ctx: AppContext) -> None:
    """Save the question like any captured message, forced into query mode; the processor answers it."""
    question = common.command_args(message)
    if not question:
        await sender.send(ctx, ASK_HINT, ForceReply(input_field_placeholder="вопрос"), purpose=sender.PURPOSE_QUESTION)
        return
    async with ctx.sessionmaker() as session:
        raw, created = await ingest.save_incoming(session, message, force_query=True)
        if created:
            raw.text = question  # without "/ask": the model gets just the question
            await ingest.enqueue_processing(session, raw, clock.utcnow())
        await session.commit()
    if not created:
        log.info("/ask message %s was already saved", message.message_id)
        return
    await sender.react(ctx, message.message_id, sender.REACT_SEEN)
    ctx.jobs_wakeup.set()


async def cmd_unknown(message: Message) -> None:
    await message.answer(UNKNOWN_COMMAND)


# --- rendering -----------------------------------------------------------------------------------------------------


Loader = Callable[["AsyncSession", "Prefs", datetime], Awaitable[views.View]]


async def _send_list(ctx: AppContext, load: Loader, *, purpose: str = "list") -> None:
    """Read in one session (prefs first: the first call creates the settings row), then send the view."""
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        text, markup = await load(session, prefs, now)
        await session.commit()
    await sender.send(ctx, text, markup, purpose=purpose)
