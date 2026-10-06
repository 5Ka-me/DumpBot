"""Inline buttons of cards, reminders, clarifying questions, edit candidates and digests (``bot.cbdata``).

Every handler follows one pattern: open a session, call the service, commit, answer the callback with a short
toast and edit the message in place with a fresh view. Unknown or stale ids answer "Запись не найдена". The
scheduler is woken after anything that changes reminders, the job worker after anything that queues a job.

``ItemCb`` time/title/tags (text-input dialogs) live in the ``edit`` router and ``SetCb`` in ``settings``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from dumpbot import clock
from dumpbot.ai.schemas import EditPatch
from dumpbot.bot import views
from dumpbot.bot.cbdata import (
    DIGEST_ACTIONS,
    DIGEST_DAY_FMT,
    OCC_ACTIONS,
    SNOOZE_MINUTES,
    AskCb,
    DigestCb,
    ItemCb,
    KindCb,
    LeadCb,
    OccCb,
    PickCb,
)
from dumpbot.bot.routers import common
from dumpbot.bot.states import EditStates
from dumpbot.db.models import Occurrence, RawMessage
from dumpbot.domain import timeresolve
from dumpbot.domain.fmt import fmt_date, fmt_dt, fmt_duration
from dumpbot.domain.kinds import KIND_LABELS, KINDS
from dumpbot.services import digest, items, query
from dumpbot.workers import jobs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.context import AppContext
    from dumpbot.db.models import Item
    from dumpbot.domain.prefs import Prefs
    from dumpbot.dto import PatchResult

log = logging.getLogger(__name__)

MAX_LEAD: Final = 43200  # same cap as the AI schema / items service
MAX_ASK_OPTIONS: Final = 8
# Digest buttons sent before the digest day was part of the callback data ("d:tmr").
OLD_DIGEST_DATA: Final = frozenset(f"{DigestCb.__prefix__}:{a}" for a in DIGEST_ACTIONS)


@dataclass
class Outcome:
    """What to show after a button: the new message view (None = leave it) and the toast."""

    view: views.View | None = None
    toast: str | None = None
    wake_scheduler: bool = False
    wake_jobs: bool = False


Op = Callable[["AsyncSession", "Prefs", datetime], Awaitable[Outcome]]


def make_router() -> Router:
    router = Router(name="callbacks")
    router.callback_query.register(on_occ, OccCb.filter())
    router.callback_query.register(on_item, ItemCb.filter())
    router.callback_query.register(on_kind, KindCb.filter())
    router.callback_query.register(on_lead, LeadCb.filter())
    router.callback_query.register(on_ask, AskCb.filter())
    router.callback_query.register(on_pick, PickCb.filter())
    router.callback_query.register(on_digest, DigestCb.filter(F.a.in_(DIGEST_ACTIONS)))
    router.callback_query.register(on_old_digest, F.data.in_(OLD_DIGEST_DATA))
    router.callback_query.register(on_stale)  # buttons of an older version, garbage data
    return router


async def run(callback: CallbackQuery, ctx: AppContext, op: Op) -> None:
    """Run ``op`` in one transaction, then toast + edit in place + wake-ups."""
    now = clock.utcnow()
    try:
        async with ctx.sessionmaker() as session:
            prefs = await ctx.prefs(session)
            outcome = await op(session, prefs, now)
            await session.commit()
    except items.NotFoundError:
        await common.answer(callback, common.NOT_FOUND)
        return
    await common.answer(callback, outcome.toast)
    if outcome.view is not None:
        await common.show(ctx, common.message_id_of(callback), outcome.view)
    if outcome.wake_scheduler:
        ctx.scheduler_wakeup.set()
    if outcome.wake_jobs:
        ctx.jobs_wakeup.set()


# --- occurrences: done / undone / snooze (cards and reminders) ---------------------------------------------------


async def on_occ(callback: CallbackQuery, callback_data: OccCb, ctx: AppContext) -> None:
    action = callback_data.a
    if action not in OCC_ACTIONS:
        await common.answer(callback, common.STALE)
        return
    message_id = common.message_id_of(callback)

    async def op(session: AsyncSession, prefs: Prefs, now: datetime) -> Outcome:
        occ = await session.get(Occurrence, callback_data.id)
        if occ is None:
            raise items.NotFoundError(f"occurrence {callback_data.id} not found")
        item = await common.get_item(session, occ.item_id)
        if item.status == "deleted":
            return Outcome(views.deleted_card(item), common.DELETED)
        # Snooze buttons exist only on reminders; done/undone also on cards (told apart by bot_messages).
        on_reminder = action in SNOOZE_MINUTES or await common.purpose_of(session, ctx, message_id) == "reminder"
        if action == "done":
            return await _done(session, item, occ, prefs, now, on_reminder=on_reminder)
        if action == "undone":
            return await _undone(session, item, occ, prefs, now, on_reminder=on_reminder)
        return await _snooze(session, item, occ, action, prefs, now)

    await run(callback, ctx, op)


async def _done(
    session: AsyncSession, item: Item, occ: Occurrence, prefs: Prefs, now: datetime, *, on_reminder: bool
) -> Outcome:
    occ = await items.mark_done(session, occ.id, now, prefs)
    toast = "✅ Готово"
    if item.rec_active and item.rrule:
        nxt = await items.current_occurrence(session, item.id)
        if nxt is not None and nxt.status == "open" and nxt.id != occ.id:
            toast += f" · следующий раз {fmt_dt(nxt.due_at, prefs.zone, now=now)}"
    if on_reminder:
        view = views.reminder_done(item, occ, prefs, now)
    else:
        view = views.item_card(item, occ, [], prefs, now)
    return Outcome(view, toast, wake_scheduler=True)


async def _undone(
    session: AsyncSession, item: Item, occ: Occurrence, prefs: Prefs, now: datetime, *, on_reminder: bool
) -> Outcome:
    occ = await items.mark_undone(session, occ.id, prefs=prefs, now=now)
    if on_reminder:
        view = views.reminder_message(item, occ, None, prefs, now)
    else:
        view = views.item_card(item, occ, await items.pending_reminders(session, occ.id), prefs, now)
    return Outcome(view, "↩️ Не сделано", wake_scheduler=True)


async def _snooze(
    session: AsyncSession, item: Item, occ: Occurrence, action: str, prefs: Prefs, now: datetime
) -> Outcome:
    if occ.status == "done":
        return Outcome(views.reminder_done(item, occ, prefs, now), "Уже сделано")
    if occ.status != "open":
        return Outcome(None, "Напоминание уже неактуально")
    minutes = SNOOZE_MINUTES[action]
    reminder = await items.snooze(session, occ.id, minutes=minutes, tomorrow=minutes is None, prefs=prefs, now=now)
    # A series instance snoozed to tomorrow may have been merged into tomorrow's: [✅ Готово] must point there.
    target = await session.get(Occurrence, reminder.occurrence_id) or occ
    toast = f"⏰ Напомню {fmt_dt(reminder.fire_at, prefs.zone, now=now)}"
    return Outcome(views.reminder_snoozed(item, target, reminder, prefs, now), toast, wake_scheduler=True)


# --- item cards ----------------------------------------------------------------------------------------------------


@dataclass
class ItemCall:
    session: AsyncSession
    ctx: AppContext
    item: Item
    prefs: Prefs
    now: datetime
    message_id: int | None


async def _edit_menu(c: ItemCall) -> Outcome:
    return Outcome(views.edit_menu(c.item))


async def _kind_menu(c: ItemCall) -> Outcome:
    return Outcome(views.kind_menu(c.item))


async def _lead_menu(c: ItemCall) -> Outcome:
    return Outcome(views.lead_menu(c.item))


async def _back(c: ItemCall) -> Outcome:
    return Outcome(await common.card_view(c.session, c.item, c.prefs, c.now))


async def _delete(c: ItemCall) -> Outcome:
    await items.delete_item(c.session, c.item.id, c.now)
    return Outcome(views.deleted_card(c.item), "🗑 Удалено", wake_scheduler=True)


async def _restore(c: ItemCall) -> Outcome:
    item = await items.restore_item(c.session, c.item.id, c.now, c.prefs)
    return Outcome(await common.card_view(c.session, item, c.prefs, c.now), "↩️ Восстановлено", wake_scheduler=True)


async def _no_reminders(c: ItemCall) -> Outcome:
    await items.remove_reminders(c.session, c.item.id)
    view = await common.card_view(c.session, c.item, c.prefs, c.now)
    return Outcome(view, "🔕 Напоминания выключены", wake_scheduler=True)


async def _stop_recurrence(c: ItemCall) -> Outcome:
    await items.stop_recurrence(c.session, c.item.id, c.now)
    if await common.purpose_of(c.session, c.ctx, c.message_id) == common.PURPOSE_RECURRING:
        view = common.recurring_view(await query.recurring(c.session), c.prefs, c.now)
    else:
        view = await common.card_view(c.session, c.item, c.prefs, c.now)
    return Outcome(view, "⏹ Повтор остановлен", wake_scheduler=True)


async def _item_done(c: ItemCall) -> Outcome:
    """[✅ Готово] from the edit menu: closes the current occurrence (or the item when it has none)."""
    result = await items.apply_patch(c.session, c.item.id, EditPatch(action="done"), c.prefs, c.now)
    return Outcome(patch_view(result, c.prefs, c.now), "✅ Готово", wake_scheduler=True)


ITEM_ACTIONS: Final[dict[str, Callable[[ItemCall], Awaitable[Outcome]]]] = {
    "edit": _edit_menu,
    "kind": _kind_menu,
    "lead": _lead_menu,
    "back": _back,
    "del": _delete,
    "undel": _restore,
    "norem": _no_reminders,
    "stoprec": _stop_recurrence,
    "done": _item_done,
}


async def on_item(callback: CallbackQuery, callback_data: ItemCb, ctx: AppContext, state: FSMContext) -> None:
    action = callback_data.a
    handler = ITEM_ACTIONS.get(action)
    if handler is None:
        await common.answer(callback, common.STALE)
        return
    message_id = common.message_id_of(callback)
    if action == "back" and await common.is_prompt(state, message_id, EditStates):
        await state.clear()  # [✖️ Отмена] of this text prompt (a dialog on another card stays open)

    async def op(session: AsyncSession, prefs: Prefs, now: datetime) -> Outcome:
        item = await common.get_item(session, callback_data.id)
        if item.status == "deleted" and action != "undel":
            return Outcome(views.deleted_card(item), common.DELETED)
        return await handler(ItemCall(session, ctx, item, prefs, now, message_id))

    await run(callback, ctx, op)


async def on_kind(callback: CallbackQuery, callback_data: KindCb, ctx: AppContext) -> None:
    kind = callback_data.k
    if kind not in KINDS:
        await common.answer(callback, common.STALE)
        return

    async def op(session: AsyncSession, prefs: Prefs, now: datetime) -> Outcome:
        item = await common.get_item(session, callback_data.id)
        if item.status == "deleted":
            return Outcome(views.deleted_card(item), common.DELETED)
        # apply_patch rather than set_kind: an item that becomes actionable gets its default reminder.
        result = await items.apply_patch(session, item.id, EditPatch(kind=kind), prefs, now)
        return Outcome(patch_view(result, prefs, now), f"🏷 {KIND_LABELS[kind]}", wake_scheduler=True)

    await run(callback, ctx, op)


async def on_lead(callback: CallbackQuery, callback_data: LeadCb, ctx: AppContext) -> None:
    minutes = callback_data.m
    if not 0 <= minutes <= MAX_LEAD:
        await common.answer(callback, common.STALE)
        return

    async def op(session: AsyncSession, prefs: Prefs, now: datetime) -> Outcome:
        item = await common.get_item(session, callback_data.id)
        if item.status == "deleted":
            return Outcome(views.deleted_card(item), common.DELETED)
        result = await items.set_lead(session, item.id, [minutes], prefs, now)
        return Outcome(patch_view(result, prefs, now), _lead_toast(result, minutes, prefs, now), wake_scheduler=True)

    await run(callback, ctx, op)


def _lead_toast(result: PatchResult, minutes: int, prefs: Prefs, now: datetime) -> str:
    """What the reminder will really do: a time the bot chose itself ("завтра" = 10:00, a task without a time)
    takes only whole-day leads, so "за 30 мин" is stored for later but does not move today's reminder."""
    occ = result.occurrence
    if occ is not None and occ.status == "open" and not timeresolve.lead_applies(occ.precision, minutes):
        if not result.reminders:
            return "⏱ У записи нет точного времени — напомню в срок"
        return f"⏱ У записи нет точного времени — напомню {fmt_dt(result.reminders[0].fire_at, prefs.zone, now=now)}"
    return "⏱ Напомню в момент события" if minutes == 0 else f"⏱ Напомню за {fmt_duration(minutes)}"


