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

Data changes are committed before their cards are sent (a lost card never duplicates items); replies that change
nothing (answers, candidate lists) are sent before the raw message is marked done, so a Telegram error retries them.
Everything sent in quiet hours is silent (a deferred message can be processed at night); the final reaction goes on
every part of an album.

The Mini App (docs/features/iteration-2.md §D): a message from the app (``RawMessage.source == "app"``) goes through
the same pipeline, but nothing about it is sent to the chat (no cards, reactions, questions, answers): its note, the
answer to a question and the edited items are kept in ``raw_json[APP_KEY]`` (the question and the candidates under
the usual keys) for ``GET /api/inbox/{raw_id}``. Jobs "edit_time" and "split" with ``app: true`` in the payload stay
out of the chat too and return their outcome (``{"ok", "reason", "message", …}``), which the worker keeps in
``Job.result``. ``ask_question`` answers ``POST /api/ask`` (the "?" path without a saved message).
Message text, transcripts and OCR are never logged at INFO or above.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import aiohttp
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot import clock
from dumpbot.ai.client import AIBadResponse, AIConfigError, AIUnavailable
from dumpbot.ai.extract import ExtractInput, ExtractOutput, SplitInput, SplitOutput, answer_query, extract, split_steps
from dumpbot.ai.linkinfo import fetch_titles
from dumpbot.ai.schemas import Ask, EditPatch, ExtractedItem, Extraction, QuerySpec, When
from dumpbot.bot import cards, sender, views
from dumpbot.context import AppContext
from dumpbot.db.models import Job, OnceKey, RawMessage
from dumpbot.domain import quiet, timeresolve
from dumpbot.domain.fmt import fmt_date
from dumpbot.domain.kinds import ACTIONABLE, SourceKind
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import BudgetState, CreatedItem, PatchResult, Row
from dumpbot.services import items as items_svc
from dumpbot.services import query, usage
from dumpbot.services.ingest import APP_KEY, APP_SOURCE, ASK_KEY, CANDIDATES_KEY
from dumpbot.workers.jobs import BUDGET_REASON, Handler, RetryLater, describe_error, try_once
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
# Edits by target that wait for the owner to pick the item even when the search found only one.
CONFIRM_ACTIONS: Final = frozenset({"delete", "done", "no_reminder", "stop_recurrence"})

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

    @property
    def empty(self) -> bool:
        return not self.text.strip() and not self.images


# --- handlers ---------------------------------------------------------------------------------------------------


async def handle_message(ctx: AppContext, payload: dict[str, Any]) -> None:
    """Job "message" ``{raw_id}``: process one saved message."""
    raws = await _load_raws(ctx, [int(payload["raw_id"])])
    if not raws or raws[0].status in _FINISHED:
        return
    await _process(ctx, raws)


async def handle_album(ctx: AppContext, payload: dict[str, Any]) -> None:
    """Job "album" ``{media_group_id, chat_id?}``: all unfinished parts as one message (captions + up to 4
    pictures); the items belong to the first part, every part ends up done."""
    stmt = select(RawMessage).where(
        RawMessage.media_group_id == str(payload["media_group_id"]), RawMessage.status.not_in(_FINISHED)
    )
    if payload.get("chat_id") is not None:
        stmt = stmt.where(RawMessage.chat_id == int(payload["chat_id"]))
    async with ctx.sessionmaker() as session:
        raws = list((await session.scalars(stmt.order_by(RawMessage.message_id))).all())
    if raws:
        await _process(ctx, raws)


