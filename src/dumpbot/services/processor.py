"""Job handlers (architecture.md §4.14): AI processing of saved messages, edits and clarifications.

Pipeline of a message (an album is processed as one message, attached to its first part):
1. load the raw message(s) (done/ignored -> nothing to do) and prefs; "now" is the message date;
2. media without any text (stickers, videos, video notes…) -> one "other" item without AI, reaction 👌;
3. content: voice notes and audio files (forwarded ones too) up to ``stt_max_duration_s`` -> download -> STT routed
   by ``Prefs.stt_mode`` (chutes / the local Whisper; the transcript is committed at once, so a retry does not pay
   again); pictures up to ``MAX_IMAGE_BYTES`` (``MAX_REQUEST_IMAGE_BYTES`` together) -> download (the bytes are never
   stored); links -> page titles (no AI); nothing to read (a silent voice note) -> saved without AI;
4. budget, checked right before each paid call (chutes STT, ``extract()``), never before the free local Whisper:
   exhausted -> ✍, raw ``deferred``, notice once a month, ``RetryLater(next month after its quiet hours,
   count_attempt=False)``; >= 80 % -> notice once a month;
5. ``extract()`` -> save (items, cards, 👌 / clarifying question 🤔), query (list or prose answer) or edit;
6. raw ``done``, scheduler woken.

Errors: ``AIUnavailable`` -> ✍ once (raw ``deferred``) and re-raised so the queue backs off; ``AIConfigError`` ->
notice once a day + ``RetryLater(1 h, count_attempt=False)``; ``AIBadResponse`` (the models answered, nothing usable:
the same request gives the same result) -> no retries: the message is saved without AI (its text as a note), an
edit is reported as failed, 😢; Telegram download errors -> ``RetryLater(5 min)``. The fallback model standing in
for a missing (404) primary -> notice once a day.

Related items and checklists (docs/features/iteration-2.md §C): before ``extract()`` up to 5 earlier open items sharing
a subject word with the message (not a reminder / time word or a common verb) go into the prompt (RELATED CANDIDATES),
and only their ids are kept from the model's ``related``, and only when the titles share a subject word too;
"переезд, распиши по шагам" comes back with ``steps``; "разбей на шаги" (a reply, or by target) and the card's
[🪄 Разбить на шаги] (job "split") run the separate "split" call and show the checklist. Edits that close a parent
by its last step are followed by "🎉 Все шаги выполнены" (``cards.after_steps``).

Edits by description (docs/features/iteration-3.md §3): "закрой задачу про документацию" with one exact match among
the open items is applied at once and answered with what changed and [↩️ Отменить] (``services.undo`` keeps the
difference with the raw message); with several the owner picks one, and the pick gets the edit already read from the
message (``EDIT_KEY``, no second AI call). "Exact" is about the item's own words (title / details), never the message
it came from, so the items of one message are told apart; new items said with the edit are saved too.

Voice notes (§6): every reply to one shows what was heard, "🎤 «…»" (cards: ``CardExtras.heard``; answers, questions,
candidate lists and edits: the line on top). One that gave only notes / ideas (otherwise just 👌) gets the short
"🎤 «…» → 💡 Идея «…»" with [✏️ Изменить] [🗑 Не то — удалить] (a message per item), so that a garbled transcript
shows at once.

Data changes are committed before their cards are sent (a lost card never duplicates items); replies that change
nothing (answers, candidate lists) are sent before the raw message is marked done, so a Telegram error retries them.
A message saved as items, or a reply applied to its card, is marked done after its card (docs/features/iteration-4.md
§7: one database wait less before it); a run of the job after a crash in between finds the items (the once-key of the
edit) and only marks it done.
Everything sent in quiet hours is silent (a deferred message can be processed at night); the final reaction goes on
every part of an album.

The Mini App (docs/features/iteration-2.md §D): a message from the app (``RawMessage.source == "app"``) goes through
the same pipeline, but nothing about it is sent to the chat (no cards, reactions, questions, answers): its note, the
answer to a question and the edited items are kept in ``raw_json[APP_KEY]`` (the question and the candidates under
the usual keys) for ``GET /api/inbox/{raw_id}``. Jobs "edit_time" and "split" with ``app: true`` in the payload stay
out of the chat too and return their outcome (``{"ok", "reason", "message", …}``), which the worker keeps in
``Job.result``. ``ask_question`` answers ``POST /api/ask`` (the "?" path without a saved message).
Message text, transcripts and OCR are never logged at INFO or above.

Every edit applied from a message (a reply to a card or reminder, an edit by description, the "🕒 Время" answer) goes
into the events journal as ``edit`` (``services.events.edit_entry``, ``"move": true`` when it moved the item): the
stale-items check counts it as an action and its moves (docs/features/iteration-4.md §4).
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any, Final

import aiohttp
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup
from pydantic import ValidationError
from sqlalchemy import Select, case, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from dumpbot import clock
from dumpbot.ai.client import AIBadResponse, AIConfigError, AIUnavailable
from dumpbot.ai.extract import ExtractInput, ExtractOutput, SplitInput, SplitOutput, answer_query, extract, split_steps
from dumpbot.ai.linkinfo import fetch_titles
from dumpbot.ai.schemas import Ask, EditPatch, ExtractedItem, Extraction, QuerySpec, When
from dumpbot.ai.stt import GarbageTranscript
from dumpbot.bot import cards, sender, views
from dumpbot.context import AppContext
from dumpbot.db import meter
from dumpbot.db.models import Item, Job, OnceKey, RawMessage
from dumpbot.domain import phrases, quiet, recurrence, timeresolve
from dumpbot.domain.fmt import fmt_date
from dumpbot.domain.kinds import ACTIONABLE, SourceKind
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import BudgetState, CardExtras, CreatedItem, PatchResult, Row
from dumpbot.services import events as journal
from dumpbot.services import items as items_svc
from dumpbot.services import query, undo, usage
from dumpbot.services.ingest import APP_KEY, APP_SOURCE, ASK_KEY, CANDIDATES_KEY, EDIT_KEY, UNHEARD_KEY
from dumpbot.workers.jobs import BUDGET_REASON, Handler, RetryLater, describe_error, enqueue, try_once
from dumpbot.workers.jobs import WATCH_NAME as JOBS_WATCH

__all__ = [
    "HANDLERS",
    "QueryAnswer",
    "ask_question",
    "handle_album",
    "handle_clarify",
    "handle_edit",
    "handle_edit_time",
    "handle_message",
    "handle_split",
    "on_dead",
    "retry_unheard",
    "run_question",
    "split_budget_block",
]

log = logging.getLogger(__name__)

DOWNLOAD_RETRY: Final = timedelta(minutes=5)
CONFIG_RETRY: Final = timedelta(hours=1)
SPLIT_NOW_MAX_AGE: Final = timedelta(hours=1)  # an older press time of a split job is replaced by the run time
BUDGET_WARN_RATIO: Final = 0.8
MAX_IMAGES: Final = 4
# Bigger pictures are not sent to the model: the upload counts against llm_timeout_s, and the models downscale anyway.
MAX_IMAGE_BYTES: Final = 8 * 1024 * 1024
MAX_REQUEST_IMAGE_BYTES: Final = 12 * 1024 * 1024  # all pictures of one message / album together
TITLE_MAX: Final = 120  # ExtractedItem.title limit
VOICE_TYPES: Final = frozenset({"voice", "audio"})
IMAGE_TYPES: Final = frozenset({"photo", "image_document"})
# Formats vision models take; other pictures (HEIC, SVG, TIFF…) are saved without being sent to the model.
VISION_MIME: Final[dict[str, str]] = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/gif": "image/gif",
}
_FINISHED: Final = ("done", "ignored")
# Edits by target that a message from the Mini App (no [↩️ Отменить] there) applies only after the owner picked the
# item, even when the search found one.
CONFIRM_ACTIONS: Final = frozenset({"delete", "done", "no_reminder", "stop_recurrence"})
EXACT_POOL: Final = 20  # exact matches looked at for the single open one (the candidate list shows the first 5)

NOT_FOUND_TEXT: Final = "🤷 Не нашёл, что изменить — сохранил как запись."
TIME_NOT_UNDERSTOOD_TEXT: Final = "🤷 Не понял время — напиши, например, «завтра в 15:00»."
ITEM_GONE_TEXT: Final = "🤷 Не нашёл эту запись — возможно, её уже нет."
NOTHING_FOUND: Final = "Ничего не нашёл 🤷"
FAILED_MESSAGE_TEXT: Final = "😢 Не смог обработать сообщение — оно сохранено, но разобрать его не получилось."
FAILED_EDIT_TEXT: Final = "😢 Не смог применить изменение — попробуй ещё раз или через «✏️ Изменить»."
FAILED_SPLIT_TEXT: Final = "😢 Не смог разбить на шаги — попробуй ещё раз."
SPLIT_WAIT_TEXT: Final = "✍ ИИ сейчас недоступен — разобью на шаги, как только получится."
NO_STEPS_TEXT: Final = "🤷 Не вижу, на какие шаги это разбить."
HAS_STEPS_TEXT: Final = "📋 У задачи уже есть шаги."
CANT_SPLIT_TEXT: Final = "🤷 Эту запись не разбить на шаги: она уже закрыта, повторяется или сама — шаг."
# Snapshot fields the "split" prompt gets (the task, not its bookkeeping).
SPLIT_TASK_FIELDS: Final = ("kind", "title", "details", "tags", "due_local", "period", "someday")

_MEDIA_TITLES: Final[dict[str, str]] = {
    "video": "Видео",
    "video_note": "Кружочек",
    "animation": "GIF",
    "voice": "Голосовое сообщение",
    "photo": "Фото",
    "image_document": "Картинка",
}

View = tuple[str, InlineKeyboardMarkup | None]


@dataclass
class _Content:
    """What the model gets for one message (or album)."""

    text: str
    source: SourceKind
    urls: list[str]
    images: list[tuple[bytes, str]] = field(default_factory=list)
    forward_origin: dict[str, Any] | None = None
    link_titles: dict[str, str] = field(default_factory=dict)
    force_query: bool = False
    unheard: bool = False  # a voice note's transcript was not speech (twice): nothing is saved, "Не разобрал"
    # Transcripts / link titles being stored meanwhile (``_gather`` with ``meanwhile``): awaited by ``_stored``.
    writes: list[asyncio.Task[None]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text.strip() and not self.images


# --- handlers ---------------------------------------------------------------------------------------------------


async def handle_message(ctx: AppContext, payload: dict[str, Any]) -> None:
    """Job "message" ``{raw_id}``: process one saved message (the one the capture router has just handed over, or
    loaded)."""
    raw_id = int(payload["raw_id"])
    fresh = ctx.take_fresh(raw_id)
    if fresh is not None:  # saved a moment ago: new, nothing saved from it yet
        raws, saved, prefs = [fresh], False, await _prefs(ctx)
    else:
        stmt = select(RawMessage).where(RawMessage.id == raw_id)
        (raws, saved), prefs = await _together(_load_message(ctx, stmt), _prefs(ctx))
    if not raws or raws[0].status in _FINISHED:
        return
    if saved:
        await _done(ctx, raws, None)
        return
    await _process(ctx, raws, prefs=prefs)


async def handle_album(ctx: AppContext, payload: dict[str, Any]) -> None:
    """Job "album" ``{media_group_id, chat_id?}``: all unfinished parts as one message (captions + up to 4
    pictures); the items belong to the first part, every part ends up done."""
    stmt = select(RawMessage).where(
        RawMessage.media_group_id == str(payload["media_group_id"]), RawMessage.status.not_in(_FINISHED)
    )
    if payload.get("chat_id") is not None:
        stmt = stmt.where(RawMessage.chat_id == int(payload["chat_id"]))
    (raws, saved), prefs = await _together(_load_message(ctx, stmt.order_by(RawMessage.message_id)), _prefs(ctx))
    if saved:
        await _done(ctx, raws, None)
    elif raws:
        await _process(ctx, raws, prefs=prefs)


async def handle_clarify(ctx: AppContext, payload: dict[str, Any]) -> None:
    """Job "clarify" ``{raw_id, option}``: extract again with the chosen answer and replace the raw's items.

    ``option`` is the option text, or its index (int) in the stored question; ``n`` (int) is accepted too.
    Applied at most once per raw message: a once-key is committed together with the new items, so a re-run of
    the job (a restart after that commit) neither pays the AI again nor replaces the items a second time.
    """
    async with ctx.autocommit() as session:
        raw = await session.get(RawMessage, int(payload["raw_id"]))
        if raw is None or await session.get(OnceKey, _clarified_key(raw.id)) is not None:
            return
        raws = [raw]
        if raw.media_group_id:
            stmt = select(RawMessage).where(
                RawMessage.media_group_id == raw.media_group_id, RawMessage.chat_id == raw.chat_id
            )
            raws = list((await session.scalars(stmt)).all())
            raws.sort(key=lambda r: (r.id != raw.id, r.message_id))  # the asked-about raw first
    option = _option_text(raw, payload)
    if not option:
        log.warning("clarify for raw %s: no usable option", raw.id)
        return
    await _process(ctx, raws, clarification=option)


async def handle_edit(ctx: AppContext, payload: dict[str, Any]) -> None:
    """Job "edit" ``{raw_id, item_id}``: a reply to an item card, or the item picked among edit candidates.

    A pick from the list of an edit by description gets the edit already read from that message (``EDIT_KEY``):
    applied without the AI, answered like an edit applied at once (with [↩️ Отменить]). Otherwise (a reply to a card,
    a pick after a reply to the owner's own message) the message is read again with the item as context.
    The patch is applied at most once per (raw, item): a once-key is committed together with it.
    """
    raw_id, item_id = int(payload["raw_id"]), int(payload["item_id"])
    key = f"edit:{raw_id}:{item_id}"
    fresh = ctx.take_fresh(raw_id)
    if fresh is not None:  # the reply the capture router saved a moment ago: nothing applied from it yet
        raw, applied, prefs = fresh, False, await _prefs(ctx)
    else:
        (raw, applied), prefs = await _together(_load_edit(ctx, raw_id, key), _prefs(ctx))
    if raw is None or raw.status == "ignored":
        return
    if applied:
        if raw.status not in _FINISHED:  # the job stopped between the edit and marking the reply done (below)
            await _done(ctx, [raw], None)
        return
    if raw.status == "done" and raw.reply_item_id == item_id:  # answered as a "?" query on an earlier run
        return
    picked = _picked_patch(raw, item_id)
    if picked is not None:
        await _apply_picked(ctx, raw, item_id, picked, key, prefs)
        return
    now = _msg_now(raw)
    out: ExtractOutput | None = None
    try:
        # A picked candidate re-reads an already processed message: reuse its OCR text instead of the pictures.
        content = await _gather(ctx, [raw], prefs, with_images=not raw.image_text)
        if content.unheard:  # a voice reply that is not speech: no edit, "Не разобрал" with [🔁 Ещё раз]
            await _unheard(ctx, [raw], prefs, {"kind": "edit", "payload": {"raw_id": raw.id, "item_id": item_id}})
            return
        # «Закрывай вообще», «хватит повторять» and nothing more (iteration 4, §2): no AI needed to stop the series
        # (a one-off: done) — nor any budget.
        with_ai = content.force_query or bool(content.images) or not phrases.only_stop_request(content.text)
        budget = _check_budget(ctx, prefs, [raw]) if with_ai else _nothing()
        snapshot, _ = await _together(_snapshot(ctx, item_id, prefs, now), budget)
        if with_ai:
            out = await _extract(ctx, prefs, _extract_input(content, raw, prefs, reply_item=snapshot))
            if out.extraction.intent == "query" and out.extraction.query is not None:  # "?…" wins over the reply
                await _reply_query(ctx, [raw], content, out.extraction.query, prefs, _image_text(out.extraction))
                return
    except items_svc.NotFoundError:
        await _tell(ctx, [raw], ITEM_GONE_TEXT, prefs=prefs, purpose="answer")
        await _mark_done(ctx, [raw], None)
        return
    except AIUnavailable:
        await _defer(ctx, [raw])
        raise
    except AIConfigError as e:
        await _config_problem(ctx, prefs, e, [raw])
        raise RetryLater(CONFIG_RETRY, "AI config error", count_attempt=False) from e
    except AIBadResponse as e:
        await _give_up(ctx, prefs, [raw], FAILED_EDIT_TEXT, e, reply_to=raw.message_id)
        return

    if out is None:
        patch, image_text = EditPatch(action="stop_recurrence"), None
    else:
        patch = stop_phrase_patch(out.extraction.edit or EditPatch(), content.text)
        image_text = _image_text(out.extraction)
    if patch.action == "split":  # "разбей на шаги" in reply to the card
        await _split(ctx, prefs, item_id, now=now, note=content.text, raws=[raw], once_key=key, image_text=image_text)
        return
    # The reply is marked done after its card (the once-key, committed with the edit, guards a re-run: see the top).
    done_later = not _is_app([raw])
    async with ctx.sessionmaker() as session:
        if not await try_once(session, key):
            return  # applied concurrently
        result = await items_svc.apply_patch(session, item_id, patch, prefs, now)
        if patch.rrule and (lost := lost_times_warning(content.text, result.item)) is not None:
            result.warnings.append(lost)
        view = await _patch_view(session, result, prefs, heard=_heard([raw]))
        if not done_later:
            attached = _attach(session, [raw])
            _finish(attached, image_text)
            _app_update(attached, edited=[item_id])
        await session.commit()
        events = items_svc.take_parent_events(session)
    if not _is_app([raw]):
        await _send_patch_card(ctx, prefs, view, result, reply_to=raw.message_id, raw_message_id=raw.id)
    if done_later:
        await _done(ctx, [raw], image_text)
    await cards.after_steps(ctx, events, prefs, clock.utcnow(), silent=_quiet(prefs))
    await _react(ctx, [raw], sender.REACT_SAVED)
    ctx.scheduler_wakeup.set()
    await journal.record(ctx.sessionmaker, journal.edit_entry(item_id, move=moves(patch)))


def moves(patch: EditPatch) -> bool:
    """A text edit that moves the item to another time (not "когда-нибудь", not closing or deleting it): the
    stale-items check counts those (docs/features/iteration-4.md §4)."""
    when = patch.when
    return when is not None and when.period != "someday" and patch.action not in ("done", "delete")


def _picked_patch(raw: RawMessage, item_id: int) -> EditPatch | None:
    """The edit read from an edit-by-description message (``EDIT_KEY``) when ``item_id`` is on its candidate list;
    None for any other edit job (or a stored edit that no longer validates: the AI reads the message again)."""
    data = raw.raw_json or {}
    listed, stored = data.get(CANDIDATES_KEY), data.get(EDIT_KEY)
    if not isinstance(stored, dict) or not isinstance(listed, list) or item_id not in listed:
        return None
    try:
        return EditPatch.model_validate(stored)
    except ValidationError:
        log.warning("raw %s: the stored edit does not validate, reading the message again", raw.id)
        return None


async def _apply_picked(
    ctx: AppContext, raw: RawMessage, item_id: int, patch: EditPatch, key: str, prefs: Prefs
) -> None:
    """The picked item gets the edit read from the message: no second AI call (no budget check either). Times in
    it are relative to the message, as when it was read."""
    now = _msg_now(raw)
    if patch.action == "split":  # "разбей переезд на шаги": the split call itself still asks the AI
        note = _join(raw.text, raw.transcript, raw.image_text)
        await _split(ctx, prefs, item_id, now=now, note=note, raws=[raw], once_key=key)
        return
    await _apply_edit(ctx, prefs, [raw], item_id, patch, now=now, image_text=None, once_key=key)


async def handle_edit_time(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Job "edit_time" ``{item_id, text, message_id?, now?, app?}`` from the card's "🕒 Время" button.

    ``message_id`` (the user's text message) gets the reply card and reactions; ``now`` (ISO time the text was
    sent) keeps "завтра" right when the job runs late. Both are optional. With ``message_id`` the patch is applied
    at most once (a once-key is committed together with it). ``app`` (the Mini App): nothing goes to the chat, the
    outcome is returned for ``Job.result``.

    Only the time changes (when / repetition / lead of the model's patch; a title, category or "удали" read into
    the answer is dropped). An answer without a usable time changes nothing: "🤷 Не понял время…" and 🤔.
    """
    item_id = int(payload["item_id"])
    text = str(payload.get("text") or "").strip()
    message_id = _opt_int(payload.get("message_id"))
    app = bool(payload.get("app"))
    if not text:
        return _outcome(False, "empty", TIME_NOT_UNDERSTOOD_TEXT) if app else None
    key = f"edit-time-done:{item_id}:{message_id}" if message_id is not None else None
    if key is not None:
        async with ctx.sessionmaker() as session:
            if await session.get(OnceKey, key) is not None:
                return None
    prefs = await _prefs(ctx)
    await _check_budget(ctx, prefs, [], message_id=message_id)
    now = _payload_time(payload.get("now")) or clock.utcnow()
    try:
        async with ctx.sessionmaker() as session:
            snapshot = await items_svc.item_snapshot(session, item_id, prefs, now)
        inp = ExtractInput(text=f"перенеси на: {text}", source="text", now=now, prefs=prefs, reply_item=snapshot)
        out = await _extract(ctx, prefs, inp)
    except items_svc.NotFoundError:
        await _send(ctx, ITEM_GONE_TEXT, prefs=prefs, reply_to=message_id, purpose="answer", app=app)
        return _outcome(False, "gone", ITEM_GONE_TEXT) if app else None
    except AIUnavailable:
        await _defer(ctx, [], message_id=message_id)
        raise
    except AIConfigError as e:
        await _config_problem(ctx, prefs, e, [], message_id=message_id)
        raise RetryLater(CONFIG_RETRY, "AI config error", count_attempt=False) from e
    except AIBadResponse as e:
        await _give_up(ctx, prefs, [], FAILED_EDIT_TEXT, e, reply_to=message_id, app=app)
        return _outcome(False, "failed", FAILED_EDIT_TEXT) if app else None

    patch = _time_patch(out.extraction.edit, now, prefs)
    if patch is None:
        await _send(ctx, TIME_NOT_UNDERSTOOD_TEXT, prefs=prefs, reply_to=message_id, purpose="answer", app=app)
        if message_id:
            await sender.react(ctx, message_id, sender.REACT_ASK)
        return _outcome(False, "not_understood", TIME_NOT_UNDERSTOOD_TEXT) if app else None
    async with ctx.sessionmaker() as session:
        if key is not None and not await try_once(session, key):
            return None  # applied by an earlier run of this job
        result = await items_svc.apply_patch(session, item_id, patch, prefs, now)
        view = await _patch_view(session, result, prefs)
        await session.commit()
        events = items_svc.take_parent_events(session)
    if not app:
        await _send_patch_card(ctx, prefs, view, result, reply_to=message_id)
    await cards.after_steps(ctx, events, prefs, clock.utcnow(), silent=_quiet(prefs))  # a step's new time
    if message_id:
        await sender.react(ctx, message_id, sender.REACT_SAVED)
    ctx.scheduler_wakeup.set()
    await journal.record(ctx.sessionmaker, journal.edit_entry(item_id, move=moves(patch)))
    return _outcome(True, item_id=item_id, warnings=list(result.warnings)) if app else None


def _outcome(ok: bool, reason: str | None = None, message: str | None = None, **extra: Any) -> dict[str, Any]:
    """``Job.result`` of a job started from the Mini App: ``{"ok", "reason", "message", …}`` (message in Russian)."""
    return {"ok": ok, "reason": reason, "message": message, **extra}


def _time_patch(edit: EditPatch | None, now: datetime, prefs: Prefs) -> EditPatch | None:
    """The time part of the model's patch for a "🕒 Время" answer; None when it has no usable time ("когда-нибудь"
    is one: it takes the dates away)."""
    if edit is None:
        return None
    when = edit.when if edit.when is not None and _usable_when(edit.when, now, prefs) else None
    if when is None and not edit.rrule and not edit.lead:
        return None
    return EditPatch(when=when, rrule=edit.rrule, lead=edit.lead)


def _usable_when(when: When, now: datetime, prefs: Prefs) -> bool:
    due, precision, _, _ = timeresolve.resolve_when(when, now, prefs)
    return due is not None or precision == "someday"


async def handle_split(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Job "split" ``{item_id, message_id?, now?, app?}`` from the card's [🪄 Разбить на шаги]: the AI breaks the task
    into steps and the card (``message_id``) turns into the checklist. ``app`` (the Mini App): nothing goes to the
    chat, the outcome is returned for ``Job.result``. ``now`` (the press) is the steps' reference time only while it
    is fresh (``SPLIT_NOW_MAX_AGE``): a job that waited (AI down, budget) plans them from the time it runs, so
    "завтра" is never already past."""
    prefs = await _prefs(ctx)
    run_at, pressed = clock.utcnow(), _payload_time(payload.get("now"))
    now = pressed if pressed is not None and run_at - pressed <= SPLIT_NOW_MAX_AGE else run_at
    card = _opt_int(payload.get("message_id"))
    app = bool(payload.get("app"))
    outcome = await _split(ctx, prefs, int(payload["item_id"]), now=now, note=None, card_message=card, app=app)
    return outcome if app else None


async def _split(
    ctx: AppContext,
    prefs: Prefs,
    item_id: int,
    *,
    now: datetime,
    note: str | None,
    card_message: int | None = None,
    raws: Sequence[RawMessage] = (),
    once_key: str | None = None,
    image_text: str | None = None,
    app: bool = False,
) -> dict[str, Any]:
    """Break the item into steps (the "split" call, ``note`` = the owner's words) and show its checklist: the card
    ``card_message`` is redrawn in place (the button), or a new card replies to the owner's message (``raws``, done
    afterwards; ``once_key`` is committed with the steps). Nothing to split (closed, a series, a step, has steps,
    the model found no steps) -> a short note with the card. For the Mini App (``app``, or ``raws`` from the app)
    nothing is shown in the chat. Returns the outcome (``_outcome``)."""
    raws = list(raws)
    app = app or _is_app(raws)
    reply_to = raws[0].message_id if raws else card_message
    raw_id = raws[0].id if raws else None
    async with ctx.sessionmaker() as session:
        item = await items_svc.get_item(session, item_id)
        problem = await _split_problem(session, item)
        snapshot = await items_svc.item_snapshot(session, item_id, prefs, now) if problem is None else None
    if item is None or item.status == "deleted":
        await _send(ctx, ITEM_GONE_TEXT, prefs=prefs, reply_to=reply_to, purpose="answer", app=app)
        await _mark_done(ctx, raws, image_text, note=ITEM_GONE_TEXT)
        return _outcome(False, "gone", ITEM_GONE_TEXT)
    if problem is not None or snapshot is None:
        await _mark_done(ctx, raws, image_text, note=problem, edited=[item_id])
        if not app:
            await _show_split(ctx, prefs, item_id, problem, reply_to=reply_to, raw_id=raw_id, heard=_heard(raws))
        await _react(ctx, raws, sender.REACT_SAVED)
        return _outcome(False, "has_steps" if problem == HAS_STEPS_TEXT else "cant_split", problem)
    task = {k: v for k, v in snapshot.items() if k in SPLIT_TASK_FIELDS and v not in (None, "", [])}
    inp = SplitInput(task=task, now=now, prefs=prefs, note=note)
    out = await _split_call(ctx, prefs, inp, raws, card_message, app=app)
    if out is None:
        return _outcome(False, "failed", FAILED_SPLIT_TEXT)
    async with ctx.sessionmaker() as session:
        if once_key is not None and not await try_once(session, once_key):
            return _outcome(True, steps=0)  # applied by an earlier run of this job
        created = await items_svc.add_steps(
            session, item_id, out.steps, prefs, now, model=out.model, prompt_version=out.prompt_version
        )
        why = None if created else NO_STEPS_TEXT if not out.steps else HAS_STEPS_TEXT  # or split / closed meanwhile
        fresh = await _reload(session, raws)
        _finish(fresh, image_text)
        _app_update(fresh, note=why, edited=[item_id])
        await session.commit()
    if not app:
        await _present_split(ctx, prefs, item_id, why, created=bool(created), card_message=card_message, raws=raws)
    await _react(ctx, raws, sender.REACT_SAVED)
    ctx.scheduler_wakeup.set()  # steps with a time have reminders
    if not created:
        return _outcome(False, "no_steps" if not out.steps else "has_steps", why)
    return _outcome(True, steps=len(created))


async def _present_split(
    ctx: AppContext,
    prefs: Prefs,
    item_id: int,
    why: str | None,
    *,
    created: bool,
    card_message: int | None,
    raws: Sequence[RawMessage],
) -> None:
    """The checklist in the chat: the button's card redrawn in place, else a new card (``why`` above it; in reply to
    a voice note with what was heard)."""
    reply_to = raws[0].message_id if raws else card_message
    raw_id = raws[0].id if raws else None
    if created and card_message is not None and not raws:
        await _redraw_card(ctx, prefs, item_id, card_message)
    else:
        await _show_split(ctx, prefs, item_id, why, reply_to=reply_to, raw_id=raw_id, heard=_heard(raws))


async def _split_problem(session: AsyncSession, item: Any) -> str | None:
    """Why the item gets no steps now (None: it can be split)."""
    if item is None or item.status == "deleted":
        return ITEM_GONE_TEXT
    if (await query.card_extras(session, item)).steps:
        return HAS_STEPS_TEXT
    return None if items_svc.can_split(item) else CANT_SPLIT_TEXT


async def _split_call(
    ctx: AppContext,
    prefs: Prefs,
    inp: SplitInput,
    raws: list[RawMessage],
    card_message: int | None,
    *,
    app: bool = False,
) -> SplitOutput | None:
    """``split_steps`` behind the budget check, with the processor's error handling; None when it gave up."""
    reply_to = raws[0].message_id if raws else card_message
    try:
        await _check_budget(ctx, prefs, raws)
        with meter.ai():
            return await split_steps(ctx.ai, ctx.settings, inp)
    except AIUnavailable:
        await _defer(ctx, raws)
        if not raws and card_message is not None:  # the button has no message to put ✍ on: say it once
            async with ctx.sessionmaker() as session:
                first = await try_once(session, f"split-wait:{card_message}")
                await session.commit()
            if first:
                await _send(ctx, SPLIT_WAIT_TEXT, prefs=prefs, reply_to=card_message, purpose="notice")
        raise
    except AIConfigError as e:
        await _config_problem(ctx, prefs, e, raws)
        raise RetryLater(CONFIG_RETRY, "AI config error", count_attempt=False) from e
    except AIBadResponse as e:
        if raws:
            await _give_up(ctx, prefs, raws, FAILED_SPLIT_TEXT, e, reply_to=reply_to)
        else:  # the button: its message is the bot's own card, so no 😢 reaction
            log.warning("split: no usable AI result (%s)", describe_error(e))
            await _send(ctx, FAILED_SPLIT_TEXT, prefs=prefs, reply_to=card_message, purpose="notice", app=app)
        return None


async def _show_split(
    ctx: AppContext,
    prefs: Prefs,
    item_id: int,
    note: str | None,
    *,
    reply_to: int | None,
    raw_id: int | None = None,
    heard: str | None = None,
) -> None:
    """A new card of the item (with ``note`` above it) in reply to ``reply_to``; ``heard``: the transcript of the
    voice note it answers (instead of the one the item came from)."""
    async with ctx.sessionmaker() as session:
        item = await items_svc.get_item(session, item_id)
        if item is None:
            return
        text, markup = await cards.card_view(session, item, prefs, clock.utcnow(), heard=heard)
        occ = await items_svc.current_occurrence(session, item_id)
        await session.commit()
    await _send(
        ctx,
        views.fit(f"{note}\n\n{text}") if note else text,
        markup,
        prefs=prefs,
        reply_to=reply_to,
        purpose="card",
        item_id=item_id,
        occurrence_id=occ.id if occ is not None and item.status != "deleted" else None,
        raw_message_id=raw_id,
    )


async def _redraw_card(ctx: AppContext, prefs: Prefs, item_id: int, message_id: int) -> None:
    """The card the button was pressed on, with its new checklist (a new card when it cannot be edited)."""
    async with ctx.sessionmaker() as session:
        item = await items_svc.get_item(session, item_id)
        view = await cards.card_view(session, item, prefs, clock.utcnow()) if item is not None else None
        await session.commit()
    if view is None:
        return
    try:
        await sender.edit(ctx, message_id, view[0], view[1])
    except TelegramAPIError as e:
        log.info("card %s not redrawn after the split (%s), sending a new one", message_id, type(e).__name__)
        await _show_split(ctx, prefs, item_id, None, reply_to=message_id)


HANDLERS: dict[str, Handler] = {
    "message": handle_message,
    "album": handle_album,
    "edit": handle_edit,
    "clarify": handle_clarify,
    "edit_time": handle_edit_time,
    "split": handle_split,
}


async def on_dead(ctx: AppContext, job: Job) -> None:
    """A job gave up: mark its raw message failed, tell the owner, react 😢 on the source message."""
    try:
        raw = await _fail_raws(ctx, job)
        if (job.payload or {}).get("app"):  # the Mini App shows the dead job itself (GET /api/jobs/{id})
            return
        text = FAILED_EDIT_TEXT if job.kind in ("edit", "edit_time") else FAILED_MESSAGE_TEXT
        if job.kind == "split":
            text = FAILED_SPLIT_TEXT
        elif job.kind not in HANDLERS:
            text = f"😢 Не смог выполнить фоновую задачу «{views.esc(job.kind)}»."
        reply_to = raw.message_id if raw is not None else _opt_int((job.payload or {}).get("message_id"))
        if raw is not None and _is_app([raw]):  # no chat message to reply to or react on: a plain notice
            reply_to = None
        prefs = await _prefs(ctx)
        await _send(ctx, text, prefs=prefs, reply_to=reply_to, purpose="notice", raw_message_id=raw.id if raw else None)
        if reply_to and job.kind != "split":  # a split job's message is the bot's own card
            await sender.react(ctx, reply_to, sender.REACT_FAILED)
    except Exception as e:
        log.error("on_dead for job %s failed: %s", job.id, describe_error(e))


# --- pipeline ---------------------------------------------------------------------------------------------------


async def _process(
    ctx: AppContext, raws: list[RawMessage], *, prefs: Prefs | None = None, clarification: str | None = None
) -> None:
    """The pipeline of the module docstring. The database waits on the way to the card are kept few
    (docs/features/iteration-4.md §7): the prefs come with the raws (``prefs``), the budget check and the related
    candidates are looked up at once, the save is one transaction, and what can wait (``bot_messages``, the AI calls'
    ai_usage rows, the journal) is written after the card."""
    primary = raws[0]
    prefs = prefs or await _prefs(ctx)
    replace = clarification is not None
    if not replace and not any(_needs_ai(r) for r in raws):
        await _save_without_ai(ctx, raws, prefs)
        return
    content: _Content | None = None
    try:
        # A clarification reuses the stored OCR text instead of paying for the pictures again.
        content = await _gather(ctx, raws, prefs, with_images=not (replace and primary.image_text), meanwhile=True)
        try:
            if content.unheard:
                await _unheard(ctx, raws, prefs, _job_of(raws))
                return
            if content.empty and not replace:
                await _save_without_ai(ctx, raws, prefs)
                return
            # One database wait for all three: the budget check, the related candidates, the new transcript stored
            # (before extract() is paid for).
            _, candidates, _ = await _all(
                _check_budget(ctx, prefs, raws), _related_candidates(ctx, raws, content, prefs), _stored(content)
            )
        finally:
            await asyncio.gather(*content.writes, return_exceptions=True)
        inp = _extract_input(content, primary, prefs, clarification=clarification, related=candidates)
        out = await _extract(ctx, prefs, inp)
        await _dispatch(ctx, raws, content, out, prefs, replace=replace, related=[c["id"] for c in candidates])
    except AIUnavailable:
        await _defer(ctx, raws)
        raise
    except AIConfigError as e:
        await _config_problem(ctx, prefs, e, raws)
        raise RetryLater(CONFIG_RETRY, "AI config error", count_attempt=False) from e
    except AIBadResponse as e:
        if replace:  # the items of the first pass stay as they are
            await _give_up(ctx, prefs, raws, FAILED_MESSAGE_TEXT, e, reply_to=primary.message_id)
        else:
            await _save_unparsed(ctx, raws, prefs, content or _plain_content(raws), e)
        return
    ctx.scheduler_wakeup.set()


async def _extract(ctx: AppContext, prefs: Prefs, inp: ExtractInput) -> ExtractOutput:
    """``extract()``; when the fallback stood in for a missing (404) primary model, or the fallback itself is missing,
    tell the owner once a day (each)."""
    with meter.ai():
        out = await extract(ctx.ai, ctx.settings, inp)
    day = clock.utcnow().astimezone(prefs.zone).date().isoformat()
    if out.missing_model:
        text = (
            f"⚠️ Модели <code>{views.esc(out.missing_model)}</code> нет на chutes.ai (404), "
            f"пока работаю на запасной <code>{views.esc(out.model)}</code>.\n"
            "Проверь LLM_MODEL в .env."
        )
        await _notice_once(ctx, prefs, f"aimodel:{day}", text)
    if out.missing_fallback:
        setting = "VISION_FALLBACK_MODEL" if inp.images else "LLM_FALLBACK_MODEL"
        text = (
            f"⚠️ Запасной модели <code>{views.esc(out.missing_fallback)}</code> нет на chutes.ai (404): "
            "когда основная занята, подменить её некому.\n"
            f"Проверь {setting} в .env."
        )
        await _notice_once(ctx, prefs, f"aifallback:{out.missing_fallback}:{day}", text)
    return out


async def _notice_once(ctx: AppContext, prefs: Prefs, key: str, text: str) -> None:
    async with ctx.sessionmaker() as session:
        first = await try_once(session, key)
        await session.commit()
    if first:
        await _send(ctx, text, prefs=prefs, purpose="notice")


async def _dispatch(
    ctx: AppContext,
    raws: list[RawMessage],
    content: _Content,
    out: ExtractOutput,
    prefs: Prefs,
    *,
    replace: bool,
    related: Sequence[int] = (),
) -> None:
    ext = out.extraction
    log.info("message %s: intent=%s items=%d (%s)", raws[0].message_id, ext.intent, len(ext.items), out.model)
    if ext.intent == "query" and ext.query is not None:
        await _reply_query(ctx, raws, content, ext.query, prefs, _image_text(ext))
    elif ext.intent == "edit" and ext.edit is not None and not content.forward_origin:
        await _edit_by_target(ctx, raws, content, out, prefs, replace=replace, related=related)
    else:
        # A forwarded post is someone else's words ("встреча отменяется, удали"): it is saved, never applied to the
        # owner's items; edits come from replies to cards and the ✏️ menu.
        await _save(ctx, raws, content, out, prefs, replace=replace, related=related)


async def _related_candidates(
    ctx: AppContext, raws: Sequence[RawMessage], content: _Content, prefs: Prefs
) -> list[dict[str, Any]]:
    """Earlier open items sharing a subject word with the message (§C1, ``query.related_candidates``) as the
    prompt's RELATED CANDIDATES: ``{id, kind, title, date}`` (the local date it was saved). None for questions and
    messages without text."""
    if content.force_query or not content.text.strip():
        return []
    async with ctx.autocommit() as session:
        rows = await query.related_candidates(
            session, content.text, _msg_now(raws[0]), exclude_raw_ids=[r.id for r in raws]
        )
    tz = prefs.zone
    return [
        {"id": r.item_id, "kind": r.kind, "title": r.title, "date": r.created_at.astimezone(tz).date().isoformat()}
        for r in rows
    ]


async def _save(
    ctx: AppContext,
    raws: list[RawMessage],
    content: _Content,
    out: ExtractOutput,
    prefs: Prefs,
    *,
    replace: bool,
    note: str | None = None,
    related: Sequence[int] = (),
) -> None:
    """Create the items (or replace them after a clarification), then cards / question and the reaction.

    ``note`` (why the message was saved, e.g. an edit whose item was not found) goes above the cards, and then
    every item gets a card, so that it can be fixed with its buttons. ``related``: the ids of the RELATED CANDIDATES
    the model was shown (only these may be linked).
    """
    primary = raws[0]
    ext = out.extraction
    image_text = _image_text(ext)
    extraction = ext if ext.items else _fallback_note(content.text, image_text)
    ask = ext.ask if not replace and note is None else None  # no question loops after a clarification
    now_view = clock.utcnow()
    # A chat message is marked done after its reply, one round trip less before the cards (docs/features/
    # iteration-4.md §7); a re-run after a crash in between finds its items and only marks it (``_load_message``).
    # A question (the answer reads its raw_json) and a message from the Mini App (no reply) are marked with the items.
    done_later = ask is None and not _is_app(raws)
    async with ctx.sessionmaker() as session:
        fresh = _attach(session, raws)
        head = fresh[0]
        if replace and not await try_once(session, _clarified_key(head.id)):
            return  # this answer was applied by an earlier run of the job
        kwargs: dict[str, Any] = {
            "extraction": extraction,
            "source": content.source,
            "source_text": _join(content.text, image_text) or None,
            "source_meta": _source_meta(content),
            "urls": content.urls,
            "prefs": prefs,
            "now": _msg_now(head),
            "model": out.model,
            "prompt_version": out.prompt_version,
            "related_allowed": related,
        }
        if replace:
            created = await items_svc.replace_items_for_raw(session, head.id, raw=head, **kwargs)
        else:
            created = await items_svc.create_items(session, raw=head, **kwargs)
        quiet_chat = ask is not None or _is_app(fresh)  # the Mini App reads the items, no cards
        lost = _lost_times(content.text, created, prefs)
        shown = (
            []
            if quiet_chat
            else await _cards(
                session, created, prefs, now_view, every=note is not None, lost=lost, heard=_heard(_by_message(raws))
            )
        )
        if ask is not None:
            head.raw_json = {**(head.raw_json or {}), ASK_KEY: ask.model_dump(mode="json")}
        if not done_later:
            _finish(fresh, image_text)
            _app_update(fresh, note=note)
        await session.commit()

    if ask is not None:
        if not _is_app(raws):  # the app shows the question from raw_json and answers it with POST …/answer
            await _send_ask(ctx, prefs, raws, ask)
        return
    heard = _heard(raws)
    if heard and created and not shown and not _is_app(raws):
        # Only notes / ideas from a voice note: no card, but the owner sees what was heard (a garbled transcript) —
        # a message per item, so that its buttons and a reply to it are about that item alone.
        for saved in created:
            text, markup = views.heard_item(heard, saved.item)
            await _send(
                ctx,
                text,
                markup,
                prefs=prefs,
                reply_to=primary.message_id,
                purpose=sender.PURPOSE_HEARD,
                item_id=saved.item.id,
                raw_message_id=primary.id,
            )
    for created_item, (text, markup) in shown:
        occ = created_item.occurrence
        await _send(
            ctx,
            views.fit(f"{note}\n\n{text}") if note else text,
            markup,
            prefs=prefs,
            reply_to=primary.message_id,
            purpose="card",
            item_id=created_item.item.id,
            occurrence_id=occ.id if occ is not None else None,
            raw_message_id=primary.id,
        )
    if done_later:
        await _done(ctx, raws, image_text)
    await _react(ctx, raws, sender.REACT_SAVED)


async def _cards(
    session: AsyncSession,
    created: Sequence[CreatedItem],
    prefs: Prefs,
    now: datetime,
    *,
    every: bool = False,
    lost: dict[int, str] | None = None,
    heard: str | None = None,
) -> list[tuple[CreatedItem, View]]:
    """Cards for items with reminders, actionable kinds, a checklist or related items, or something to confirm
    (``every``: all items); the rest only get 👌. ``lost``: one more warning by item id (``_lost_times``).

    A new item's extras are known without asking the database unless it got steps or links just now: no checklist,
    no related items, no parent, and ``heard`` (what was heard in the message's voice notes)."""
    out: list[tuple[CreatedItem, View]] = []
    for c in created:
        res = c.resolution
        warnings = [*res.warnings, *filter(None, [(lost or {}).get(c.item.id)])]
        if c.steps or c.related:
            extras = await query.card_extras(session, c.item)
        else:
            extras = CardExtras(heard=heard)
        shown = every or c.reminders or c.item.kind in ACTIONABLE or res.needs_confirm or warnings
        if shown or extras.steps or extras.related:
            view = views.item_card(c.item, c.occurrence, c.reminders, prefs, now, warnings=warnings, extras=extras)
            out.append((c, view))
    return out


def _lost_times(text: str, created: Sequence[CreatedItem], prefs: Prefs) -> dict[int, str]:
    """«⚠️ понял только 09:00» for the series of a message that named more times than its items kept (iteration 4,
    §2: "каждый день в 9:00 и 18:00" saved with 09:00 only). The times of all items of the message count — "в 9 пить
    таблетки, в 18 гулять" may well be two series — compared on a 12-hour dial (``phrases.lost_times``)."""
    named = phrases.clock_times(text)
    series = [c.item for c in created if c.item.rec_active and c.item.rrule]
    if len(named) < 2 or not series:
        return {}
    kept = {c.item.id: _kept_times(c, prefs) for c in created}
    if not phrases.lost_times(named, [t for times in kept.values() for t in times]):
        return {}
    return {item.id: phrases.understood_only(kept[item.id]) for item in series if kept[item.id]}


def _kept_times(created: CreatedItem, prefs: Prefs) -> list[time]:
    """The times of day an item keeps: a series its rule's ones (``_series_times``), a one-off at a time of day the
    time stated for it."""
    item, res = created.item, created.resolution
    if item.rrule:
        return _series_times(item)
    at = res.stated_at or res.due_at
    if at is None or res.precision not in ("exact", "slot", "relative"):
        return []
    return [at.astimezone(prefs.zone).time()]


def _series_times(item: Item) -> list[time]:
    """The times of day of a series: its rule's own ("каждый день в 9 и 18"), else the one it started at."""
    start = item.rec_dtstart_local
    return list(recurrence.rule_times(item.rrule)) or ([start.time()] if start is not None else [])


def lost_times_warning(text: str, item: Item) -> str | None:
    """«понял только 09:00» for a series an edit gave a rule ("каждый день в 9 и 18" in reply to a card) that kept
    fewer times than the message named; None when nothing was lost (or the item is no active series)."""
    if not (item.rec_active and item.rrule):
        return None
    named = phrases.clock_times(text)
    kept = _series_times(item)
    if len(named) < 2 or not kept or not phrases.lost_times(named, kept):
        return None
    return phrases.understood_only(kept)


def stop_phrase_patch(patch: EditPatch, text: str) -> EditPatch:
    """«Больше не напоминай», «закрывай вообще», «хватит повторять», «останови повтор» (iteration 4, §2) are a stop
    whatever the model made of them: "done" or "no_reminder" (or an update without a new time) become
    ``stop_recurrence`` — a series stops and its instance already due is closed, a one-off is done
    (``items.apply_patch``). A patch with a new time, a split or a delete stays as it is."""
    if not phrases.stop_request(text):
        return patch
    timed = patch.when is not None or bool(patch.rrule) or bool(patch.lead)
    if patch.action in ("done", "no_reminder") or (patch.action == "update" and not timed):
        return patch.model_copy(update={"action": "stop_recurrence"})
    return patch


async def _send_ask(ctx: AppContext, prefs: Prefs, raws: list[RawMessage], ask: Ask) -> None:
    primary = raws[0]
    text, markup = views.with_heard(views.ask_message(ask, primary.id), _heard(raws))
    reply_to, raw_id = primary.message_id, primary.id
    await _send(ctx, text, markup, prefs=prefs, reply_to=reply_to, purpose=sender.PURPOSE_ASK, raw_message_id=raw_id)
    await _react(ctx, raws, sender.REACT_ASK)


async def _reply_query(
    ctx: AppContext,
    raws: list[RawMessage],
    content: _Content,
    spec: QuerySpec,
    prefs: Prefs,
    image_text: str | None,
) -> None:
    """Rows for the QuerySpec as a list, or a short prose answer (the list when no answer comes). Sent before the raw
    is marked done; for a message from the Mini App kept with it instead (``APP_KEY`` "answer")."""
    primary = raws[0]
    found = await run_question(ctx, content.text.strip() or spec.text or "", spec, prefs, _msg_now(primary))
    if _is_app(raws):
        answer = {"text": found.text, "item_ids": [r.item_id for r in found.rows], "title": found.title}
        await _mark_done(ctx, raws, image_text, answer=answer)
        return
    markup: InlineKeyboardMarkup | None = None
    if found.text is not None:
        text = views.fit(views.esc(found.text))
    else:
        text, markup = views.rows_list(found.title, found.rows, prefs, clock.utcnow(), empty=NOTHING_FOUND)
    text, markup = views.with_heard((text, markup), _heard(raws))
    await sender.send(
        ctx,
        text,
        markup,
        reply_to=primary.message_id,
        silent=_quiet(prefs),
        quiet_checked=True,
        purpose="answer",
        raw_message_id=primary.id,
    )
    await _mark_done(ctx, raws, image_text)
    await _react(ctx, raws, sender.REACT_SAVED)


@dataclass
class QueryAnswer:
    """What a question found: the rows, the prose answer (plain text, None -> show the rows) and a list title."""

    rows: list[Row]
    text: str | None
    title: str
    spec: QuerySpec


async def run_question(ctx: AppContext, question: str, spec: QuerySpec, prefs: Prefs, now: datetime) -> QueryAnswer:
    """Rows of the QuerySpec and, when the model asked for a summary, a short prose answer. ``AIUnavailable`` /
    ``AIBadResponse`` of the answer only leave it out: the rows are found already, and the list beats a retry that
    would pay for the extraction again."""
    async with ctx.autocommit() as session:
        rows = await query.run_query(session, spec, prefs, now)
    text: str | None = None
    if spec.answer == "summary":
        try:
            with meter.ai():
                text = await answer_query(ctx.ai, ctx.settings, question, query.rows_for_llm(rows, prefs), now, prefs)
        except (AIUnavailable, AIBadResponse) as e:
            log.warning("no prose answer (%s), showing the list", describe_error(e))
    title = f"🔎 По запросу «{spec.text}»" if spec.text else "🔎 Вот что нашёл"
    return QueryAnswer(rows=rows, text=text, title=title, spec=spec)


async def ask_question(ctx: AppContext, question: str, prefs: Prefs, now: datetime) -> QueryAnswer:
    """A question from the Mini App (``POST /api/ask``): the "?" path without a saved message — the budget check
    (``RetryLater`` when it is exhausted), ``extract()`` forced into a query, then ``run_question``. AI errors of the
    extraction propagate (``AIUnavailable``, ``AIConfigError``, ``AIBadResponse``)."""
    await _check_budget(ctx, prefs, [])
    inp = ExtractInput(text=question, source="text", now=now, prefs=prefs, force_query=True)
    out = await _extract(ctx, prefs, inp)
    spec = out.extraction.query or QuerySpec(text=question[:120])
    return await run_question(ctx, question, spec, prefs, now)


async def _edit_by_target(
    ctx: AppContext,
    raws: list[RawMessage],
    content: _Content,
    out: ExtractOutput,
    prefs: Prefs,
    *,
    replace: bool,
    related: Sequence[int] = (),
) -> None:
    """ "закрой задачу про документацию", "перенеси встречу с Олегом на 16": the item is looked up by ``edit.target``
    (docs/features/iteration-3.md §3). Exactly one exact match (every word of the target) among the open items (the
    done ones for "undone"; or the only exact match there is) -> the edit is applied at once, whatever it is, and
    answered "✅ Закрыл «…»" / "🕒 Перенёс «…» на …" / "🗑 Удалил «…»" with [↩️ Отменить] (``_apply_edit``). Several,
    or only a loose match (some word, a tag) -> the owner picks the item (buttons, or a reply with its number); the
    edit read from the message is kept with it (``EDIT_KEY``) and applied to the picked item without asking the AI
    again. The Mini App has no [↩️ Отменить]: there delete / done / no_reminder / stop_recurrence still wait for the
    pick, even for one match. Nothing found -> the message is kept as a note and its card says so: it may be a new
    task worded like an edit («удалить приложение банка»). New items the model found in the same message ("купил
    молоко, хлеба не было — купить завтра") are saved as well, after the edit (``_with_items``)."""
    primary = raws[0]
    patch = stop_phrase_patch(out.extraction.edit or EditPatch(), content.text)
    image_text = _image_text(out.extraction)
    now = _msg_now(primary)
    target = patch.target or ""
    async with ctx.sessionmaker() as session:
        rows = await query.find_edit_candidates(session, target, prefs, now, EXACT_POOL, exact=True)
        exact = bool(rows)
        if not exact:
            rows = await query.find_edit_candidates(session, target, prefs, now)
    if not rows:
        await _save(ctx, raws, content, out, prefs, replace=replace, note=NOT_FOUND_TEXT, related=related)
        return
    single = _single_match(rows, patch.action) if exact else None
    if single is None or (_is_app(raws) and patch.action in CONFIRM_ACTIONS):
        await _send_candidates(ctx, prefs, raws, rows, image_text, patch=patch)
    elif patch.action == "split":  # "разбей переезд на шаги"
        await _split(ctx, prefs, single.item_id, now=now, note=content.text, raws=raws, image_text=image_text)
    else:
        await _apply_edit(ctx, prefs, raws, single.item_id, patch, now=now, image_text=image_text)
    if (also := _with_items(out)) is not None:
        await _save(ctx, raws, content, also, prefs, replace=False, related=related)


def _with_items(out: ExtractOutput) -> ExtractOutput | None:
    """The new items of an edit by description, when the message asked for more than the edit (else None): they
    are saved like any message, nothing it said is lost."""
    ext = out.extraction
    if not ext.items:
        return None
    extraction = Extraction(intent="save", items=ext.items, image=ext.image)
    return ExtractOutput(extraction=extraction, model=out.model, prompt_version=out.prompt_version)


def _single_match(rows: Sequence[Row], action: str) -> Row | None:
    """The item an edit by description is about without asking: the only exact match among the open items (among
    the done ones for "undone"; for "done" an open task, event or purchase: an open note or idea is never closed
    in place of a task that is done already), else the only exact match there is; None when it is not clear."""
    wanted = "done" if action == "undone" else "open"
    pool = [r for r in rows if r.status == wanted and (action != "done" or r.kind in ACTIONABLE)]
    if len(pool) == 1:
        return pool[0]
    return rows[0] if len(rows) == 1 else None


async def _apply_edit(
    ctx: AppContext,
    prefs: Prefs,
    raws: list[RawMessage],
    item_id: int,
    patch: EditPatch,
    *,
    now: datetime,
    image_text: str | None,
    once_key: str | None = None,
) -> None:
    """Apply an edit by description to the item it is about and answer with what changed and [↩️ Отменить]
    (``views.edit_applied``); what the edit changed is kept with the raw message for the undo (``services.undo``).
    ``once_key`` (a picked candidate) is committed together with the edit. An item deleted meanwhile -> "🤷 Не нашёл
    эту запись"."""
    primary = raws[0]
    app = _is_app(raws)
    view: View | None = None
    applied: undo.Applied | None = None
    async with ctx.sessionmaker() as session:
        if once_key is not None and not await try_once(session, once_key):
            return  # applied by an earlier run of the job
        item = await items_svc.get_item(session, item_id)
        if item is not None and item.status != "deleted":
            applied = await undo.apply(session, item_id, patch, prefs, now)
            if patch.rrule and (lost := lost_times_warning(_said(raws), applied.result.item)) is not None:
                applied.result.warnings.append(lost)
        fresh = await _reload(session, raws)
        _finish(fresh, image_text)
        if applied is None:
            _app_update(fresh, note=ITEM_GONE_TEXT)
        else:
            snapshot = applied.snapshot if applied.changed else None  # nothing changed: nothing to undo
            if snapshot is not None and fresh:
                undo.keep(fresh[0], item_id, snapshot)
            _app_update(fresh, edited=[item_id])
            result = applied.result
            view = views.edit_applied(
                result.item,
                result.occurrence,
                result.reminders,
                patch,
                prefs,
                clock.utcnow(),
                title_before=applied.title_before,
                moved=applied.moved,
                changed=applied.changed,
                undo=(primary.id, item_id) if snapshot is not None and not app else None,
                steps_closed=applied.steps_closed,
                warnings=result.warnings,
            )
            view = views.with_heard(view, _heard(raws))
        await session.commit()
        events = items_svc.take_parent_events(session)
    if applied is None or view is None:
        await _send(ctx, ITEM_GONE_TEXT, prefs=prefs, reply_to=primary.message_id, purpose="answer", app=app)
        return
    occ = applied.result.occurrence
    await _send(
        ctx,
        view[0],
        view[1],
        prefs=prefs,
        app=app,
        reply_to=primary.message_id,
        purpose=sender.PURPOSE_EDITED,
        item_id=item_id,
        occurrence_id=occ.id if occ is not None and not applied.result.deleted else None,
        raw_message_id=primary.id,
    )
    await cards.after_steps(ctx, events, prefs, clock.utcnow(), silent=_quiet(prefs))
    await _react(ctx, raws, sender.REACT_SAVED)
    ctx.scheduler_wakeup.set()
    await journal.record(ctx.sessionmaker, journal.edit_entry(item_id, move=moves(patch)))


async def _send_candidates(
    ctx: AppContext,
    prefs: Prefs,
    raws: list[RawMessage],
    rows: Sequence[Row],
    image_text: str | None,
    *,
    patch: EditPatch | None = None,
) -> None:
    """ "✏️ Какую запись изменить?" with a button per item (PickCb -> job "edit"); the ids are kept with the raw so
    that a text reply "2" picks the second one, and so is the edit itself (``patch`` -> ``EDIT_KEY``): the picked
    item gets it without a second AI call. Sent before the raw is marked done."""
    primary = raws[0]
    rows = list(rows)[: views.CANDIDATES_MAX]
    async with ctx.sessionmaker() as session:
        [head] = await _reload(session, [primary])
        data = {**(head.raw_json or {}), CANDIDATES_KEY: [r.item_id for r in rows]}
        if patch is not None:
            data[EDIT_KEY] = patch.model_dump(mode="json", exclude_none=True)
        head.raw_json = data
        await session.commit()
    if _is_app(raws):  # the app lists them from raw_json and picks with POST /api/inbox/{raw_id}/pick
        await _mark_done(ctx, raws, image_text)
        return
    text, markup = views.with_heard(views.edit_candidates(rows, primary.id, prefs, clock.utcnow()), _heard(raws))
    await sender.send(
        ctx,
        text,
        markup,
        reply_to=primary.message_id,
        silent=_quiet(prefs),
        quiet_checked=True,
        purpose=sender.PURPOSE_PICK,
        raw_message_id=primary.id,
    )
    await _mark_done(ctx, raws, image_text)
    await _react(ctx, raws, sender.REACT_ASK)


async def _patch_view(session: AsyncSession, result: PatchResult, prefs: Prefs, *, heard: str | None = None) -> View:
    """The card after an edit; ``heard``: the transcript of a voice edit, shown on the card instead of the item's own
    ("🎤 «…»", docs/features/iteration-3.md §6)."""
    view = await cards.patch_card(session, result, prefs, clock.utcnow(), heard=heard)
    return views.with_heard(view, heard) if result.deleted or result.item.status == "deleted" else view


async def _send_patch_card(
    ctx: AppContext,
    prefs: Prefs,
    view: View,
    result: PatchResult,
    *,
    reply_to: int | None,
    raw_message_id: int | None = None,
) -> None:
    occ = result.occurrence
    await _send(
        ctx,
        view[0],
        view[1],
        prefs=prefs,
        reply_to=reply_to,
        purpose="card",
        item_id=result.item.id,
        occurrence_id=occ.id if occ is not None and not result.deleted else None,
        raw_message_id=raw_message_id,
    )


async def _save_without_ai(ctx: AppContext, raws: list[RawMessage], prefs: Prefs) -> None:
    """Media without text: one "other" item titled by type («Видео», «Стикер 😀», «Документ: a.pdf»), 👌."""
    primary = raws[0]
    await _store_without_ai(ctx, raws, prefs, _plain_content(raws))
    log.info("message %s (%s) saved without AI", primary.message_id, primary.content_type)
    await _react(ctx, raws, sender.REACT_SAVED)
    ctx.scheduler_wakeup.set()


async def _save_unparsed(
    ctx: AppContext, raws: list[RawMessage], prefs: Prefs, content: _Content, error: AIBadResponse
) -> None:
    """``AIBadResponse`` for a new message: the same request would give the same (temperature 0, fixed seed), so
    instead of retrying it is kept without AI — its text as a note, media without text as an item titled by type —
    and the owner is told (😢)."""
    primary = raws[0]
    log.warning("message %s: no usable AI result (%s), saved without AI", primary.message_id, describe_error(error))
    await _store_without_ai(ctx, raws, prefs, content, note=FAILED_MESSAGE_TEXT)
    await _tell(ctx, raws, FAILED_MESSAGE_TEXT, prefs=prefs, purpose="notice")
    await _react(ctx, raws, sender.REACT_FAILED)


async def _store_without_ai(
    ctx: AppContext, raws: list[RawMessage], prefs: Prefs, content: _Content, *, note: str | None = None
) -> None:
    """One item and raws ``done``: a note with the text of ``content``, or an "other" item titled by type
    (``note``: what a message from the Mini App is told)."""
    text = content.text.strip()
    if text:
        extraction = _fallback_note(text, None)
        source = content.source
    else:
        title = _media_title(raws[0])
        if len(raws) > 1:
            title = f"{title} ×{len(raws)}"
        extraction = Extraction(items=[ExtractedItem(kind="other", title=_clip(title, TITLE_MAX))])
        source = "forward" if content.forward_origin else "other"
    async with ctx.sessionmaker() as session:
        fresh = await _reload(session, raws)
        await items_svc.create_items(
            session,
            raw=fresh[0],
            extraction=extraction,
            source=source,
            source_text=text or None,
            source_meta=_source_meta(content),
            urls=content.urls,
            prefs=prefs,
            now=_msg_now(fresh[0]),
            model=None,
            prompt_version=None,
        )
        _finish(fresh, None)
        _app_update(fresh, note=note)
        await session.commit()


def _plain_content(raws: Sequence[RawMessage]) -> _Content:
    """What the raws carry without downloads, STT or AI: their texts, URLs and forward origin."""
    urls = list(dict.fromkeys(u for r in raws for u in (r.urls or [])))
    return _Content(
        text=_join(*(r.text for r in raws)),
        source=_source_kind(raws, urls),
        urls=urls,
        forward_origin=next((r.forward_origin for r in raws if r.forward_origin), None),
    )


# --- content ----------------------------------------------------------------------------------------------------


async def _gather(
    ctx: AppContext, raws: list[RawMessage], prefs: Prefs, *, with_images: bool, meanwhile: bool = False
) -> _Content:
    """Texts, transcripts and pictures of the raws (in message order), their URLs, forward origin, link titles.

    ``meanwhile``: new transcripts and link titles are stored while the job goes on (``_Content.writes``; the caller
    awaits them with ``_stored`` before it pays for anything else), else before this returns."""
    writes: list[asyncio.Task[None]] = []
    try:
        content = await _read(ctx, raws, prefs, with_images=with_images, writes=writes)
    except BaseException:
        await asyncio.gather(*writes, return_exceptions=True)  # a transcript paid for stays stored
        raise
    if not meanwhile:
        await _stored(content)
    return content


async def _read(
    ctx: AppContext, raws: list[RawMessage], prefs: Prefs, *, with_images: bool, writes: list[asyncio.Task[None]]
) -> _Content:
    """``_gather`` without waiting for ``writes``."""
    primary = raws[0]
    texts: list[str] = []
    urls: list[str] = []
    images: list[tuple[bytes, str]] = []
    origin: dict[str, Any] | None = None
    unheard = False
    for raw in raws:
        if raw.text and raw.text.strip():
            texts.append(raw.text.strip())
        urls.extend(u for u in raw.urls or [] if u not in urls)
        origin = origin or raw.forward_origin
        if raw.content_type in VOICE_TYPES:
            transcript = await _transcript(ctx, raw, prefs, raws, writes)
            if _is_unheard(raw):
                unheard = True
            elif transcript:
                texts.append(transcript)
        elif raw.content_type in IMAGE_TYPES and with_images and len(images) < MAX_IMAGES:
            room = MAX_REQUEST_IMAGE_BYTES - sum(len(data) for data, _ in images)
            image = await _image(ctx, raw, max_bytes=min(MAX_IMAGE_BYTES, room))
            if image is not None:
                images.append(image)
    if not with_images and primary.image_text:
        texts.append(primary.image_text)
    force_query = any(r.force_query for r in raws)
    text = "\n\n".join(texts)
    if force_query:
        text = _strip_question_mark(text)
    return _Content(
        text=text,
        source=_source_kind(raws, urls),
        urls=urls,
        images=images,
        forward_origin=origin,
        link_titles=await _link_titles(ctx, primary, urls, writes),
        force_query=force_query,
        unheard=unheard,
        writes=writes,
    )


async def _stored(content: _Content) -> None:
    """Wait for the transcripts and link titles ``_gather`` stores meanwhile; the first failure is raised once all of
    them ended."""
    await _all(*content.writes)


async def _transcript(
    ctx: AppContext, raw: RawMessage, prefs: Prefs, raws: list[RawMessage], writes: list[asyncio.Task[None]]
) -> str:
    """Stored transcript, or download + STT and commit it right away (a retry must not pay twice).

    STT goes to chutes and / or the local Whisper in the order of ``prefs.stt_mode``; the budget is checked only
    before chutes (paid; exhausted -> ``raws`` deferred, ``RetryLater``), the local Whisper is free.
    Audio longer than ``stt_max_duration_s`` is not transcribed by either (chutes bills GPU seconds, and a long file
    outlasts the timeouts): it is saved by its caption, or as «Аудио: name» / «Голосовое сообщение».

    A transcript that is not speech (docs/features/iteration-4.md §1: ``RoutedSTT`` with ``screen`` already tried a
    second path) is kept for analysis — in ``transcript``, the tries in ``raw_json[UNHEARD_KEY]`` — and "" is
    returned; ``_is_unheard`` tells the caller (a re-run of the job reads the same from the stored raw).

    The paid call needs a budget check: the month's spend is read while the file downloads (``usage.prefetch_spend``),
    and the transcript is stored by a task in ``writes`` (``_gather``).
    """
    if raw.transcript is not None:
        return raw.transcript
    if raw.duration is not None and raw.duration > ctx.settings.stt_max_duration_s:
        log.info("message %s: %d s of audio is over the STT limit, not transcribed", raw.message_id, raw.duration)
        return ""
    # Every part of an album of audio files is transcribed in this one job (up to stt_timeout_s + local_stt_timeout_s
    # each), while the worker beats only between jobs: each part is progress for the jobs watchdog.
    ctx.watchdog.beat(JOBS_WATCH)
    usage.prefetch_spend(ctx.autocommit, prefs, clock.utcnow())
    audio = await _download(ctx, raw)
    if audio is None:
        return ""

    async def before_paid() -> None:
        await _check_budget(ctx, prefs, raws)

    try:
        with meter.ai():
            text = await ctx.stt.transcribe(
                audio,
                mime_type=raw.mime_type,
                language=prefs.stt_language,
                mode=prefs.stt_mode,
                before_paid=before_paid,
                screen=True,
                duration=raw.duration,
            )
    except GarbageTranscript as garbage:
        await _keep_unheard(ctx, raw, garbage)
        return ""
    text = text.strip()
    writes.append(asyncio.ensure_future(_store(ctx, raw, "transcript", text)))
    log.info("message %s transcribed (%d chars)", raw.message_id, len(text))
    return text


async def _store(ctx: AppContext, raw: RawMessage, column: str, value: Any) -> None:
    """One column of a raw message, committed at once (a transcript: a retry must not pay for it again)."""
    async with ctx.autocommit() as session:
        await session.execute(update(RawMessage).where(RawMessage.id == raw.id).values({column: value}))
        await session.commit()
    set_committed_value(raw, column, value)  # as stored: attaching the raw later writes nothing (``_attach``)


# --- voice notes that are not speech (docs/features/iteration-4.md §1) ------------------------------------------


async def _keep_unheard(ctx: AppContext, raw: RawMessage, garbage: GarbageTranscript) -> None:
    """Commit the rejected transcript and the tries (``UNHEARD_KEY``) right away, like a usable transcript: a re-run
    of the job does not pay for STT again."""
    marker = {"tries": garbage.tries}
    async with ctx.sessionmaker() as session:
        fresh = await session.get(RawMessage, raw.id)
        if fresh is not None:
            fresh.transcript = garbage.text
            fresh.raw_json = {**(fresh.raw_json or {}), UNHEARD_KEY: marker}
            await session.commit()
    set_committed_value(raw, "transcript", garbage.text)
    set_committed_value(raw, "raw_json", {**(raw.raw_json or {}), UNHEARD_KEY: marker})
    reasons = ", ".join(t.get("reason") or t.get("error") or "?" for t in garbage.tries)
    log.info("message %s: the transcript is not speech (%s), nothing saved", raw.message_id, reasons)


def _is_unheard(raw: RawMessage) -> bool:
    """The voice note's transcript was rejected as not speech (``UNHEARD_KEY``)."""
    return isinstance((raw.raw_json or {}).get(UNHEARD_KEY), dict)


def _job_of(raws: Sequence[RawMessage]) -> dict[str, Any]:
    """The job that processes ``raws`` again (``retry_unheard``): an album or one message."""
    primary = raws[0]
    if len(raws) > 1 and primary.media_group_id:
        return {"kind": "album", "payload": {"media_group_id": primary.media_group_id, "chat_id": primary.chat_id}}
    return {"kind": "message", "payload": {"raw_id": primary.id}}


async def _unheard(ctx: AppContext, raws: list[RawMessage], prefs: Prefs, job: dict[str, Any]) -> None:
    """No items: the raws are done, the first one keeps ``job`` (what [🔁 Ещё раз] queues again), the owner gets
    "🎤 Не разобрал голосовое — повтори или напиши текстом" with [🔁 Ещё раз] and 🤔."""
    primary = raws[0]
    async with ctx.sessionmaker() as session:
        fresh = await _reload(session, raws)
        if fresh:
            head = fresh[0]
            data = dict(head.raw_json or {})
            data[UNHEARD_KEY] = {**(data.get(UNHEARD_KEY) or {}), "job": job}
            head.raw_json = data
        _finish(fresh, None)
        _app_update(fresh, note=views.UNHEARD_TEXT)
        await session.commit()
    if not _is_app(raws):
        text, markup = views.unheard(primary.id)
        await _send(
            ctx,
            text,
            markup,
            prefs=prefs,
            reply_to=primary.message_id,
            purpose=sender.PURPOSE_UNHEARD,
            raw_message_id=primary.id,
        )
    await _react(ctx, raws, sender.REACT_ASK)
    ctx.scheduler_wakeup.set()


RETRY_QUEUED: Final = "queued"
RETRY_ALREADY: Final = "already"
RETRY_MISSING: Final = "missing"


async def retry_unheard(session: AsyncSession, raw_id: int) -> str:
    """[🔁 Ещё раз] under "🎤 Не разобрал…": the same voice note is transcribed again. The message (an album: all of
    its parts) forgets its transcripts and the rejection and goes back to ``new``, and the job that processed it
    (``UNHEARD_KEY`` "job") is queued again. ``RETRY_QUEUED``; ``RETRY_ALREADY`` when it was retried already (a
    second press: the raw row is locked, so it waits for the first one and then finds nothing to retry);
    ``RETRY_MISSING`` without the message. Does not commit."""
    raw = await session.get(RawMessage, raw_id, with_for_update=True, populate_existing=True)
    if raw is None:
        return RETRY_MISSING
    marker = (raw.raw_json or {}).get(UNHEARD_KEY)
    job = marker.get("job") if isinstance(marker, dict) else None
    if not isinstance(job, dict) or not isinstance(job.get("payload"), dict) or job.get("kind") not in HANDLERS:
        return RETRY_ALREADY
    raws = [raw]
    if raw.media_group_id:
        stmt = select(RawMessage).where(
            RawMessage.media_group_id == raw.media_group_id, RawMessage.chat_id == raw.chat_id, RawMessage.id != raw.id
        )
        raws += list((await session.scalars(stmt.with_for_update())).all())
    for part in raws:
        data = dict(part.raw_json or {})
        data.pop(UNHEARD_KEY, None)
        part.raw_json = data
        if part.content_type in VOICE_TYPES:
            part.transcript = None
        part.status = "new"
        part.processed_at = None
    # No dedupe key: the lock and the removed marker keep a double press from queueing it twice, and a voice note
    # rejected again may be retried again.
    await enqueue(session, str(job["kind"]), dict(job["payload"]))
    log.info("message %s: transcribing again", raw.message_id)
    return RETRY_QUEUED


async def _image(ctx: AppContext, raw: RawMessage, *, max_bytes: int) -> tuple[bytes, str] | None:
    """Picture bytes for the vision model; None when its type is not supported or it is over ``max_bytes``."""
    mime = "image/jpeg" if raw.content_type == "photo" else VISION_MIME.get((raw.mime_type or "").lower())
    if mime is None:
        log.info("message %s: picture type not supported by the vision model, not sent", raw.message_id)
        return None
    too_big = "message %s: picture of %d bytes is too large for the vision model, not sent"
    if raw.file_size is not None and raw.file_size > max_bytes:
        log.info(too_big, raw.message_id, raw.file_size)
        return None
    data = await _download(ctx, raw)
    if data and len(data) > max_bytes:  # the size was not known before the download
        log.info(too_big, raw.message_id, len(data))
        return None
    return (data, mime) if data else None


async def _download(ctx: AppContext, raw: RawMessage) -> bytes | None:
    """File bytes from Telegram; None when there is no file or it is over the 20 MB bot limit.

    Errors only report the exception type: their text may contain the file URL with the bot token.
    """
    if not raw.file_id:
        return None
    try:
        buf = await ctx.bot.download(raw.file_id)
    except TelegramBadRequest as e:
        if "too big" in (e.message or "").lower():
            log.warning("message %s: file too big to download, processing without it", raw.message_id)
            return None
        raise RetryLater(DOWNLOAD_RETRY, f"telegram download failed ({type(e).__name__})") from e
    except (TelegramAPIError, aiohttp.ClientError, TimeoutError, OSError) as e:
        raise RetryLater(DOWNLOAD_RETRY, f"telegram download failed ({type(e).__name__})") from e
    if buf is None:
        return None
    data = buf.getvalue() if isinstance(buf, io.BytesIO) else buf.read()
    return bytes(data) or None


async def _link_titles(
    ctx: AppContext, primary: RawMessage, urls: list[str], writes: list[asyncio.Task[None]]
) -> dict[str, str]:
    """Page titles of the links, fetched once and kept on the raw message (stored by a task in ``writes``)."""
    if not urls:
        return {}
    if primary.link_titles is not None:
        return {str(k): str(v) for k, v in primary.link_titles.items()}
    try:
        titles = await fetch_titles(urls)
    except Exception as e:  # titles are decoration: never fail the message for them
        log.info("link titles for message %s failed: %s", primary.message_id, type(e).__name__)
        titles = {}
    set_committed_value(primary, "link_titles", titles)  # what the job uses from now on, stored or not yet
    writes.append(asyncio.ensure_future(_store(ctx, primary, "link_titles", titles)))
    return titles


def _extract_input(
    content: _Content,
    primary: RawMessage,
    prefs: Prefs,
    *,
    clarification: str | None = None,
    reply_item: dict[str, Any] | None = None,
    related: list[dict[str, Any]] | None = None,
) -> ExtractInput:
    return ExtractInput(
        text=content.text,
        source=content.source,
        now=_msg_now(primary),
        prefs=prefs,
        images=content.images,
        forward_from=_forward_label(content.forward_origin),
        link_titles=content.link_titles,
        force_query=content.force_query,
        reply_item=reply_item,
        clarification=clarification,
        related=related or [],
    )


def _needs_ai(raw: RawMessage) -> bool:
    has_text = bool((raw.text or "").strip()) or bool(raw.urls)
    return has_text or raw.content_type in VOICE_TYPES or raw.content_type in IMAGE_TYPES


def _source_kind(raws: Sequence[RawMessage], urls: list[str]) -> SourceKind:
    """forward > voice > photo > link (has URLs) > text; other media with a caption -> other."""
    types = {r.content_type for r in raws}
    if any(r.forward_origin for r in raws):
        return "forward"
    if types & VOICE_TYPES:
        return "voice"
    if types & IMAGE_TYPES:
        return "photo"
    if urls:
        return "link"
    return "text" if types <= {"text"} else "other"


def _forward_label(origin: dict[str, Any] | None) -> str | None:
    """ "канал «Хабр»", "чат «Семья»", "Иван Петров" for the prompt."""
    if not origin:
        return None
    username = origin.get("username")
    name = origin.get("title") or origin.get("sender_name") or (f"@{username}" if username else None)
    kind = origin.get("type")
    if kind == "channel":
        return f"канал «{name}»" if name else "канал"
    if kind == "chat":
        return f"чат «{name}»" if name else "чат"
    return name or "скрытый пользователь"


def _source_meta(content: _Content) -> dict[str, Any] | None:
    """``Item.source``: ``{"forward_origin": {...}, "link_titles": {url: title}}`` (read by bot.views)."""
    meta: dict[str, Any] = {}
    if content.forward_origin:
        meta["forward_origin"] = content.forward_origin
    if content.link_titles:
        meta["link_titles"] = content.link_titles
    return meta or None


def _media_title(raw: RawMessage) -> str:
    data = raw.raw_json or {}
    match raw.content_type:
        case "document":
            return f"Документ: {raw.file_name}" if raw.file_name else "Документ"
        case "audio":
            return f"Аудио: {raw.file_name}" if raw.file_name else "Аудио"
        case "sticker":
            emoji = (data.get("sticker") or {}).get("emoji")
            return f"Стикер {emoji}" if emoji else "Стикер"
        case "contact":
            contact = data.get("contact") or {}
            name = " ".join(p for p in (contact.get("first_name"), contact.get("last_name")) if p)
            return f"Контакт: {name}" if name else "Контакт"
        case "location":
            venue = (data.get("venue") or {}).get("title")
            return f"Место: {venue}" if venue else "Геолокация"
        case other:
            return _MEDIA_TITLES.get(other, "Сообщение")


def _fallback_note(text: str, image_text: str | None) -> Extraction:
    """The model found nothing to save: keep the message as a note (the full text goes to source_text)."""
    body = text.strip() or (image_text or "").strip()
    first_line = next((line for line in body.splitlines() if line.strip()), "")
    return Extraction(items=[ExtractedItem(kind="note", title=_clip(first_line, TITLE_MAX) or "Без текста")])


def _image_text(ext: Extraction) -> str | None:
    if ext.image is None:
        return None
    parts = [p.strip() for p in (ext.image.text, ext.image.description) if p and p.strip()]
    return "\n".join(parts) or None


def _option_text(raw: RawMessage, payload: dict[str, Any]) -> str | None:
    """The chosen answer: option text, or an index into the question stored with the raw message."""
    stored = ((raw.raw_json or {}).get(ASK_KEY) or {}).get("options") or []
    option = payload.get("option")
    index = payload.get("n")
    if index is None and isinstance(option, int) and not isinstance(option, bool):
        index = option
    if index is not None:
        i = _opt_int(index)
        return str(stored[i]) if i is not None and 0 <= i < len(stored) else None
    return option.strip() if isinstance(option, str) and option.strip() else None


# --- budget / AI problems ---------------------------------------------------------------------------------------


async def _check_budget(
    ctx: AppContext, prefs: Prefs, raws: list[RawMessage], *, message_id: int | None = None
) -> None:
    """Exhausted -> defer until next month (notice once a month); >= 80 % -> notice once a month.

    The deferred messages are processed when the new month starts, but not before the quiet hours of that night end
    (their cards must not arrive at 00:00 on the 1st).
    """
    now = clock.utcnow()
    month = now.astimezone(prefs.zone).strftime("%Y-%m")
    notify = False
    # Read once per job (the check before a paid STT call and the one before extract() share it), with the job's own
    # calls not written yet (``usage.job_spend``).
    state = await usage.job_budget(ctx.autocommit, prefs, now)
    if state.exhausted or state.ratio >= BUDGET_WARN_RATIO:
        async with ctx.autocommit() as session:
            notify = await try_once(session, f"budget{100 if state.exhausted else 80}:{month}")
    if state.exhausted:
        resume = _budget_resume(now, prefs)
        if notify:
            await _send(ctx, _budget_exhausted_text(state, resume, prefs), prefs=prefs, purpose="notice")
        await _defer(ctx, raws, message_id=message_id)
        log.info("AI budget exhausted: job deferred to %s", resume.isoformat())
        raise RetryLater(reason=BUDGET_REASON, at=resume, count_attempt=False)
    if notify:
        await _send(ctx, _budget_warning_text(state), prefs=prefs, purpose="notice")


def _budget_resume(now: datetime, prefs: Prefs) -> datetime:
    """When jobs deferred by an exhausted budget run: the next month, after the quiet hours of that night."""
    return quiet.shift_out_of_quiet(usage.next_month_start(now, prefs), prefs)


async def split_budget_block(session: AsyncSession, prefs: Prefs, now: datetime) -> str | None:
    """Why [🪄 Разбить на шаги] cannot run now — the month's AI budget is spent (a toast) — or None. Checked before the
    job is queued: it would only wait until next month, with the owner told nothing but "Разбиваю на шаги…"."""
    state = await usage.budget_state(session, prefs, now)
    if not state.exhausted:
        return None
    if state.limit_usd <= 0:
        return "⛔ ИИ выключен (бюджет $0) — включи его в настройках, чтобы разбить на шаги."
    today = now.astimezone(prefs.zone).date()
    day = fmt_date(_budget_resume(now, prefs).astimezone(prefs.zone).date(), today=today)
    return f"⛔ Бюджет на ИИ исчерпан до {day} — увеличь его в настройках, чтобы разбить на шаги сейчас."


def _budget_warning_text(state: BudgetState) -> str:
    spent, limit = views.fmt_money(state.spent_usd), views.fmt_money(state.limit_usd)
    return f"⚠️ Израсходовано {state.ratio:.0%} месячного бюджета на ИИ: {spent} из {limit}."


def _budget_exhausted_text(state: BudgetState, resume: datetime, prefs: Prefs) -> str:
    day = fmt_date(resume.astimezone(prefs.zone).date())
    if state.limit_usd <= 0:
        head = "⛔ ИИ выключен: бюджет $0."
    else:
        spent, limit = views.fmt_money(state.spent_usd), views.fmt_money(state.limit_usd)
        head = f"⛔ Бюджет на ИИ за этот месяц исчерпан: {spent} из {limit}."
    return f"{head}\nСообщения сохраняю и разберу {day} — или раньше, если увеличить бюджет в /settings."


async def _defer(ctx: AppContext, raws: list[RawMessage], *, message_id: int | None = None) -> None:
    """Mark the raws deferred and react ✍ (once: not again while they stay deferred)."""
    first = True
    if raws:
        async with ctx.sessionmaker() as session:
            fresh = await _reload(session, raws)
            first = bool(fresh) and fresh[0].status != "deferred"
            for raw in fresh:
                if raw.status not in _FINISHED:
                    raw.status = "deferred"
            await session.commit()
    if not first:
        return
    if raws:
        await _react(ctx, raws, sender.REACT_DEFERRED)
    elif message_id:
        await sender.react(ctx, message_id, sender.REACT_DEFERRED)


async def _config_problem(
    ctx: AppContext, prefs: Prefs, error: AIConfigError, raws: list[RawMessage], *, message_id: int | None = None
) -> None:
    """chutes rejected the key/balance/model: tell the owner once a day, keep the messages deferred."""
    now = clock.utcnow()
    log.error("AI configuration problem: %s", describe_error(error))
    async with ctx.sessionmaker() as session:
        first = await try_once(session, f"aiconfig:{now.astimezone(prefs.zone).date().isoformat()}")
        await session.commit()
    if first:
        reason = views.clip(str(error), 200)
        text = (
            "⚠️ chutes.ai отклоняет запросы"
            + (f": <code>{views.esc(reason)}</code>" if reason else "")
            + ".\nПроверь ключ, баланс и ID моделей в .env. Сообщения сохранены — разберу их, как только заработает."
        )
        await _send(ctx, text, prefs=prefs, purpose="notice")
    await _defer(ctx, raws, message_id=message_id)


async def _give_up(
    ctx: AppContext,
    prefs: Prefs,
    raws: list[RawMessage],
    text: str,
    error: AIBadResponse,
    *,
    reply_to: int | None,
    app: bool = False,
) -> None:
    """``AIBadResponse``: the same request would give the same result, so the job ends now (no retries) like
    ``on_dead`` would end it: unfinished raws -> failed, ``text`` to the owner (kept with a message from the Mini
    App, ``app``: not sent), 😢."""
    reason = describe_error(error)
    log.warning("AI result unusable, giving up without retries: %s", reason)
    app = app or _is_app(raws)
    if raws:
        async with ctx.sessionmaker() as session:
            fresh = await _reload(session, raws)
            for raw in fresh:
                if raw.status not in _FINISHED:
                    raw.status = "failed"
                    raw.error = reason[:500]
            _app_update(fresh, note=text)
            await session.commit()
    await _send(
        ctx,
        _with_heard_text(text, raws),
        prefs=prefs,
        reply_to=reply_to,
        purpose="notice",
        raw_message_id=raws[0].id if raws else None,
        app=app,
    )
    if raws:
        await _react(ctx, raws, sender.REACT_FAILED)
    elif reply_to:
        await sender.react(ctx, reply_to, sender.REACT_FAILED)


# --- small helpers ----------------------------------------------------------------------------------------------


async def _prefs(ctx: AppContext) -> Prefs:
    """The prefs of the job: the ones loaded within the last minute (``AppContext.recent_prefs``), else from the
    database."""
    recent = ctx.recent_prefs()
    if recent is not None:
        return recent
    async with ctx.autocommit() as session:  # the first load inserts the defaults row (committed by itself)
        return await ctx.prefs(session)


async def _load_edit(ctx: AppContext, raw_id: int, key: str) -> tuple[RawMessage | None, bool]:
    """The raw of an edit job and whether its edit was applied already (its once-key ``key``), in one statement."""
    applied = exists().where(OnceKey.key == key)
    async with ctx.autocommit() as session:
        row = (await session.execute(select(RawMessage, applied).where(RawMessage.id == raw_id))).first()
    return (row[0], bool(row[1])) if row is not None else (None, False)


async def _snapshot(ctx: AppContext, item_id: int, prefs: Prefs, now: datetime) -> dict[str, Any]:
    """``items.item_snapshot`` in a session of its own."""
    async with ctx.autocommit() as session:
        return await items_svc.item_snapshot(session, item_id, prefs, now)


async def _nothing() -> None:
    return None


async def _load_message(ctx: AppContext, stmt: Select[tuple[RawMessage]]) -> tuple[list[RawMessage], bool]:
    """The raws of a message job (``stmt``) and whether its items were saved already: the job ran before and stopped
    between the save and marking the raws done (``_save``), so only that is left to do."""
    saved = exists().where(Item.raw_message_id == RawMessage.id, Item.parent_id.is_(None))
    async with ctx.autocommit() as session:
        rows = (await session.execute(stmt.add_columns(saved))).all()
    return [raw for raw, _ in rows], any(has_items for _, has_items in rows)


async def _reload(session: AsyncSession, raws: Sequence[RawMessage]) -> list[RawMessage]:
    """The same raws, attached to ``session`` (in the given order)."""
    return await _reload_ids(session, [r.id for r in raws])


def _attach(session: AsyncSession, raws: Sequence[RawMessage]) -> list[RawMessage]:
    """The raws this job loaded (and kept up to date with what it wrote since: transcripts, link titles), attached
    to ``session`` as they are — ``_reload`` without the round trip. Only this job changes an unfinished raw."""
    for raw in raws:
        session.add(raw)
    return list(raws)


def _by_message(raws: Sequence[RawMessage]) -> list[RawMessage]:
    """The raws in the order they were sent (an album's parts; a clarification lists the asked-about one first)."""
    return sorted(raws, key=lambda r: r.message_id)


async def _all(*steps: Awaitable[Any]) -> list[Any]:
    """All at once (each on a connection of its own: one database wait instead of several in a row), every one to its
    end — a database call is never cancelled half-way; then the first failure is raised."""
    results = await asyncio.gather(*steps, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


async def _together[A, B](first: Awaitable[A], second: Awaitable[B]) -> tuple[A, B]:
    """``_all`` of two, typed."""
    one, two = await _all(first, second)
    return one, two


async def _reload_ids(session: AsyncSession, ids: list[int]) -> list[RawMessage]:
    if not ids:
        return []
    found = {r.id: r for r in (await session.scalars(select(RawMessage).where(RawMessage.id.in_(ids)))).all()}
    return [found[i] for i in ids if i in found]


def _finish(raws: Sequence[RawMessage], image_text: str | None) -> None:
    now = clock.utcnow()
    for raw in raws:
        raw.status = "done"
        raw.processed_at = now
        raw.error = None
    if raws and image_text:
        raws[0].image_text = image_text


async def _done(ctx: AppContext, raws: Sequence[RawMessage], image_text: str | None) -> None:
    """``_finish`` for raws not attached to a session: one UPDATE, without a transaction around it."""
    if not raws:
        return
    primary = raws[0]
    now = clock.utcnow()
    values: dict[str, Any] = {"status": "done", "processed_at": now, "error": None}
    if image_text:
        values["image_text"] = case((RawMessage.id == primary.id, image_text), else_=RawMessage.image_text)
    async with ctx.autocommit() as session:
        await session.execute(update(RawMessage).where(RawMessage.id.in_([r.id for r in raws])).values(**values))
        await session.commit()
    for raw in raws:
        for key, value in (("status", "done"), ("processed_at", now), ("error", None)):
            set_committed_value(raw, key, value)
    if image_text:
        set_committed_value(primary, "image_text", image_text)


async def _mark_done(ctx: AppContext, raws: list[RawMessage], image_text: str | None, **app: Any) -> None:
    """Raws done; ``app`` fields go to ``raw_json[APP_KEY]`` of a message from the Mini App."""
    if not raws:
        return
    async with ctx.sessionmaker() as session:
        fresh = await _reload(session, raws)
        _finish(fresh, image_text)
        _app_update(fresh, **app)
        await session.commit()


def _is_app(raws: Sequence[RawMessage]) -> bool:
    """A message sent from the Mini App ("закинуть в свалку"): nothing about it goes to the chat."""
    return bool(raws) and raws[0].source == APP_SOURCE


def _app_update(raws: Sequence[RawMessage], **fields: Any) -> None:
    """Keep what the owner would have been told in the chat with a message from the Mini App
    (``raw_json[APP_KEY]``: ``note``, ``answer``, ``edited``); no-op for Telegram messages and without fields."""
    if not fields or not _is_app(raws):
        return
    head = raws[0]
    data = dict(head.raw_json or {})
    data[APP_KEY] = {**(data.get(APP_KEY) or {}), **fields}
    head.raw_json = data


async def _tell(ctx: AppContext, raws: list[RawMessage], text: str, *, prefs: Prefs, purpose: str) -> None:
    """A short reply to the message; for a message from the Mini App it is kept as its note instead."""
    if _is_app(raws):
        async with ctx.sessionmaker() as session:
            _app_update(await _reload(session, raws), note=text)
            await session.commit()
        return
    primary = raws[0]
    text = _with_heard_text(text, raws)
    await _send(ctx, text, prefs=prefs, reply_to=primary.message_id, purpose=purpose, raw_message_id=primary.id)


def _with_heard_text(text: str, raws: Sequence[RawMessage]) -> str:
    """A reply to a voice note with the "🎤 «…»" line on top (§6: the owner sees what a garbled transcript was)."""
    return views.with_heard((text, None), _heard(raws))[0]


async def _fail_raws(ctx: AppContext, job: Job) -> RawMessage | None:
    """Raw message(s) of a dead job -> failed (finished ones stay as they are); returns the first one."""
    payload = job.payload or {}
    async with ctx.sessionmaker() as session:
        raws: list[RawMessage] = []
        raw_id = _opt_int(payload.get("raw_id"))
        if raw_id is not None:
            raws = await _reload_ids(session, [raw_id])
        elif payload.get("media_group_id"):
            stmt = select(RawMessage).where(RawMessage.media_group_id == str(payload["media_group_id"]))
            raws = list((await session.scalars(stmt.order_by(RawMessage.message_id))).all())
        for raw in raws:
            if raw.status not in _FINISHED:
                raw.status = "failed"
                raw.error = (job.last_error or "")[:500] or None
        await session.commit()
    return raws[0] if raws else None


async def _send(
    ctx: AppContext,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    prefs: Prefs,
    app: bool = False,
    **kwargs: Any,
) -> int | None:
    """Best-effort send for messages that follow committed data: a failure is logged, not retried. Silent in quiet
    hours (``_quiet``). Nothing is sent for the Mini App (``app``): it reads the outcome from the job / raw message."""
    if app:
        return None
    try:
        return await sender.send(ctx, text, markup, silent=_quiet(prefs), quiet_checked=True, **kwargs)
    except Exception as e:
        log.warning("could not send %s message: %s", kwargs.get("purpose"), describe_error(e))
        return None


def _quiet(prefs: Prefs) -> bool:
    """Messages of the processor go out without a sound in quiet hours: a message deferred by the budget or an AI
    outage can be processed at night, and its card / question / answer must not ring then."""
    return quiet.in_quiet_hours(clock.utcnow(), prefs)


async def _react(ctx: AppContext, raws: Sequence[RawMessage], emoji: str) -> None:
    """The final reaction on every part of the message (the capture router put 👀 on each album part); none on a
    message from the Mini App (it is not in the chat). The AI calls' ai_usage rows that waited for the reply
    (``usage.deferred``) are written first, so the reaction still means that everything is stored."""
    await usage.write_pending(ctx.autocommit)
    for raw in raws:
        if raw.source != APP_SOURCE:
            await sender.react(ctx, raw.message_id, emoji)


def _clarified_key(raw_id: int) -> str:
    """once_keys marker of an applied clarification (not the job's dedupe key ``clarify:{raw_id}``)."""
    return f"clarify-done:{raw_id}"


def _said(raws: Sequence[RawMessage]) -> str:
    """What the owner said in a message: its texts and voice transcripts."""
    return _join(*(part for raw in raws for part in (raw.text, raw.transcript)))


def _heard(raws: Sequence[RawMessage]) -> str | None:
    """What the bot heard in the voice notes / audio files among ``raws`` (their transcripts, in order) — the
    "🎤 «…»" line of every reply to them (docs/features/iteration-3.md §6); None without any."""
    texts = [r.transcript.strip() for r in raws if r.content_type in VOICE_TYPES and r.transcript]
    return " ".join(t for t in texts if t) or None


def _msg_now(raw: RawMessage) -> datetime:
    dt = raw.tg_date
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _payload_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _strip_question_mark(text: str) -> str:
    stripped = text.lstrip()
    return stripped[1:].lstrip() if stripped.startswith("?") else text


def _join(*parts: str | None) -> str:
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