def patch_view(result: PatchResult, prefs: Prefs, now: datetime) -> views.View:
    if result.deleted or result.item.status == "deleted":
        return views.deleted_card(result.item)
    return views.item_card(result.item, result.occurrence, result.reminders, prefs, now, warnings=result.warnings)


# --- clarifying questions and edit candidates (queue a job) ------------------------------------------------------


async def on_ask(callback: CallbackQuery, callback_data: AskCb, ctx: AppContext) -> None:
    if not 0 <= callback_data.n < MAX_ASK_OPTIONS:
        await common.answer(callback, common.STALE)
        return

    async def op(session: AsyncSession, prefs: Prefs, now: datetime) -> Outcome:
        raw = await session.get(RawMessage, callback_data.raw)
        if raw is None:
            raise items.NotFoundError(f"raw message {callback_data.raw} not found")
        payload = {"raw_id": raw.id, "option": callback_data.n}
        if await jobs.enqueue(session, "clarify", payload, dedupe_key=f"clarify:{raw.id}") is None:
            return Outcome(None, "Ответ уже принят")
        return Outcome(chosen_view(callback), "Принято, уточняю…", wake_jobs=True)

    await run(callback, ctx, op)


async def on_pick(callback: CallbackQuery, callback_data: PickCb, ctx: AppContext) -> None:
    async def op(session: AsyncSession, prefs: Prefs, now: datetime) -> Outcome:
        raw = await session.get(RawMessage, callback_data.raw)
        if raw is None:
            raise items.NotFoundError(f"raw message {callback_data.raw} not found")
        item = await common.get_item(session, callback_data.item)
        if item.status == "deleted":
            return Outcome(None, common.DELETED)
        payload = {"raw_id": raw.id, "item_id": item.id}
        # Not "edit:{raw_id}": that key belongs to the reply-to-card job of the same raw message.
        if await jobs.enqueue(session, "edit", payload, dedupe_key=f"pick:{raw.id}:{item.id}") is None:
            return Outcome(None, "Уже применяю")
        return Outcome(chosen_view(callback), "Применяю правку…", wake_jobs=True)

    await run(callback, ctx, op)