async def handle_clarify(ctx: AppContext, payload: dict[str, Any]) -> None:
    """Job "clarify" ``{raw_id, option}``: extract again with the chosen answer and replace the raw's items.

    ``option`` is the option text, or its index (int) in the stored question; ``n`` (int) is accepted too.
    Applied at most once per raw message: a once-key is committed together with the new items, so a re-run of
    the job (a restart after that commit) neither pays the AI again nor replaces the items a second time.
    """
    async with ctx.sessionmaker() as session:
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

    The patch is applied at most once per (raw, item): a once-key is committed together with it.
    """
    raw_id, item_id = int(payload["raw_id"]), int(payload["item_id"])
    key = f"edit:{raw_id}:{item_id}"
    async with ctx.sessionmaker() as session:
        raw = await session.get(RawMessage, raw_id)
        applied = await session.get(OnceKey, key) is not None
    if raw is None or applied or raw.status == "ignored":
        return
    if raw.status == "done" and raw.reply_item_id == item_id:  # answered as a "?" query on an earlier run
        return
    prefs = await _prefs(ctx)
    now = _msg_now(raw)
    try:
        # A picked candidate re-reads an already processed message: reuse its OCR text instead of the pictures.
        content = await _gather(ctx, [raw], prefs, with_images=not raw.image_text)
        await _check_budget(ctx, prefs, [raw])
        async with ctx.sessionmaker() as session:
            snapshot = await items_svc.item_snapshot(session, item_id, prefs, now)
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

    patch = out.extraction.edit or EditPatch()
    image_text = _image_text(out.extraction)
    if patch.action == "split":  # "разбей на шаги" in reply to the card
        await _split(ctx, prefs, item_id, now=now, note=content.text, raws=[raw], once_key=key, image_text=image_text)
        return
    async with ctx.sessionmaker() as session:
        if not await try_once(session, key):
            return  # applied concurrently
        result = await items_svc.apply_patch(session, item_id, patch, prefs, now)
        view = await _patch_view(session, result, prefs)
        fresh = await _reload(session, [raw])
        _finish(fresh, image_text)
        _app_update(fresh, edited=[item_id])
        await session.commit()
        events = items_svc.take_parent_events(session)
    if not _is_app([raw]):
        await _send_patch_card(ctx, prefs, view, result, reply_to=raw.message_id, raw_message_id=raw.id)
    await cards.after_steps(ctx, events, prefs, clock.utcnow(), silent=_quiet(prefs))
    await _react(ctx, [raw], sender.REACT_SAVED)
    ctx.scheduler_wakeup.set()


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
            await _show_split(ctx, prefs, item_id, problem, reply_to=reply_to, raw_id=raw_id)
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
    """The checklist in the chat: the button's card redrawn in place, else a new card (``why`` above it)."""
    reply_to = raws[0].message_id if raws else card_message
    raw_id = raws[0].id if raws else None
    if created and card_message is not None and not raws:
        await _redraw_card(ctx, prefs, item_id, card_message)
    else:
        await _show_split(ctx, prefs, item_id, why, reply_to=reply_to, raw_id=raw_id)


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
    ctx: AppContext, prefs: Prefs, item_id: int, note: str | None, *, reply_to: int | None, raw_id: int | None = None
) -> None:
    """A new card of the item (with ``note`` above it) in reply to ``reply_to``."""
    async with ctx.sessionmaker() as session:
        item = await items_svc.get_item(session, item_id)
        if item is None:
            return
        text, markup = await cards.card_view(session, item, prefs, clock.utcnow())
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


