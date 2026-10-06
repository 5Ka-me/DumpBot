"""Settings menu (``SetCb``, keys documented in ``bot.views``) and the typed-value dialog.

Buttons change a preference at once; ``SetCb("ask", field)`` turns the menu into a prompt and the answer
(``common.is_answer``) is validated locally (``HH:MM``, IANA timezone, minutes, dollars). After a change: save
prefs, wake the scheduler, re-render the submenu. Raising the budget also makes jobs deferred for the budget runnable
now. Existing reminders are not re-planned (the menus say so).

A value that does not parse gets one "попробуй ещё раз"; a second one closes the dialog and goes on to capture
(it was a note typed into an abandoned prompt), like anything that is not an answer (a forward, a reply to another
message, a "?" question, a photo, a message sent minutes later).
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from datetime import datetime, time
from typing import TYPE_CHECKING, Any, Final, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from dumpbot import clock
from dumpbot.bot import sender, views
from dumpbot.bot.cbdata import SetCb
from dumpbot.bot.routers import common
from dumpbot.bot.states import SettingsStates, prompt_data
from dumpbot.domain.prefs import STT_MODES, Prefs
from dumpbot.services.settings_repo import save_prefs
from dumpbot.workers import jobs

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.context import AppContext

log = logging.getLogger(__name__)

SAVED: Final = "✅ Сохранено"
INVALID: Final = "Неверное значение"
MAX_LEAD_MINUTES: Final = 10080  # Prefs.lead_minutes upper bound

# What the user is told when a typed value does not parse (by ASK_FIELDS kind).
ERRORS: Final[dict[str, str]] = {
    "time": "Нужно время в формате ЧЧ:ММ, например 09:30.",
    "tz": "Не знаю такой часовой пояс. Нужно название IANA, например Europe/Warsaw.",
    "minutes": f"Нужно целое число минут от 0 до {MAX_LEAD_MINUTES}, например 15.",
    "money": "Нужна сумма в долларах (0 или больше), например 5 или 7.5.",
}
RETRY: Final = " Попробуй ещё раз или /cancel."

_TIME_RE: Final = re.compile(r"^\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*$")


def make_router() -> Router:
    router = Router(name="settings")
    router.callback_query.register(on_setting, SetCb.filter())
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(on_value, SettingsStates.value, F.text)
    router.message.register(on_other, StateFilter(SettingsStates))  # photos, voice…: never a value
    return router


# --- buttons -------------------------------------------------------------------------------------------------------


def _set_tz(p: Prefs, v: str) -> None:
    p.tz = v  # validated by Prefs (IANA name)


def _set_lead(p: Prefs, v: str) -> None:
    p.lead_minutes = int(v)


def _toggle_quiet(p: Prefs, v: str) -> None:
    p.quiet_enabled = not p.quiet_enabled


def _toggle_digest(p: Prefs, v: str) -> None:
    if v not in ("morning", "evening"):
        raise ValueError(f"unknown digest: {v}")
    field = f"digest_{v}_enabled"
    setattr(p, field, not getattr(p, field))


def _set_stt(p: Prefs, v: str) -> None:
    if v not in ("auto", "ru", "en"):
        raise ValueError(f"unknown language: {v}")
    p.stt_language = None if v == "auto" else v  # type: ignore[assignment]


def _set_stt_mode(p: Prefs, v: str) -> None:
    if v not in STT_MODES:
        raise ValueError(f"unknown speech-to-text mode: {v}")
    p.stt_mode = v  # type: ignore[assignment]


def _set_budget(p: Prefs, v: str) -> None:
    p.budget_usd = parse_money(v)


CHANGES: Final[dict[str, Callable[[Prefs, str], None]]] = {
    "tz": _set_tz,
    "lead": _set_lead,
    "quiet_toggle": _toggle_quiet,
    "digest_toggle": _toggle_digest,
    "stt": _set_stt,
    "stt_mode": _set_stt_mode,
    "budget": _set_budget,
}


async def on_setting(callback: CallbackQuery, callback_data: SetCb, ctx: AppContext, state: FSMContext) -> None:
    key, value = callback_data.key, callback_data.v
    message_id = common.message_id_of(callback)
    if key == "ask":
        await _ask(callback, ctx, state, value, message_id)
    elif key in ("menu", "close"):
        await _navigate(callback, ctx, state, key, value, message_id)
    elif key in CHANGES:
        await _change(callback, ctx, key, value, message_id)
    else:
        await common.answer(callback, common.STALE)


async def _navigate(
    callback: CallbackQuery, ctx: AppContext, state: FSMContext, key: str, value: str, message_id: int | None
) -> None:
    """Show a submenu (also [✖️ Отмена] of a prompt) or close the menu (text stays, buttons go)."""
    if await common.is_prompt(state, message_id, SettingsStates):
        await state.clear()  # [✖️ Отмена] of this prompt (a dialog on another message stays open)
    prefs = await _load_prefs(ctx)
    view = _view(ctx, value, prefs) if key == "menu" else (views.settings_menu(prefs)[0], None)
    await common.answer(callback)
    await common.show(ctx, message_id, view)


async def _ask(callback: CallbackQuery, ctx: AppContext, state: FSMContext, field: str, message_id: int | None) -> None:
    if field not in views.ASK_FIELDS:
        await common.answer(callback, common.STALE)
        return
    prefs = await _load_prefs(ctx)
    await common.restore_prompt(ctx, state, prefs, clock.utcnow(), keep=message_id)  # an older dialog elsewhere
    await state.set_state(SettingsStates.value)
    await state.set_data(prompt_data(message_id, field=field))
    await common.answer(callback)
    await common.show(ctx, message_id, views.settings_ask(field, prefs))


async def _change(callback: CallbackQuery, ctx: AppContext, key: str, value: str, message_id: int | None) -> None:
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        new = prefs.model_copy(deep=True)
        try:
            CHANGES[key](new, value)
        except ValueError:  # pydantic's ValidationError included
            await session.commit()
            await common.answer(callback, INVALID)
            return
        budget_raised = await save_changes(session, ctx.owner_id, prefs, new, now)
        await session.commit()
    await common.answer(callback, SAVED)
    await common.show(ctx, message_id, _view(ctx, views.SETTINGS_PARENT.get(key, "main"), new))
    _wake(ctx, budget_raised)


# --- typed values --------------------------------------------------------------------------------------------------


async def on_value(message: Message, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    spec = views.ASK_FIELDS.get(str(data.get("field")))
    if spec is None or not common.is_answer(message, data):
        await _leave(message, ctx, state)
    field = str(data["field"])
    try:
        value = parse_value(spec.kind, message.text or "")
    except ValueError:
        await _invalid(message, ctx, state, data, spec.kind)
        return
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        new = prefs.model_copy(deep=True)
        try:
            setattr(new, field, value)
        except ValueError:
            await session.commit()
            await _invalid(message, ctx, state, data, spec.kind)
            return
        budget_raised = await save_changes(session, ctx.owner_id, prefs, new, now)
        await session.commit()
    await state.clear()
    await common.show(ctx, data.get("prompt_id"), _view(ctx, spec.menu, new))
    await sender.react(ctx, message.message_id, sender.REACT_SAVED)
    _wake(ctx, budget_raised)


async def on_other(message: Message, ctx: AppContext, state: FSMContext) -> None:
    """Not a value (a photo, a voice, a sticker): close the dialog, save the message."""
    await _leave(message, ctx, state)


async def _invalid(message: Message, ctx: AppContext, state: FSMContext, data: dict[str, Any], kind: str) -> None:
    """The first value that does not parse: say what is expected and wait again; the second one is not a typo
    but something else typed into a forgotten prompt, so it is saved as a note instead of being swallowed."""
    if data.get("retried"):
        await _leave(message, ctx, state)
    await state.update_data(retried=True)
    await message.answer(ERRORS[kind] + RETRY)


async def _leave(message: Message, ctx: AppContext, state: FSMContext) -> NoReturn:
    if await common.leave_dialog(ctx, state):
        log.info("message %s is not a settings value; it goes to capture", message.message_id)
    raise SkipHandler


def parse_value(kind: str, text: str) -> Any:
    """Typed value for an ``ASK_FIELDS`` kind (time | tz | minutes | money); ValueError when it does not parse."""
    if kind == "time":
        return parse_time(text)
    if kind == "tz":
        return parse_tz(text)
    if kind == "minutes":
        minutes = int(text.strip())
        if not 0 <= minutes <= MAX_LEAD_MINUTES:
            raise ValueError(f"minutes out of range: {minutes}")
        return minutes
    if kind == "money":
        return parse_money(text)
    raise ValueError(f"unknown field kind: {kind}")


def parse_time(text: str) -> time:
    """``"9:30"`` / ``"09.30"`` -> 09:30."""
    m = _TIME_RE.match(text)
    if m is None:
        raise ValueError("not HH:MM")
    return time(int(m.group(1)), int(m.group(2)))


def parse_tz(text: str) -> str:
    """An IANA zone name that zoneinfo knows, e.g. ``Europe/Warsaw``."""
    name = text.strip()
    if not name or any(c.isspace() for c in name):
        raise ValueError("bad timezone name")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise ValueError("unknown timezone") from e
    return name


def parse_money(text: str) -> float:
    """``"5"`` / ``"7,5"`` / ``"$10"`` -> dollars (finite, >= 0, rounded to cents)."""
    value = float(text.strip().lstrip("$").strip().replace(",", "."))
    if not math.isfinite(value) or value < 0:
        raise ValueError("budget must be a finite number >= 0")
    return round(value, 2)


# --- shared --------------------------------------------------------------------------------------------------------


def _view(ctx: AppContext, name: str, prefs: Prefs) -> views.MenuView:
    """``views.settings_view`` with whether the local Whisper is configured (``LOCAL_STT_URL``)."""
    return views.settings_view(name, prefs, local_stt=bool(ctx.settings.local_stt_url.strip()))


async def _load_prefs(ctx: AppContext) -> Prefs:
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        await session.commit()  # the first call creates the settings row
    return prefs


async def save_changes(session: AsyncSession, owner_id: int, old: Prefs, new: Prefs, now: datetime) -> bool:
    """Save prefs; when the budget went up, make budget-deferred jobs runnable now. True if it went up."""
    await save_prefs(session, owner_id, new)
    if new.budget_usd <= old.budget_usd:
        return False
    count = await jobs.requeue_deferred(session, now)
    log.info("budget raised: %d deferred job(s) requeued", count)
    return True


def _wake(ctx: AppContext, budget_raised: bool) -> None:
    ctx.scheduler_wakeup.set()
    if budget_raised:
        ctx.jobs_wakeup.set()