def chosen_view(callback: CallbackQuery) -> views.View | None:
    """The question / candidate list with the pressed option appended and the buttons removed."""
    message = callback.message
    if not isinstance(message, Message) or not (message.text or message.caption):
        return None
    label = _button_text(message, callback.data)
    text = message.html_text
    if label:
        text = f"{text}\n\n✅ {views.q(label)}"
    return views.fit(text), None


def _button_text(message: Message, data: str | None) -> str | None:
    if message.reply_markup is None:
        return None
    for row in message.reply_markup.inline_keyboard:
        for btn in row:
            if btn.callback_data == data:
                return btn.text
    return None


# --- digest: move not-done to tomorrow ---------------------------------------------------------------------------


async def on_digest(callback: CallbackQuery, callback_data: DigestCb, ctx: AppContext) -> None:
    """[📅 Перенести невыполненное на завтра]: what is already due and not done on the digest's day (+ overdue)
    goes to that day + 1. Items still ahead that evening keep their time and reminder.

    The digest's day comes from the button (a digest delayed by quiet hours arrives the next morning), else
    (older buttons) from the day the digest was sent — never from the day the button is pressed.
    """
    sent_at = callback.message.date if isinstance(callback.message, Message) else None

    async def op(session: AsyncSession, prefs: Prefs, now: datetime) -> Outcome:
        day = _digest_day(callback_data.d) or (sent_at or now).astimezone(prefs.zone).date()
        day_of = timeresolve.localize(day, time(12, 0), prefs.zone)  # a moment of that local day
        agenda = await query.agenda(session, prefs, day_of)
        ids = [
            r.occurrence_id
            for r in (*agenda.open, *agenda.overdue)
            if r.occurrence_id is not None and r.due_at is not None and r.due_at <= now
        ]
        moved = await items.move_to_tomorrow(session, ids, prefs, now, day=day + timedelta(days=1)) if ids else []
        await session.flush()
        view = await _evening_after_move(session, prefs, day_of, now, len(moved))
        toast = f"📅 Перенёс на завтра: {len(moved)}" if moved else "Нечего переносить"
        return Outcome(view, toast, wake_scheduler=bool(moved))

    await run(callback, ctx, op)


async def on_old_digest(callback: CallbackQuery, ctx: AppContext) -> None:
    """A digest button sent before the day was in its data ("d:tmr")."""
    await on_digest(callback, DigestCb(a=(callback.data or "").partition(":")[2]), ctx)


def _digest_day(value: str) -> date | None:
    try:
        return datetime.strptime(value, DIGEST_DAY_FMT).date() if value else None
    except ValueError:
        return None


async def _evening_after_move(
    session: AsyncSession, prefs: Prefs, day_of: datetime, now: datetime, moved: int
) -> views.View:
    data = await digest.build_digest(session, "evening", prefs, day_of)
    if data is not None:
        text, markup = views.evening_digest(data, prefs, now)
    else:
        text, markup = f"🌙 <b>Итоги дня</b>, {fmt_date(day_of.astimezone(prefs.zone).date())}", None
    if moved:
        text = views.fit(f"{text}\n\n📅 Перенесено на завтра: {moved}")
    return text, markup


async def on_stale(callback: CallbackQuery) -> None:
    log.info("stale callback data from message %s", common.message_id_of(callback))
    await common.answer(callback, common.STALE)