async def _process(ctx: AppContext, raws: list[RawMessage], *, clarification: str | None = None) -> None:
    primary = raws[0]
    prefs = await _prefs(ctx)
    replace = clarification is not None
    if not replace and not any(_needs_ai(r) for r in raws):
        await _save_without_ai(ctx, raws, prefs)
        return
    content: _Content | None = None
    try:
        # A clarification reuses the stored OCR text instead of paying for the pictures again.
        content = await _gather(ctx, raws, prefs, with_images=not (replace and primary.image_text))
        if content.empty and not replace:
            await _save_without_ai(ctx, raws, prefs)
            return
        await _check_budget(ctx, prefs, raws)
        candidates = await _related_candidates(ctx, raws, content, prefs)
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
    """``extract()``; when the fallback stood in for a missing (404) primary model, tell the owner once a day."""
    out = await extract(ctx.ai, ctx.settings, inp)
    if out.missing_model:
        async with ctx.sessionmaker() as session:
            first = await try_once(session, f"aimodel:{clock.utcnow().astimezone(prefs.zone).date().isoformat()}")
            await session.commit()
        if first:
            text = (
                f"⚠️ Модели <code>{views.esc(out.missing_model)}</code> нет на chutes.ai (404), "
                f"пока работаю на запасной <code>{views.esc(out.model)}</code>.\n"
                "Проверь LLM_MODEL в .env."
            )
            await _send(ctx, text, prefs=prefs, purpose="notice")
    return out


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
    async with ctx.sessionmaker() as session:
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
    async with ctx.sessionmaker() as session:
        fresh = await _reload(session, raws)
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
        shown = [] if quiet_chat else await _cards(session, created, prefs, now_view, every=note is not None)
        if ask is not None:
            head.raw_json = {**(head.raw_json or {}), ASK_KEY: ask.model_dump(mode="json")}
        _finish(fresh, image_text)
        _app_update(fresh, note=note)
        await session.commit()

    if ask is not None:
        if not _is_app(raws):  # the app shows the question from raw_json and answers it with POST …/answer
            await _send_ask(ctx, prefs, raws, ask)
        return
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
    await _react(ctx, raws, sender.REACT_SAVED)


async def _cards(
    session: AsyncSession, created: Sequence[CreatedItem], prefs: Prefs, now: datetime, *, every: bool = False
) -> list[tuple[CreatedItem, View]]:
    """Cards for items with reminders, actionable kinds, a checklist or related items, or something to confirm
    (``every``: all items); the rest only get 👌."""
    out: list[tuple[CreatedItem, View]] = []
    for c in created:
        res = c.resolution
        extras = await query.card_extras(session, c.item)
        shown = every or c.reminders or c.item.kind in ACTIONABLE or res.needs_confirm or res.warnings
        if shown or extras.steps or extras.related:
            view = views.item_card(c.item, c.occurrence, c.reminders, prefs, now, warnings=res.warnings, extras=extras)
            out.append((c, view))
    return out


