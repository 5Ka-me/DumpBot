"""Slash commands: /start /help /today /tasks /recent /search /ask /recurring /settings /usage /cancel.

This router runs first, so commands (``/cancel`` in particular) work in the middle of an input dialog. Any other
``/word`` gets a short hint instead of being saved as a note.
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
from aiogram.types import BotCommand, Message

from dumpbot import clock
from dumpbot.bot import sender, views
from dumpbot.bot.routers import common
from dumpbot.domain.fmt import fmt_date
from dumpbot.services import ingest, query

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.context import AppContext
    from dumpbot.domain.prefs import Prefs
    from dumpbot.dto import Agenda

log = logging.getLogger(__name__)

# (command, description) for set_my_commands, in menu order.
BOT_COMMANDS: Final[tuple[tuple[str, str], ...]] = (
    ("today", "План на сегодня"),
    ("tasks", "Открытые задачи"),
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
SEARCH_HINT: Final = "🔎 Что искать? Напиши после команды: <code>/search наушники</code>"
ASK_HINT: Final = "❓ Напиши вопрос после команды: <code>/ask что у меня завтра?</code>"
UNKNOWN_COMMAND: Final = "🤷 Не знаю такой команды. Список команд — /help"
CANCELLED: Final = "Отменено."
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
    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> views.View:
        return today_view(await query.agenda(session, prefs, now), prefs, now)

    await _send_list(ctx, load)


async def cmd_tasks(message: Message, ctx: AppContext) -> None:
    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> views.View:
        rows = await query.open_tasks(session, prefs, now, limit=LIST_LIMIT)
        return views.rows_list("📋 Открытые задачи", rows, prefs, now, empty="Открытых задач нет 🎉")

    await _send_list(ctx, load)


async def cmd_recent(message: Message, ctx: AppContext) -> None:
    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> views.View:
        rows = await query.recent(session)
        return views.rows_list("🕘 Последние записи", rows, prefs, now, empty="Пока ничего не сохранено.")

    await _send_list(ctx, load)


async def cmd_search(message: Message, ctx: AppContext) -> None:
    text = common.command_args(message)
    if not text:
        await message.answer(SEARCH_HINT)
        return

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
        await message.answer(ASK_HINT)
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


def today_view(agenda: Agenda, prefs: Prefs, now: datetime) -> views.View:
    """``/today``: today's not-done occurrences, then overdue and done ones (sections only when non-empty)."""
    title = f"📅 Сегодня, {fmt_date(agenda.day)}"
    parts = [views.rows_list(title, agenda.open, prefs, now, empty="На сегодня ничего не запланировано.")[0]]
    if agenda.overdue:
        parts.append(views.rows_list("⚠️ Просрочено", agenda.overdue, prefs, now)[0])
    if agenda.done:
        parts.append(views.rows_list("✅ Сделано", agenda.done, prefs, now)[0])
    return views.fit("\n\n".join(parts)), None


Loader = Callable[["AsyncSession", "Prefs", datetime], Awaitable[views.View]]


async def _send_list(ctx: AppContext, load: Loader, *, purpose: str = "list") -> None:
    """Read in one session (prefs first: the first call creates the settings row), then send the view."""
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        text, markup = await load(session, prefs, now)
        await session.commit()
    await sender.send(ctx, text, markup, purpose=purpose)
