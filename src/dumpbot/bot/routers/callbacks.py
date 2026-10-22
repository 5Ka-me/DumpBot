"""Inline buttons of cards, reminders, clarifying questions, edit candidates and digests (``bot.cbdata``).

Every handler follows one pattern: open a session, call the service, commit, answer the callback with a short
toast and edit the message in place with a fresh view. Unknown or stale ids answer "Запись не найдена". The
scheduler is woken after anything that changes reminders, the job worker after anything that queues a job.

Checklists (docs/features/iteration-2.md §C2): [☐ N] / [☑ N] (``SubCb``) toggle a step and redraw the parent's
card; whenever a commit closed a parent by its last step (whatever button did it: a step's reminder too) the
"🎉 Все шаги выполнены" message follows and other cards of the parent are redrawn (``cards.after_steps``).
[🪄 Разбить на шаги] queues job "split" (one at a time per item; nothing with the month's AI budget spent);
[🔗 Похожее] sends the cards of the related items.

Iteration 3 (docs/features/iteration-3.md): [✅ N] / [↩️ N] under /today, /tasks and the digests (``ListCb``) close or
reopen that line without the AI and re-render the list from its buttons (``bot.lists``); [🌙 Вечером] of a reminder
(``OccCb`` ``eve``) moves the item to this evening's ``slot_evening`` like the other snooze buttons; [↩️ Отменить]
(``UndoCb``) of an edit applied by description puts back what it changed (``services.undo``).

Iteration 4 (docs/features/iteration-4.md §1): [🔁 Ещё раз] (``AgainCb``) under "🎤 Не разобрал голосовое…"
transcribes the same voice note again (``processor.retry_unheard``).

``ItemCb`` time/title/tags (text-input dialogs) live in the ``edit`` router and ``SetCb`` in ``settings``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Final

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from dumpbot import clock
from dumpbot.ai.schemas import EditPatch
from dumpbot.bot import cards, lists, sender, views
from dumpbot.bot.cbdata import (
    DIGEST_ACTIONS,
    DIGEST_DAY_FMT,
    LIST_ACTIONS,
    LIST_EVENING,
    LIST_KINDS,
    OCC_ACTIONS,
    SNOOZE_ACTIONS,
    SNOOZE_EVENING,
    SNOOZE_MINUTES,
    STEP_ACTIONS,
    AgainCb,
    AskCb,
    DigestCb,
    ItemCb,
    KindCb,
    ListCb,
    OccCb,
    PickCb,
    SubCb,
    UndoCb,
)
from dumpbot.bot.routers import common
from dumpbot.bot.states import EditStates
from dumpbot.db.models import Occurrence, RawMessage
from dumpbot.domain import timeresolve
from dumpbot.domain.fmt import fmt_dt, plural_ru
from dumpbot.domain.kinds import KIND_LABELS, KINDS
from dumpbot.services import digest, items, processor, query, undo
from dumpbot.workers import jobs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.context import AppContext
    from dumpbot.db.models import Item
    from dumpbot.domain.prefs import Prefs
    from dumpbot.dto import Row

log = logging.getLogger(__name__)

MAX_ASK_OPTIONS: Final = 8
LIST_TOAST_TITLE_MAX: Final = 60
EVENING_PASSED: Final = "🌙 Вечер уже наступил — выбери другое время"
SPLITTING: Final = "Уже разбиваю на шаги…"
UNDO_ALREADY: Final = "Уже отменено"
UNDO_STALE: Final = "🤷 Запись уже изменилась — отменить нельзя"
AGAIN_QUEUED: Final = "🔁 Распознаю ещё раз"
AGAIN_ALREADY: Final = "Уже распознаю ещё раз…"
# Digest buttons sent before the digest day was part of the callback data ("d:tmr").
OLD_DIGEST_DATA: Final = frozenset(f"{DigestCb.__prefix__}:{a}" for a in DIGEST_ACTIONS)


@dataclass
class Outcome:
    """What to show after a button: the new message view (None = leave it) and the toast."""

    view: views.View | None = None
    toast: str | None = None
    wake_scheduler: bool = False
    wake_jobs: bool = False
    # New card messages to send after the edit (🔗 Похожее): (view, item id, occurrence id).
    cards: list[tuple[views.View, int, int | None]] = field(default_factory=list)


Op = Callable[["AsyncSession", "Prefs", datetime], Awaitable[Outcome]]


def make_router() -> Router:
    router = Router(name="callbacks")
    router.callback_query.register(on_occ, OccCb.filter())
    router.callback_query.register(on_item, ItemCb.filter())
    router.callback_query.register(on_kind, KindCb.filter())
    router.callback_query.register(on_ask, AskCb.filter())
    router.callback_query.register(on_pick, PickCb.filter())
    router.callback_query.register(on_step, SubCb.filter())
    router.callback_query.register(on_list, ListCb.filter())
    router.callback_query.register(on_undo, UndoCb.filter())
    router.callback_query.register(on_again, AgainCb.filter())
    router.callback_query.register(on_digest, DigestCb.filter(F.a.in_(DIGEST_ACTIONS)))
    router.callback_query.register(on_old_digest, F.data.in_(OLD_DIGEST_DATA))
    # Buttons of an older version (the removed "⏱ Напомнить за…" menu: ItemCb "lead", "l:…"), garbage data.
    router.callback_query.register(on_stale)
    return router


async def run(callback: CallbackQuery, ctx: AppContext, op: Op) -> None:
    """Run ``op`` in one transaction, then toast + edit in place + new cards + checklist follow-ups + wake-ups."""
    now = clock.utcnow()
    try:
        async with ctx.sessionmaker() as session:
            prefs = await ctx.prefs(session)
            outcome = await op(session, prefs, now)
            await session.commit()
            events = items.take_parent_events(session)
    except items.NotFoundError:
        await common.answer(callback, common.NOT_FOUND)
        return
    await common.answer(callback, outcome.toast)
    message_id = common.message_id_of(callback)
    if outcome.view is not None:
        await common.show(ctx, message_id, outcome.view)
    for (text, markup), item_id, occ_id in outcome.cards:
        await sender.send(ctx, text, markup, reply_to=message_id, purpose="card", item_id=item_id, occurrence_id=occ_id)
    await cards.after_steps(ctx, events, prefs, now, shown=message_id if outcome.view is not None else None)
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
        on_reminder = action in SNOOZE_ACTIONS or await common.purpose_of(session, ctx, message_id) == "reminder"
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
    toast = "✅ Готово" + await _next_time(session, item, occ.id, prefs, now)
    if on_reminder:
        view = views.reminder_done(item, occ, prefs, now)
    else:
        view = await cards.render_card(session, item, occ, [], prefs, now)
    return Outcome(view, toast, wake_scheduler=True)


async def _next_time(session: AsyncSession, item: Item, done_id: int | None, prefs: Prefs, now: datetime) -> str:
    """ " · следующий раз завтра 09:00" once an instance of a series is done ("" for other items)."""
    if not (item.rec_active and item.rrule):
        return ""
    nxt = await items.current_occurrence(session, item.id)
    if nxt is None or nxt.status != "open" or nxt.id == done_id:
        return ""
    return f" · следующий раз {fmt_dt(nxt.due_at, prefs.zone, now=now)}"


async def _undone(
    session: AsyncSession, item: Item, occ: Occurrence, prefs: Prefs, now: datetime, *, on_reminder: bool
) -> Outcome:
    occ = await items.mark_undone(session, occ.id, prefs=prefs, now=now)
    if on_reminder:
        view = views.reminder_message(item, occ, None, prefs, now, parent_title=await _parent_title(session, item))
    else:
        view = await cards.render_card(session, item, occ, await items.pending_reminders(session, occ.id), prefs, now)
    return Outcome(view, "↩️ Не сделано", wake_scheduler=True)


async def _parent_title(session: AsyncSession, item: Item) -> str | None:
    parent = await items.get_item(session, item.parent_id) if item.parent_id is not None else None
    return parent.title if parent is not None else None


async def _snooze(
    session: AsyncSession, item: Item, occ: Occurrence, action: str, prefs: Prefs, now: datetime
) -> Outcome:
    if occ.status == "done":
        return Outcome(views.reminder_done(item, occ, prefs, now), "Уже сделано")
    if occ.status != "open":
        return Outcome(None, "Напоминание уже неактуально")
    if action == SNOOZE_EVENING:
        tonight = timeresolve.tonight(now, prefs)
        if tonight <= now:  # an old reminder pressed after the evening slot: the other buttons instead
            parent_title = await _parent_title(session, item)
            view = views.reminder_message(item, occ, None, prefs, now, parent_title=parent_title)
            return Outcome(view, EVENING_PASSED)
        reminder = await items.snooze(session, occ.id, at=tonight, prefs=prefs, now=now)
        toast = f"🌙 Напомню {fmt_dt(reminder.fire_at, prefs.zone, now=now)}"
    else:
        minutes = SNOOZE_MINUTES[action]
        reminder = await items.snooze(session, occ.id, minutes=minutes, tomorrow=minutes is None, prefs=prefs, now=now)
        toast = f"⏰ Напомню {fmt_dt(reminder.fire_at, prefs.zone, now=now)}"
    # The item's time moved there. No buttons on the snoozed message: the snoozed reminder (of tomorrow's instance,
    # if a series instance was merged into it) comes again with its own.
    return Outcome(views.reminder_snoozed(item, reminder, prefs, now), toast, wake_scheduler=True)


# --- item cards ----------------------------------------------------------------------------------------------------


@dataclass
class ItemCall:
    session: AsyncSession
    ctx: AppContext
    item: Item
    prefs: Prefs
    now: datetime
    message_id: int | None
    callback_id: str = ""


async def _edit_menu(c: ItemCall) -> Outcome:
    return Outcome(views.edit_menu(c.item))


async def _kind_menu(c: ItemCall) -> Outcome:
    return Outcome(views.kind_menu(c.item))


async def _back(c: ItemCall) -> Outcome:
    return Outcome(await common.card_view(c.session, c.item, c.prefs, c.now))


async def _delete(c: ItemCall) -> Outcome:
    await items.delete_item(c.session, c.item.id, c.now)
    return Outcome(views.deleted_card(c.item), "🗑 Удалено", wake_scheduler=True)


async def _restore(c: ItemCall) -> Outcome:
    item = await items.restore_item(c.session, c.item.id, c.now, c.prefs)
    return Outcome(await common.card_view(c.session, item, c.prefs, c.now), "↩️ Восстановлено", wake_scheduler=True)


async def _no_reminders(c: ItemCall) -> Outcome:
    await items.remove_reminders(c.session, c.item.id, prefs=c.prefs, now=c.now)
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
    return Outcome(await cards.patch_card(c.session, result, c.prefs, c.now), "✅ Готово", wake_scheduler=True)


async def _split(c: ItemCall) -> Outcome:
    """[🪄 Разбить на шаги]: job "split" (the AI breaks the task down, then this card turns into the checklist).
    The item row is locked first, so two quick presses (handled concurrently) run one after the other: while a split
    of the item is queued or running another press only says so (a redelivered press is deduplicated by its callback
    id); a failed split can be tried again. An item that has steps already or cannot have them just gets its card
    redrawn; with the month's AI budget spent nothing is queued (the toast says until when)."""
    item = await items.lock_item(c.session, c.item.id)
    extras = await query.card_extras(c.session, item)
    if extras.steps or not items.can_split(item):
        toast = "У задачи уже есть шаги" if extras.steps else "Эту запись не разбить на шаги"
        return Outcome(await common.card_view(c.session, item, c.prefs, c.now), toast)
    if (no_budget := await processor.split_budget_block(c.session, c.prefs, c.now)) is not None:
        return Outcome(None, no_budget)
    if await _split_pending(c.session, c.item.id):
        return Outcome(None, SPLITTING)
    payload = {"item_id": c.item.id, "message_id": c.message_id, "now": c.now.isoformat()}
    key = f"split:{c.callback_id}" if c.callback_id else None
    if await jobs.enqueue(c.session, "split", payload, dedupe_key=key) is None:
        return Outcome(None, SPLITTING)
    return Outcome(None, "🪄 Разбиваю на шаги…", wake_jobs=True)