async def _send_ask(ctx: AppContext, prefs: Prefs, raws: list[RawMessage], ask: Ask) -> None:
    primary = raws[0]
    text, markup = views.ask_message(ask, primary.id)
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
    await sender.send(
        ctx,
        text,
        markup,
        reply_to=primary.message_id,
        silent=_quiet(prefs),
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
    async with ctx.sessionmaker() as session:
        rows = await query.run_query(session, spec, prefs, now)
    text: str | None = None
    if spec.answer == "summary":
        try:
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
    """ "перенеси встречу с Олегом на 16": the item is looked up by ``edit.target``. Only an exact match (every word
    of the target) of a non-destructive change is applied at once; a loose match (some word, a tag), several
    matches, or delete / done / no_reminder / stop_recurrence -> the owner picks the item (buttons, or a reply with
    its number), even when there is one. Nothing found -> the message is kept as a note and its card says so: it may
    be a new task worded like an edit («удалить приложение банка»)."""
    primary = raws[0]
    patch = out.extraction.edit or EditPatch()
    image_text = _image_text(out.extraction)
    now = _msg_now(primary)
    target = patch.target or ""
    async with ctx.sessionmaker() as session:
        rows = await query.find_edit_candidates(session, target, prefs, now, exact=True)
        exact = bool(rows)
        if not exact:
            rows = await query.find_edit_candidates(session, target, prefs, now)
    if not rows:
        await _save(ctx, raws, content, out, prefs, replace=replace, note=NOT_FOUND_TEXT, related=related)
        return
    if len(rows) > 1 or not exact or patch.action in CONFIRM_ACTIONS:
        await _send_candidates(ctx, prefs, raws, rows, image_text)
        return
    if patch.action == "split":  # "разбей переезд на шаги"
        await _split(ctx, prefs, rows[0].item_id, now=now, note=content.text, raws=raws, image_text=image_text)
        return
    async with ctx.sessionmaker() as session:
        result = await items_svc.apply_patch(session, rows[0].item_id, patch, prefs, now)
        view = await _patch_view(session, result, prefs)
        fresh = await _reload(session, raws)
        _finish(fresh, image_text)
        _app_update(fresh, edited=[rows[0].item_id])
        await session.commit()
        events = items_svc.take_parent_events(session)
    if not _is_app(raws):
        await _send_patch_card(ctx, prefs, view, result, reply_to=primary.message_id, raw_message_id=primary.id)
    await cards.after_steps(ctx, events, prefs, clock.utcnow(), silent=_quiet(prefs))
    await _react(ctx, raws, sender.REACT_SAVED)


async def _send_candidates(
    ctx: AppContext, prefs: Prefs, raws: list[RawMessage], rows: Sequence[Row], image_text: str | None
) -> None:
    """ "✏️ Какую запись изменить?" with a button per item (PickCb -> job "edit"); the ids are kept with the raw so
    that a text reply "2" picks the second one. Sent before the raw is marked done."""
    primary = raws[0]
    rows = list(rows)[: views.CANDIDATES_MAX]
    async with ctx.sessionmaker() as session:
        [head] = await _reload(session, [primary])
        head.raw_json = {**(head.raw_json or {}), CANDIDATES_KEY: [r.item_id for r in rows]}
        await session.commit()
    if _is_app(raws):  # the app lists them from raw_json and picks with POST /api/inbox/{raw_id}/pick
        await _mark_done(ctx, raws, image_text)
        return
    text, markup = views.edit_candidates(rows, primary.id, prefs, clock.utcnow())
    await sender.send(
        ctx,
        text,
        markup,
        reply_to=primary.message_id,
        silent=_quiet(prefs),
        purpose=sender.PURPOSE_PICK,
        raw_message_id=primary.id,
    )
    await _mark_done(ctx, raws, image_text)
    await _react(ctx, raws, sender.REACT_ASK)


async def _patch_view(session: AsyncSession, result: PatchResult, prefs: Prefs) -> View:
    return await cards.patch_card(session, result, prefs, clock.utcnow())


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


async def _gather(ctx: AppContext, raws: list[RawMessage], prefs: Prefs, *, with_images: bool) -> _Content:
    """Texts, transcripts and pictures of the raws (in message order), their URLs, forward origin, link titles."""
    primary = raws[0]
    texts: list[str] = []
    urls: list[str] = []
    images: list[tuple[bytes, str]] = []
    origin: dict[str, Any] | None = None
    for raw in raws:
        if raw.text and raw.text.strip():
            texts.append(raw.text.strip())
        urls.extend(u for u in raw.urls or [] if u not in urls)
        origin = origin or raw.forward_origin
        if raw.content_type in VOICE_TYPES:
            transcript = await _transcript(ctx, raw, prefs, raws)
            if transcript:
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
        link_titles=await _link_titles(ctx, primary, urls),
        force_query=force_query,
    )


