"""One item in the Mini App: its full card, edits, actions and checklist (docs/features/miniapp-api.md).

The same service calls as the bot's buttons (``bot.routers.callbacks``): one transaction per request, then the
checklist follow-ups (``cards.after_steps``: the parent's chat card redrawn, "🎉 Все шаги выполнены") and the wake-ups
of the scheduler / the job worker. A changed item answers with its fresh detail and a short Russian ``message`` (the
bot's toast). "🕒 Время" by text and "🪄 Разбить на шаги" are AI jobs (``app: true``): 202 with the job id, the app
polls ``GET /api/jobs/{id}``. A deleted item takes only ``restore`` (409 ``deleted`` otherwise).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web

from dumpbot import clock
from dumpbot.ai.schemas import MAX_LEAD_MINUTES, PERIOD_RE, EditPatch, When
from dumpbot.bot import cards, views
from dumpbot.db.models import Item
from dumpbot.domain.fmt import fmt_dt
from dumpbot.domain.kinds import KINDS
from dumpbot.services import items, query
from dumpbot.services.processor import CANT_SPLIT_TEXT, HAS_STEPS_TEXT, split_budget_block
from dumpbot.web import serialize
from dumpbot.web.http import ApiError, bad_request, conflict, ctx_of, json_response, path_id, read_json
from dumpbot.workers import jobs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.domain.prefs import Prefs

TITLE_MAX: Final = 200
STEP_TITLE_MAX: Final = 120
DETAILS_MAX: Final = 4000
TAGS_MAX: Final = 20
LEAD_MAX: Final = 3
WHEN_TEXT_MAX: Final = 300
SNOOZE_MAX: Final = 10080  # a week
_DUE_LOCAL_RE: Final = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}))?$")

DELETED: Final = "🗑 Запись удалена — сначала верни её."
NOT_A_STEP: Final = "Это не шаг чек-листа"
NO_STEPS_HERE: Final = "К этой записи нельзя добавить шаги: она сама шаг или повторяется."
CHECKLIST_FULL: Final = f"В чек-листе уже {items.MAX_CHECKLIST} шагов."
NOTHING_TO_SNOOZE: Final = "Нечего откладывать: у записи нет открытого срока."


@dataclass
class Change:
    """What a request changed: the toast, warnings of the new time, which item answers (default: the path's)."""

    message: str | None = None
    warnings: list[str] = field(default_factory=list)
    show: int | None = None


Op = Callable[["AsyncSession", Item, "Prefs", datetime], Awaitable[Change]]


def routes() -> list[web.RouteDef]:
    return [
        web.get("/api/items/{id}", get_item),
        web.patch("/api/items/{id}", patch_item),
        web.post("/api/items/{id}/when", set_when),
        web.post("/api/items/{id}/done", done),
        web.post("/api/items/{id}/undone", undone),
        web.post("/api/items/{id}/norem", no_reminders),
        web.post("/api/items/{id}/delete", delete),
        web.post("/api/items/{id}/restore", restore),
        web.post("/api/items/{id}/stoprec", stop_recurrence),
        web.post("/api/items/{id}/snooze", snooze),
        web.post("/api/items/{id}/split", split),
        web.post("/api/items/{id}/steps", add_step),
        web.patch("/api/steps/{id}", patch_step),
        web.delete("/api/steps/{id}", delete_step),
        web.post("/api/steps/{id}/toggle", toggle_step),
    ]


# --- plumbing ------------------------------------------------------------------------------------------------------


async def _get(session: AsyncSession, item_id: int) -> Item:
    item = await items.get_item(session, item_id)
    if item is None:
        raise items.NotFoundError(f"item {item_id} not found")
    return item


async def _detail(request: web.Request, item_id: int, change: Change | None = None, **extra: Any) -> dict[str, Any]:
    ctx = ctx_of(request)
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        item = await _get(session, item_id)
        warnings = change.warnings if change is not None else ()
        detail = await serialize.item_detail(session, item, prefs, clock.utcnow(), warnings=warnings)
        await session.commit()
    return {"item": detail, "message": change.message if change is not None else None, **extra}


async def _apply(
    request: web.Request, item_id: int, op: Op, *, allow_deleted: bool = False
) -> tuple[Change, items.ParentEvents]:
    """``op`` in one transaction (the item must exist; deleted only with ``allow_deleted``), then the checklist
    follow-ups and the scheduler's wake-up."""
    ctx = ctx_of(request)
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        item = await _get(session, item_id)
        if item.status == "deleted" and not allow_deleted:
            raise conflict("deleted", DELETED)
        change = await op(session, item, prefs, now)
        await session.commit()
        events = items.take_parent_events(session)
    await cards.after_steps(ctx, events, prefs, now)
    ctx.scheduler_wakeup.set()
    return change, events


async def _change(request: web.Request, op: Op, *, allow_deleted: bool = False) -> web.Response:
    item_id = path_id(request)
    change, _ = await _apply(request, item_id, op, allow_deleted=allow_deleted)
    return json_response(await _detail(request, change.show or item_id, change))


# --- read ----------------------------------------------------------------------------------------------------------


async def get_item(request: web.Request) -> web.Response:
    """The full card: fields, occurrence, reminders, checklist, related items, source, picture previews."""
    return json_response((await _detail(request, path_id(request)))["item"])


# --- edits ---------------------------------------------------------------------------------------------------------


async def patch_item(request: web.Request) -> web.Response:
    """``{title?, details?, kind?, tags?, lead?, period?, someday?}`` — validated first, applied together."""
    edits = _item_edits(await read_json(request))

    async def op(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> Change:
        return Change(message="✅ Сохранено", warnings=await _apply_edits(session, item, edits, prefs, now))

    return await _change(request, op)


ITEM_FIELDS: Final = ("title", "details", "tags", "kind", "lead", "period", "someday")


def _item_edits(body: dict[str, Any]) -> dict[str, Any]:
    """The checked values of a PATCH body (only the fields it has); 400 naming the field otherwise."""
    unknown = [key for key in body if key not in ITEM_FIELDS]
    if unknown:
        raise bad_request("Это поле не меняется", unknown[0])
    if not body:
        raise bad_request("Нечего менять")
    if "period" in body and "someday" in body:
        raise bad_request("Либо period, либо someday", "someday")
    edits = dict(body)
    if "title" in body:
        edits["title"] = _title(body["title"], TITLE_MAX)
    details = body.get("details")
    if details is not None and (not isinstance(details, str) or len(details) > DETAILS_MAX):
        raise bad_request(f"Подробности — текст до {DETAILS_MAX} символов", "details")
    tags = body.get("tags", [])
    if not isinstance(tags, list) or len(tags) > TAGS_MAX or not all(isinstance(t, str) for t in tags):
        raise bad_request(f"Теги — список строк, не больше {TAGS_MAX}", "tags")
    if "kind" in body and body["kind"] not in KINDS:
        raise bad_request("Неизвестная категория", "kind")
    if "lead" in body:
        edits["lead"] = _lead(body["lead"])
    period = body.get("period", "someday")
    if not isinstance(period, str) or re.match(PERIOD_RE, period) is None:
        raise bad_request("Срок: this_week, next_week, this_month, next_month, ГГГГ-ММ, сезон, soon, someday", "period")
    if "someday" in body and not isinstance(body["someday"], bool):
        raise bad_request("someday — true или false", "someday")
    return edits


async def _apply_edits(
    session: AsyncSession, item: Item, edits: dict[str, Any], prefs: Prefs, now: datetime
) -> list[str]:
    """The checked PATCH values as service calls: the fields first, then the time. Returns the time's warnings."""
    warnings: list[str] = []
    if "title" in edits:
        await items.set_title(session, item.id, edits["title"])
    if "details" in edits:
        await items.set_details(session, item.id, edits["details"])
    if "tags" in edits:
        await items.set_tags(session, item.id, edits["tags"])
    if "kind" in edits:  # apply_patch, not set_kind: a new task gets its default reminder, like the bot's menu
        warnings += (await items.apply_patch(session, item.id, EditPatch(kind=edits["kind"]), prefs, now)).warnings
    if "lead" in edits:
        await items.set_lead(session, item.id, edits["lead"], prefs, now)
    period = edits.get("period") or ("someday" if edits.get("someday") is True else None)
    if period is not None:
        patch = EditPatch(when=_period_when(period))
        warnings += (await items.apply_patch(session, item.id, patch, prefs, now)).warnings
    elif edits.get("someday") is False:
        await items.clear_someday(session, item.id, prefs, now)
    return warnings


def _title(value: Any, limit: int) -> str:
    title = " ".join(value.split()) if isinstance(value, str) else ""
    if not title or len(title) > limit:
        raise bad_request(f"Название — от 1 до {limit} символов", "title")
    return title


def _lead(value: Any) -> list[int]:
    """Minutes before the time ([] or null: back to the default from the settings)."""
    if value is None:
        return []
    ok = isinstance(value, list) and len(value) <= LEAD_MAX
    ok = ok and all(isinstance(m, int) and not isinstance(m, bool) and 0 <= m <= MAX_LEAD_MINUTES for m in value)
    if not ok:
        raise bad_request(f"Напомнить за — до {LEAD_MAX} значений в минутах, от 0 до {MAX_LEAD_MINUTES}", "lead")
    return list(value)


def _role(item: Item) -> str:
    return str((item.when_spec or {}).get("role") or "event")


def _period_when(period: str) -> When:
    """A chip "на следующей неделе" / "в декабре" / "когда-нибудь" means that span, whatever the item's role was: a
    deadline item does not turn it into "до пт …" (like the bot's reply «перенеси на следующую неделю»)."""
    return When(text=period, period=period)  # type: ignore[arg-type]


def _due_when(item: Item, value: Any) -> When:
    """``due_local`` from a date / time picker: "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM" (local time), no AI."""
    match = _DUE_LOCAL_RE.match(value) if isinstance(value, str) else None
    try:
        if match is None:
            raise ValueError("bad due_local")
        datetime.fromisoformat(value)
        return When(text=value, day=match.group(1), time=match.group(2), role=_role(item))  # type: ignore[arg-type]
    except ValueError:
        raise bad_request("Время — ГГГГ-ММ-ДД или ГГГГ-ММ-ДДTЧЧ:ММ", "due_local") from None


async def set_when(request: web.Request) -> web.Response:
    """``{text}`` -> job "edit_time" through the AI (202 ``{job_id}``), or ``{due_local}`` -> set directly."""
    body = await read_json(request)
    if ("text" in body) == ("due_local" in body):
        raise bad_request("Нужно либо text, либо due_local")
    if "due_local" in body:
        value = body["due_local"]

        async def op(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> Change:
            result = await items.apply_patch(session, item.id, EditPatch(when=_due_when(item, value)), prefs, now)
            return Change(message="🕒 Время изменено", warnings=list(result.warnings))

        return await _change(request, op)
    text = body["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > WHEN_TEXT_MAX:
        raise bad_request(f"Напиши время словами, до {WHEN_TEXT_MAX} символов", "text")
    return await _enqueue(request, "edit_time", {"text": text.strip()})


async def _enqueue(request: web.Request, kind: str, payload: dict[str, Any]) -> web.Response:
    """Job ``kind`` for the path's item (``app: true``): 202 ``{job_id, pending: false}``."""
    item_id = path_id(request)
    ctx = ctx_of(request)
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        item = await _get(session, item_id)
        if item.status == "deleted":
            raise conflict("deleted", DELETED)
        job_id = await jobs.enqueue(session, kind, {"item_id": item_id, **payload, "now": now.isoformat(), "app": True})
        await session.commit()
    ctx.jobs_wakeup.set()
    return json_response({"job_id": job_id, "pending": False}, status=202)


# --- actions -------------------------------------------------------------------------------------------------------


async def done(request: web.Request) -> web.Response:
    """Close the current occurrence (or the item without one); a series moves on to its next time."""

    async def op(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> Change:
        await items.apply_patch(session, item.id, EditPatch(action="done"), prefs, now)
        message = "✅ Готово"
        if item.rec_active and item.rrule:
            nxt = await items.current_occurrence(session, item.id)
            if nxt is not None and nxt.status == "open":
                message += f" · следующий раз {fmt_dt(nxt.due_at, prefs.zone, now=now)}"
        return Change(message=message)

    return await _change(request, op)


async def undone(request: web.Request) -> web.Response:
    async def op(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> Change:
        await items.apply_patch(session, item.id, EditPatch(action="undone"), prefs, now)
        return Change(message="↩️ Не сделано")

    return await _change(request, op)


async def no_reminders(request: web.Request) -> web.Response:
    async def op(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> Change:
        await items.remove_reminders(session, item.id)
        return Change(message="🔕 Напоминания выключены")

    return await _change(request, op)


async def delete(request: web.Request) -> web.Response:
    async def op(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> Change:
        await items.delete_item(session, item.id, now)
        return Change(message="🗑 Удалено")

    return await _change(request, op, allow_deleted=True)


async def restore(request: web.Request) -> web.Response:
    async def op(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> Change:
        await items.restore_item(session, item.id, now, prefs)
        return Change(message="↩️ Восстановлено")

    return await _change(request, op, allow_deleted=True)


async def stop_recurrence(request: web.Request) -> web.Response:
    async def op(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> Change:
        await items.stop_recurrence(session, item.id, now)
        return Change(message="⏹ Повтор остановлен")

    return await _change(request, op)


async def snooze(request: web.Request) -> web.Response:
    """``{minutes}`` (1…10080) or ``{tomorrow: true}``: one more reminder of the current open occurrence."""
    body = await read_json(request)
    minutes, tomorrow = body.get("minutes"), body.get("tomorrow") is True
    valid_minutes = isinstance(minutes, int) and not isinstance(minutes, bool) and 1 <= minutes <= SNOOZE_MAX
    if tomorrow == valid_minutes or (minutes is not None and not valid_minutes):
        raise bad_request(f"Нужно либо minutes (1…{SNOOZE_MAX}), либо tomorrow: true", "minutes")

    async def op(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> Change:
        occ = await items.current_occurrence(session, item.id)
        if occ is None or occ.status != "open" or item.someday:
            raise conflict("nothing_to_snooze", NOTHING_TO_SNOOZE)
        reminder = await items.snooze(
            session, occ.id, minutes=minutes if valid_minutes else None, tomorrow=tomorrow, prefs=prefs, now=now
        )
        return Change(message=f"⏰ Напомню {fmt_dt(reminder.fire_at, prefs.zone, now=now)}")

    return await _change(request, op)


async def split(request: web.Request) -> web.Response:
    """[🪄 Разбить на шаги]: job "split" (202 ``{job_id, pending}``); while one runs for the item its id is returned.
    The item row is locked first, so two quick taps queue one job. With the month's AI budget spent nothing is queued
    (402 ``budget_exhausted``: the job would only wait until next month)."""
    item_id = path_id(request)
    ctx = ctx_of(request)
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        item = await items.lock_item(session, item_id)
        if item.status == "deleted":
            raise conflict("deleted", DELETED)
        if (await query.card_extras(session, item)).steps:
            raise conflict("has_steps", HAS_STEPS_TEXT)
        if item.kind not in views.SPLIT_KINDS or not items.can_split(item):
            raise conflict("cant_split", CANT_SPLIT_TEXT)
        if (no_budget := await split_budget_block(session, await ctx.prefs(session), now)) is not None:
            raise ApiError(402, "budget_exhausted", no_budget)
        pending = await jobs.pending_job(session, "split", item_id)
        payload = {"item_id": item_id, "now": now.isoformat(), "app": True}
        job_id = pending if pending is not None else await jobs.enqueue(session, "split", payload)
        await session.commit()
    if pending is None:
        ctx.jobs_wakeup.set()
    return json_response({"job_id": job_id, "pending": pending is not None}, status=202)


# --- checklist -----------------------------------------------------------------------------------------------------


async def add_step(request: web.Request) -> web.Response:
    """``{title, due_local?}``: one more step at the end of the checklist (201, the parent's detail)."""
    body = await read_json(request)
    title = _title(body.get("title"), STEP_TITLE_MAX)
    due = body.get("due_local")

    async def op(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> Change:
        if item.parent_id is not None or item.rec_active:
            raise conflict("cant_have_steps", NO_STEPS_HERE)
        when = _due_when(item, due) if due is not None else None
        try:
            created = await items.add_step(session, item.id, title, prefs, now, when=when)
        except ValueError:
            raise conflict("checklist_full", CHECKLIST_FULL) from None
        return Change(message="➕ Шаг добавлен", warnings=list(created.resolution.warnings))

    item_id = path_id(request)
    change, _ = await _apply(request, item_id, op)
    return json_response(await _detail(request, item_id, change), status=201)


async def _step_change(request: web.Request, op: Op) -> web.Response:
    """``op`` on the step of the path; the answer is its parent's detail (``closed``: the step closed the parent)."""
    step_id = path_id(request)
    parents: list[Item] = []

    async def guarded(session: AsyncSession, step: Item, prefs: Prefs, now: datetime) -> Change:
        if step.parent_id is None:
            raise ApiError(404, "not_a_step", NOT_A_STEP)
        parent = await _get(session, step.parent_id)
        if parent.status == "deleted":
            raise conflict("deleted", DELETED)
        parents.append(parent)
        change = await op(session, step, prefs, now)
        change.show = parent.id
        return change

    change, events = await _apply(request, step_id, guarded)
    parent = parents[0]
    closed = parent.id in events.closed
    if closed:  # the bot's "🎉 Все шаги выполнены" (also sent to the chat by cards.after_steps)
        change.message = f"🎉 Все шаги выполнены — «{parent.title}» закрыт"
    return json_response(await _detail(request, parent.id, change, closed=closed))


async def patch_step(request: web.Request) -> web.Response:
    """``{title?, due_local?}``: rename the step, give it a time of its own (``due_local``) or take it (null)."""
    body = await read_json(request)
    unknown = [key for key in body if key not in ("title", "due_local")]
    if unknown or not body:
        raise bad_request("Меняются только title и due_local", unknown[0] if unknown else None)
    title = _title(body["title"], STEP_TITLE_MAX) if "title" in body else None

    async def op(session: AsyncSession, step: Item, prefs: Prefs, now: datetime) -> Change:
        change = Change(message="✅ Сохранено")
        if title is not None:
            await items.set_title(session, step.id, title)
        if "due_local" in body and body["due_local"] is None:
            await items.clear_time(session, step.id)
        elif "due_local" in body:
            patch = EditPatch(when=_due_when(step, body["due_local"]))
            change.warnings += (await items.apply_patch(session, step.id, patch, prefs, now)).warnings
        return change

    return await _step_change(request, op)


async def delete_step(request: web.Request) -> web.Response:
    async def op(session: AsyncSession, step: Item, prefs: Prefs, now: datetime) -> Change:
        await items.delete_item(session, step.id, now)
        return Change(message="🗑 Шаг удалён")

    return await _step_change(request, op)


async def toggle_step(request: web.Request) -> web.Response:
    """``{done?}``: ☐ / ☑ (without ``done`` the state flips). The last open step closes the parent (``closed``)."""
    body = await read_json(request)
    wanted = body.get("done")
    if wanted is not None and not isinstance(wanted, bool):
        raise bad_request("done — true или false", "done")

    async def op(session: AsyncSession, step: Item, prefs: Prefs, now: datetime) -> Change:
        done = wanted if wanted is not None else step.status != "done"
        await items.toggle_step(session, step.id, done=done, prefs=prefs, now=now)
        return Change(message=f"{'☑' if step.status == 'done' else '☐'} {views.clip(step.title, 100)}")

    return await _step_change(request, op)