async def _split_pending(session: AsyncSession, item_id: int) -> bool:
    return await jobs.pending_job(session, "split", item_id) is not None


async def _related(c: ItemCall) -> Outcome:
    """[🔗 Похожее]: the cards of up to ``views.RELATED_SENT`` related items, newest first, as new messages."""
    sent: list[tuple[views.View, int, int | None]] = []
    for ref in (await query.related_refs(c.session, c.item.id))[: views.RELATED_SENT]:
        item = await common.get_item(c.session, ref.item_id)
        occ = await items.current_occurrence(c.session, item.id)
        reminders = await items.pending_reminders(c.session, occ.id) if occ is not None else []
        view = await cards.render_card(c.session, item, occ, reminders, c.prefs, c.now)
        sent.append((view, item.id, occ.id if occ is not None else None))
    if not sent:
        return Outcome(await common.card_view(c.session, c.item, c.prefs, c.now), "Похожих записей нет")
    return Outcome(None, f"🔗 Похожих записей: {len(sent)}", cards=sent)


ITEM_ACTIONS: Final[dict[str, Callable[[ItemCall], Awaitable[Outcome]]]] = {
    "edit": _edit_menu,
    "kind": _kind_menu,
    "back": _back,
    "del": _delete,
    "undel": _restore,
    "norem": _no_reminders,
    "stoprec": _stop_recurrence,
    "done": _item_done,
    "split": _split,
    "rel": _related,
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
        return await handler(ItemCall(session, ctx, item, prefs, now, message_id, callback.id))

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
        view = await cards.patch_card(session, result, prefs, now)
        return Outcome(view, f"🏷 {KIND_LABELS[kind]}", wake_scheduler=True)

    await run(callback, ctx, op)


# --- checklists ---------------------------------------------------------------------------------------------------


async def on_step(callback: CallbackQuery, callback_data: SubCb, ctx: AppContext) -> None:
    """[☐ N] / [☑ N] on a parent's card: the step done / open again (the parent follows), the card redrawn."""
    if callback_data.a not in STEP_ACTIONS:
        await common.answer(callback, common.STALE)
        return

    async def op(session: AsyncSession, prefs: Prefs, now: datetime) -> Outcome:
        step = await common.get_item(session, callback_data.id)
        if step.parent_id is None:
            raise items.NotFoundError(f"item {step.id} is not a step")
        parent = await common.get_item(session, step.parent_id)
        if step.status == "deleted" or parent.status == "deleted":
            return Outcome(await common.card_view(session, parent, prefs, now), common.DELETED)
        await items.toggle_step(session, step.id, done=callback_data.a == "done", prefs=prefs, now=now)
        toast = f"{'☑' if step.status == 'done' else '☐'} {views.clip(step.title, 100)}"
        return Outcome(await common.card_view(session, parent, prefs, now), toast, wake_scheduler=True)

    await run(callback, ctx, op)


# --- numbered lists: [✅ N] / [↩️ N] under /today, /tasks and the digests ----------------------------------------


async def on_list(callback: CallbackQuery, callback_data: ListCb, ctx: AppContext) -> None:
    """[✅ N] closes the line's occurrence (the current instance of a series; a parent together with its open steps;
    the item itself when the line has none), [↩️ N] takes that back — no AI. The list is re-rendered in place from
    its buttons (``bot.lists``): the line struck through with [↩️ N], or open again with [✅ N]. A line already in
    the wanted state, deleted or gone only gets a toast (and the list as it is now)."""
    if callback_data.a not in LIST_ACTIONS or callback_data.k not in LIST_KINDS:
        await common.answer(callback, common.STALE)
        return
    refs = lists.snapshot(callback.message)
    day = _digest_day(callback_data.d)

    async def op(session: AsyncSession, prefs: Prefs, now: datetime) -> Outcome:
        toast, changed, row = await _toggle_line(session, callback_data, prefs, now)
        await session.flush()
        view = await lists.render(session, callback_data.k, day, prefs, now, _repinned(refs, callback_data, row))
        return Outcome(view, toast, wake_scheduler=changed)

    await run(callback, ctx, op)


def _repinned(refs: Sequence[ListCb], cb: ListCb, row: Row | None) -> list[ListCb]:
    """The message's lines with the pressed one pinned to the occurrence the press acted on (a /tasks line of an
    instance left missed: the current one), so that the line shows that occurrence, with [↩️ N] for it."""
    o = (row.occurrence_id or 0) if row is not None else cb.o
    return [ref.model_copy(update={"o": o}) if (ref.s, ref.n) == (cb.s, cb.n) else ref for ref in refs]


async def _toggle_line(session: AsyncSession, cb: ListCb, prefs: Prefs, now: datetime) -> tuple[str, bool, Row | None]:
    """Close / reopen one list line; returns (toast, whether anything changed, the line's row it acted on). [↩️ N]
    of a step that its parent's [✅] closed (done at the parent's ``done_at``) takes that whole [✅] back: the parent
    and the steps closed with it, as the parent's own [↩️] would."""
    [row] = await lists.line_rows(session, cb.k, [cb])
    if row is None:
        return (common.DELETED if await items.get_item(session, cb.i) is not None else common.NOT_FOUND), False, None
    toast, changed = await _toggle_row(session, cb, row, prefs, now)
    return toast, changed, row


async def _toggle_row(session: AsyncSession, cb: ListCb, row: Row, prefs: Prefs, now: datetime) -> tuple[str, bool]:
    title = views.clip(row.title, LIST_TOAST_TITLE_MAX)
    done = views.row_done(row)
    if cb.a == "done":
        if done:
            return f"Уже сделано: «{title}»", False
        if row.occurrence_id is not None:
            await items.mark_done(session, row.occurrence_id, now, prefs)
        else:
            await items.apply_patch(session, row.item_id, EditPatch(action="done"), prefs, now)
        toast = f"✅ Закрыл «{title}»"
        if (steps := row.steps_total - row.steps_done) > 0:  # a parent: its open steps are closed with it
            toast += f" и {steps} {plural_ru(steps, 'шаг', 'шага', 'шагов')}"
        item = await common.get_item(session, row.item_id)
        return toast + await _next_time(session, item, row.occurrence_id, prefs, now), True
    if not done:
        return f"Уже открыто: «{title}»", False
    if (closed_by := await _closed_with_parent(session, row)) is not None:
        parent, steps = closed_by
        await items.apply_patch(session, parent.id, EditPatch(action="undone"), prefs, now)
        parent_title = views.clip(parent.title, LIST_TOAST_TITLE_MAX)
        return f"↩️ Снова открыто: «{parent_title}» и {steps} {plural_ru(steps, 'шаг', 'шага', 'шагов')}", True
    if row.occurrence_id is not None:
        await items.mark_undone(session, row.occurrence_id, prefs=prefs, now=now)
    else:
        await items.apply_patch(session, row.item_id, EditPatch(action="undone"), prefs, now)
    return f"↩️ Снова открыто: «{title}»", True


async def _closed_with_parent(session: AsyncSession, row: Row) -> tuple[Item, int] | None:
    """(the parent, how many of its steps it closed) when the step of ``row`` was closed by its parent's [✅]
    (done at the very moment the parent was), else None."""
    if row.parent_id is None:
        return None
    step = await items.get_item(session, row.item_id)
    parent = await items.get_item(session, row.parent_id)
    if step is None or parent is None or parent.status != "done" or step.status != "done":
        return None
    if parent.done_at is None or step.done_at != parent.done_at:
        return None
    steps = await items.steps_done_at(session, parent.id, parent.done_at)
    return (parent, steps) if steps > 1 else None


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


# --- [↩️ Отменить] of an edit applied by description ----------------------------------------------------------------


async def on_undo(callback: CallbackQuery, callback_data: UndoCb, ctx: AppContext) -> None:
    """[↩️ Отменить] under "✅ Закрыл «…»" / "🕒 Перенёс «…»" / "🗑 Удалил «…»" (docs/features/iteration-3.md §3):
    everything that edit changed goes back exactly as it was (``services.undo``) and the message turns into the
    item's card. A second press (a double tap): "Уже отменено", nothing more. The item changed since (another edit, a
    button, a reminder that went off): nothing is touched, the button goes away and the toast says why."""
    as_is = _without_buttons(callback)

    async def op(session: AsyncSession, prefs: Prefs, now: datetime) -> Outcome:
        outcome = await undo.undo_edit(session, callback_data.raw, callback_data.item, now)
        if outcome == undo.MISSING:
            return Outcome(None, common.STALE)
        if outcome == undo.ALREADY:
            return Outcome(None, UNDO_ALREADY)
        if outcome == undo.STALE:
            return Outcome(as_is, UNDO_STALE)
        item = await common.get_item(session, callback_data.item)
        return Outcome(await common.card_view(session, item, prefs, now), "↩️ Отменено", wake_scheduler=True)

    await run(callback, ctx, op)


# --- [🔁 Ещё раз] of a voice note that was not understood ------------------------------------------------------------


async def on_again(callback: CallbackQuery, callback_data: AgainCb, ctx: AppContext) -> None:
    """[🔁 Ещё раз] under "🎤 Не разобрал голосовое…" (docs/features/iteration-4.md §1): the same file is transcribed
    again (``processor.retry_unheard`` queues its job), the message turns into "🎤 Распознаю ещё раз…" and the voice
    note gets 👀; the outcome comes as a reply to the voice note, like the first time. A second press: a toast only."""
    voice: list[int] = []

    async def op(session: AsyncSession, prefs: Prefs, now: datetime) -> Outcome:
        outcome = await processor.retry_unheard(session, callback_data.raw)
        if outcome == processor.RETRY_MISSING:
            return Outcome(None, common.STALE)
        if outcome == processor.RETRY_ALREADY:
            return Outcome(None, AGAIN_ALREADY)
        raw = await session.get(RawMessage, callback_data.raw)
        if raw is not None and raw.message_id > 0:
            voice.append(raw.message_id)
        return Outcome(views.unheard_again(), AGAIN_QUEUED, wake_jobs=True)

    await run(callback, ctx, op)
    for message_id in voice:
        await sender.react(ctx, message_id, sender.REACT_SEEN)


def _without_buttons(callback: CallbackQuery) -> views.View | None:
    """The pressed message as it is, without its keyboard (None when it is not accessible)."""
    message = callback.message
    if not isinstance(message, Message) or not (message.text or message.caption):
        return None
    return views.fit(message.html_text), None


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
    refs = lists.snapshot(callback.message)

    async def op(session: AsyncSession, prefs: Prefs, now: datetime) -> Outcome:
        day = _digest_day(callback_data.d) or (sent_at or now).astimezone(prefs.zone).date()
        moved = await digest.move_not_done(session, day, prefs, now)
        await session.flush()
        view = await _evening_after_move(session, prefs, day, now, moved, refs)
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
    session: AsyncSession, prefs: Prefs, day: date, now: datetime, moved: Sequence[Occurrence], refs: Sequence[ListCb]
) -> views.View:
    """The digest after the move, re-rendered from its buttons (``refs``) like after [✅ N]: the lines keep their
    numbers (a closed one its [↩️ N]), the moved ones are gone; the count of moved items under it."""
    text, markup = await lists.render(session, LIST_EVENING, day, prefs, now, refs, drop={o.id for o in moved})
    if moved:
        text = views.fit(f"{text}\n\n📅 Перенесено на завтра: {len(moved)}")
    return text, markup


async def on_stale(callback: CallbackQuery) -> None:
    log.info("stale callback data from message %s", common.message_id_of(callback))
    await common.answer(callback, common.STALE)