async def _transcript(ctx: AppContext, raw: RawMessage, prefs: Prefs, raws: list[RawMessage]) -> str:
    """Stored transcript, or download + STT and commit it right away (a retry must not pay twice).

    STT goes to chutes and / or the local Whisper in the order of ``prefs.stt_mode``; the budget is checked only
    before chutes (paid; exhausted -> ``raws`` deferred, ``RetryLater``), the local Whisper is free.
    Audio longer than ``stt_max_duration_s`` is not transcribed by either (chutes bills GPU seconds, and a long file
    outlasts the timeouts): it is saved by its caption, or as «Аудио: name» / «Голосовое сообщение».
    """
    if raw.transcript is not None:
        return raw.transcript
    if raw.duration is not None and raw.duration > ctx.settings.stt_max_duration_s:
        log.info("message %s: %d s of audio is over the STT limit, not transcribed", raw.message_id, raw.duration)
        return ""
    # Every part of an album of audio files is transcribed in this one job (up to stt_timeout_s + local_stt_timeout_s
    # each), while the worker beats only between jobs: each part is progress for the jobs watchdog.
    ctx.watchdog.beat(JOBS_WATCH)
    audio = await _download(ctx, raw)
    if audio is None:
        return ""

    async def before_paid() -> None:
        await _check_budget(ctx, prefs, raws)

    text = await ctx.stt.transcribe(
        audio, mime_type=raw.mime_type, language=prefs.stt_language, mode=prefs.stt_mode, before_paid=before_paid
    )
    text = text.strip()
    async with ctx.sessionmaker() as session:
        await session.execute(update(RawMessage).where(RawMessage.id == raw.id).values(transcript=text))
        await session.commit()
    raw.transcript = text
    log.info("message %s transcribed (%d chars)", raw.message_id, len(text))
    return text


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


async def _link_titles(ctx: AppContext, primary: RawMessage, urls: list[str]) -> dict[str, str]:
    """Page titles of the links, fetched once and kept on the raw message."""
    if not urls:
        return {}
    if primary.link_titles is not None:
        return {str(k): str(v) for k, v in primary.link_titles.items()}
    try:
        titles = await fetch_titles(urls)
    except Exception as e:  # titles are decoration: never fail the message for them
        log.info("link titles for message %s failed: %s", primary.message_id, type(e).__name__)
        titles = {}
    async with ctx.sessionmaker() as session:
        await session.execute(update(RawMessage).where(RawMessage.id == primary.id).values(link_titles=titles))
        await session.commit()
    primary.link_titles = titles
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
    async with ctx.sessionmaker() as session:
        state = await usage.budget_state(session, prefs, now)
        if state.exhausted:
            notify = await try_once(session, f"budget100:{month}")
        elif state.ratio >= BUDGET_WARN_RATIO:
            notify = await try_once(session, f"budget80:{month}")
        await session.commit()
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
        text,
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
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        await session.commit()  # the first load inserts the defaults row
    return prefs


async def _load_raws(ctx: AppContext, ids: list[int]) -> list[RawMessage]:
    async with ctx.sessionmaker() as session:
        return await _reload_ids(session, ids)


async def _reload(session: AsyncSession, raws: Sequence[RawMessage]) -> list[RawMessage]:
    """The same raws, attached to ``session`` (in the given order)."""
    return await _reload_ids(session, [r.id for r in raws])


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
    await _send(ctx, text, prefs=prefs, reply_to=primary.message_id, purpose=purpose, raw_message_id=primary.id)


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
        return await sender.send(ctx, text, markup, silent=_quiet(prefs), **kwargs)
    except Exception as e:
        log.warning("could not send %s message: %s", kwargs.get("purpose"), describe_error(e))
        return None


def _quiet(prefs: Prefs) -> bool:
    """Messages of the processor go out without a sound in quiet hours: a message deferred by the budget or an AI
    outage can be processed at night, and its card / question / answer must not ring then."""
    return quiet.in_quiet_hours(clock.utcnow(), prefs)


async def _react(ctx: AppContext, raws: Sequence[RawMessage], emoji: str) -> None:
    """The final reaction on every part of the message (the capture router put 👀 on each album part); none on a
    message from the Mini App (it is not in the chat)."""
    for raw in raws:
        if raw.source != APP_SOURCE:
            await sender.react(ctx, raw.message_id, emoji)


def _clarified_key(raw_id: int) -> str:
    """once_keys marker of an applied clarification (not the job's dedupe key ``clarify:{raw_id}``)."""
    return f"clarify-done:{raw_id}"


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
