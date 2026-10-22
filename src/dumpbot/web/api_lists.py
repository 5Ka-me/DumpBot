"""Read-mostly endpoints of the Mini App API: who am I, the feed, today / tasks / someday / recurring / week (and
«📅 Перенести невыполненное на завтра» of a day), a question to the base, settings, AI spend, the state of a background
job (docs/features/miniapp-api.md).

Each is a thin layer over ``services.query`` / ``services.processor`` / the settings dialog's validation; the first
read of the prefs creates their row, so read sessions commit too.
"""

from __future__ import annotations

import base64
import binascii
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web
from pydantic import ValidationError

from dumpbot import clock
from dumpbot.ai.client import AIBadResponse, AIConfigError, AIUnavailable
from dumpbot.bot import views
from dumpbot.bot.routers.settings import parse_money, parse_time, parse_tz, save_changes
from dumpbot.db.models import Job
from dumpbot.domain.kinds import KIND_EMOJI, KIND_LABELS, KINDS
from dumpbot.domain.prefs import Prefs
from dumpbot.services import digest, events, processor, query
from dumpbot.web import serialize
from dumpbot.web.auth import USER
from dumpbot.web.http import (
    NUL_TEXT,
    ApiError,
    bad_request,
    ctx_of,
    has_nul,
    journal_also,
    json_response,
    path_id,
    read_json,
)
from dumpbot.workers.jobs import BUDGET_REASON, RetryLater

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


FEED_LIMIT: Final = 30
FEED_MAX: Final = 100
LIST_MAX: Final = 200
WEEK_DAYS: Final = 7
QUESTION_MAX: Final = 1000
SEARCH_MAX: Final = 200
TAGS_MAX: Final = 10
CURSOR_ID_DIGITS: Final = 18  # ids are bigint: more digits may not fit
# Preferences that no longer exist, skipped in PATCH /api/settings (an older app saves its whole form):
# lead_minutes — "remind N minutes before", dropped 2026-10-07 (an item's time is its notification time).
RETIRED_PREFS: Final = frozenset({"lead_minutes"})

BUDGET_TEXT: Final = "⛔ Бюджет на ИИ исчерпан — увеличь его в настройках или подожди следующего месяца."
AI_UNAVAILABLE_TEXT: Final = "✍ ИИ сейчас недоступен — попробуй чуть позже."
AI_CONFIG_TEXT: Final = "⚠️ chutes.ai отклоняет запросы — проверь ключ, баланс и модели в .env."
AI_FAILED_TEXT: Final = "😢 Не смог разобрать вопрос — попробуй сформулировать иначе."

Loader = Callable[["AsyncSession", Prefs, datetime], Awaitable[Any]]


def routes() -> list[web.RouteDef]:
    return [
        web.get("/api/me", me),
        web.get("/api/feed", feed),
        web.get("/api/today", today),
        web.post("/api/today/move", move_today),
        web.get("/api/tasks", tasks),
        web.get("/api/someday", someday),
        web.get("/api/recurring", recurring),
        web.get("/api/week", week),
        web.post("/api/ask", ask),
        web.get("/api/settings", get_settings),
        web.patch("/api/settings", patch_settings),
        web.get("/api/usage", usage),
        web.get("/api/jobs/{id}", job_status),
    ]


async def _read(request: web.Request, load: Loader) -> web.Response:
    """``load(session, prefs, now)`` in one session, as JSON."""
    ctx = ctx_of(request)
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        data = await load(session, prefs, now)
        await session.commit()  # the first read of the prefs inserts their row
    return json_response(data)


# --- who am I ------------------------------------------------------------------------------------------------------


async def me(request: web.Request) -> web.Response:
    """The owner (from initData), the timezone and the categories with their labels."""
    user = request[USER]
    local_stt = bool(ctx_of(request).settings.local_stt_url.strip())

    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> dict[str, Any]:
        return {
            "user": {
                "id": user.id,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "username": user.username,
            },
            "tz": prefs.tz,
            "now": serialize.iso(now),
            "today": serialize.day(now.astimezone(prefs.zone).date()),
            "kinds": [{"kind": k, "label": KIND_LABELS[k], "emoji": KIND_EMOJI[k]} for k in KINDS],
            "local_stt": local_stt,
        }

    return await _read(request, load)


# --- feed and lists ------------------------------------------------------------------------------------------------


async def feed(request: web.Request) -> web.Response:
    """Items newest first, filtered (q, kinds, tags, status, from, to), one page per ``cursor``."""
    params = request.query
    for name, value in params.items():
        if has_nul(value):
            raise bad_request(NUL_TEXT, name)
    text = params.get("q", "").strip()
    if len(text) > SEARCH_MAX:
        raise bad_request(f"Поиск — не длиннее {SEARCH_MAX} символов", "q")
    kinds = _csv(params.get("kinds", ""))
    if any(k not in KINDS for k in kinds):
        raise bad_request("Неизвестная категория", "kinds")
    tags = list(dict.fromkeys(t.lstrip("#").lower() for t in _csv(params.get("tags", "")) if t.lstrip("#")))
    if len(tags) > TAGS_MAX:
        raise bad_request(f"Не больше {TAGS_MAX} тегов", "tags")
    status = params.get("status", "any") or "any"
    if status not in query.FEED_STATUSES:
        raise bad_request("Статус: any, open, done или deleted", "status")
    created_from, created_to = _date(params, "from"), _date(params, "to")
    before = _cursor(params.get("cursor", ""))
    limit = _limit(params, FEED_LIMIT, FEED_MAX)

    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> dict[str, Any]:
        found = await query.feed(
            session,
            prefs,
            text=text,
            kinds=kinds,
            tags=tags,
            status=status,
            created_from=created_from,
            created_to=created_to,
            before=before,
            limit=limit,
        )
        last = found[-1] if len(found) == limit else None
        return {
            "rows": await serialize.rows(session, found, prefs, now),
            "next_cursor": encode_cursor(last.created_at, last.item_id) if last is not None else None,
        }

    return await _read(request, load)


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _date(params: Any, name: str) -> date | None:
    value = params.get(name, "")
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise bad_request("Дата — в формате ГГГГ-ММ-ДД", name) from None


def _limit(params: Any, default: int, maximum: int) -> int:
    value = params.get("limit", "")
    if not value:
        return default
    if not value.isdecimal() or not 1 <= int(value) <= maximum:
        raise bad_request(f"limit — от 1 до {maximum}", "limit")
    return int(value)


def encode_cursor(created_at: datetime, item_id: int) -> str:
    """Opaque keyset cursor of the feed: the last row's (created_at, id)."""
    raw = f"{created_at.isoformat()}|{item_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor(value: str) -> tuple[datetime, int] | None:
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        at, _, item_id = raw.partition("|")
        moment = datetime.fromisoformat(at)
        if moment.tzinfo is None or not item_id.isdecimal() or len(item_id) > CURSOR_ID_DIGITS:
            raise ValueError("bad cursor")
        return moment.astimezone(UTC), int(item_id)
    except (ValueError, OverflowError, UnicodeDecodeError, binascii.Error):  # OverflowError: year 1 at UTC+14
        raise bad_request("Неверный курсор", "cursor") from None


async def today(request: web.Request) -> web.Response:
    """Today's agenda: open (by time), overdue (30 days), done, the running spans."""

    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> dict[str, Any]:
        return await serialize.agenda(session, await query.agenda(session, prefs, now), prefs, now)

    return await _read(request, load)


async def move_today(request: web.Request) -> web.Response:
    """``{day?: "YYYY-MM-DD"}`` (default: today; yesterday too, for a list opened before midnight): what is already due
    and not done that day, and the overdue, goes to the next day — the evening digest's «📅 Перенести невыполненное на
    завтра». → ``{moved, message}``."""
    body = await read_json(request)
    unknown = [key for key in body if key != "day"]
    if unknown:
        raise bad_request("Можно передать только day", unknown[0])
    ctx = ctx_of(request)
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        day = _move_day(body.get("day"), now.astimezone(prefs.zone).date())
        moved = await digest.move_not_done(session, day, prefs, now)
        await session.commit()
    if moved:
        ctx.scheduler_wakeup.set()
    journal_also(request, *events.moved_entries(moved, events.SRC_MINIAPP))
    message = f"📅 Перенёс на завтра: {len(moved)}" if moved else "Нечего переносить"
    return json_response({"moved": len(moved), "message": message})


def _move_day(value: Any, today: date) -> date:
    if value is None:
        return today
    try:
        day = date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        day = None
    if day is None or not today - timedelta(days=1) <= day <= today:
        raise bad_request("День — сегодня или вчера, ГГГГ-ММ-ДД", "day")
    return day


async def week(request: web.Request) -> web.Response:
    """Agendas of today and the next six days (overdue only for today)."""

    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> dict[str, Any]:
        days = [await query.agenda(session, prefs, now, day_offset=offset) for offset in range(WEEK_DAYS)]
        return {"days": [await serialize.agenda(session, a, prefs, now) for a in days]}

    return await _read(request, load)


async def tasks(request: web.Request) -> web.Response:
    """Open actionable items (not "когда-нибудь"): with a time first, then the newest."""
    limit = _limit(request.query, LIST_MAX, LIST_MAX)

    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> dict[str, Any]:
        found = await query.open_tasks(session, prefs, now, limit=limit)
        return {"rows": await serialize.rows(session, found, prefs, now)}

    return await _read(request, load)


async def someday(request: web.Request) -> web.Response:
    """Open "когда-нибудь" items, newest first."""
    limit = _limit(request.query, LIST_MAX, LIST_MAX)

    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> dict[str, Any]:
        found = await query.someday_items(session, limit=limit)
        return {"rows": await serialize.rows(session, found, prefs, now)}

    return await _read(request, load)


async def recurring(request: web.Request) -> web.Response:
    """Active series with their next open occurrence (``repeat_text``: "каждый пн")."""

    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> dict[str, Any]:
        return {"rows": await serialize.rows(session, await query.recurring(session), prefs, now)}

    return await _read(request, load)


# --- a question to the base ----------------------------------------------------------------------------------------


async def ask(request: web.Request) -> web.Response:
    """``{question}`` -> the "?" path: QuerySpec by the AI, its rows, a prose answer when it asked for one."""
    body = await read_json(request)
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        raise bad_request("Напиши вопрос", "question")
    question = question.strip().lstrip("?").strip() or question.strip()
    if len(question) > QUESTION_MAX:
        raise bad_request(f"Вопрос — не длиннее {QUESTION_MAX} символов", "question")
    ctx = ctx_of(request)
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        await session.commit()
    try:
        found = await processor.ask_question(ctx, question, prefs, now)
    except RetryLater:
        raise ApiError(402, "budget_exhausted", BUDGET_TEXT) from None
    except AIUnavailable:
        raise ApiError(503, "ai_unavailable", AI_UNAVAILABLE_TEXT) from None
    except AIConfigError:
        raise ApiError(502, "ai_config", AI_CONFIG_TEXT) from None
    except AIBadResponse:
        raise ApiError(502, "ai_failed", AI_FAILED_TEXT) from None
    async with ctx.sessionmaker() as session:
        rows = await serialize.rows(session, found.rows, prefs, clock.utcnow())
    return json_response(
        {"answer": found.text, "title": found.title, "rows": rows, "query": found.spec.model_dump(mode="json")}
    )


# --- settings ------------------------------------------------------------------------------------------------------


def prefs_json(prefs: Prefs) -> dict[str, Any]:
    """Every preference; times as "HH:MM"."""
    out: dict[str, Any] = {}
    for name, value in prefs.model_dump().items():
        out[name] = serialize.hhmm(value) if hasattr(value, "strftime") else value
    return out


async def get_settings(request: web.Request) -> web.Response:
    local_stt = bool(ctx_of(request).settings.local_stt_url.strip())

    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> dict[str, Any]:
        return {"prefs": prefs_json(prefs), "local_stt": local_stt}

    return await _read(request, load)


async def patch_settings(request: web.Request) -> web.Response:
    """Some preferences, validated like the bot's settings dialog (HH:MM, an IANA zone, minutes, dollars) and by
    ``Prefs`` itself. Saved together; a raised budget releases the jobs that waited for it. ``RETIRED_PREFS`` are
    skipped."""
    body = await read_json(request)
    if not body:
        raise bad_request("Нечего менять")
    ctx = ctx_of(request)
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        changes = {name: _pref_value(prefs, name, value) for name, value in body.items() if name not in RETIRED_PREFS}
        try:
            new = Prefs.model_validate({**prefs.model_dump(), **changes})
        except ValidationError as e:
            loc = e.errors()[0].get("loc") or ("",)
            raise bad_request("Недопустимое значение", str(loc[0])) from None
        budget_raised = await save_changes(session, ctx.owner_id, prefs, new, now)
        await session.commit()
    ctx.scheduler_wakeup.set()
    if budget_raised:
        ctx.jobs_wakeup.set()
    return json_response({"prefs": prefs_json(new), "local_stt": bool(ctx.settings.local_stt_url.strip())})


def _pref_value(prefs: Prefs, name: str, value: Any) -> Any:
    """One typed value of a preference (ApiError 400 naming the field when it does not fit)."""
    if name not in Prefs.model_fields:
        raise bad_request("Нет такой настройки", name)
    current = getattr(prefs, name)
    try:
        if name == "tz":
            return parse_tz(_text(value))
        if name == "budget_usd":
            if not _number(value):
                raise ValueError("not a number")
            return parse_money(str(value))
        if hasattr(current, "strftime"):  # a time of day
            return parse_time(_text(value))
        if isinstance(current, bool):
            if not isinstance(value, bool):
                raise ValueError("not a boolean")
            return value
        if isinstance(current, int):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError("not an integer")
            return value
    except (ValueError, OverflowError):
        raise bad_request(_PREF_ERRORS.get(_pref_kind(name, current), "Недопустимое значение"), name) from None
    return value  # Literal fields (stt_language, stt_mode): Prefs checks them


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("not a string")
    return value


def _number(value: Any) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer too big for a float
        return False


def _pref_kind(name: str, current: Any) -> str:
    if name == "tz":
        return "tz"
    if name == "budget_usd":
        return "money"
    if hasattr(current, "strftime"):
        return "time"
    return "bool" if isinstance(current, bool) else "int"


_PREF_ERRORS: Final[dict[str, str]] = {
    "tz": "Не знаю такой часовой пояс. Нужно название IANA, например Europe/Warsaw.",
    "money": "Нужна сумма в долларах (0 или больше), например 5 или 7.5.",
    "time": "Нужно время в формате ЧЧ:ММ, например 09:30.",
    "bool": "Нужно true или false.",
    "int": "Нужно целое число.",
}


# --- AI spend ------------------------------------------------------------------------------------------------------


async def usage(request: web.Request) -> web.Response:
    """This month's AI spend against the budget, by purpose."""

    async def load(session: AsyncSession, prefs: Prefs, now: datetime) -> dict[str, Any]:
        s = await query.usage_summary(session, prefs, now)
        ratio = s.spent_usd / s.limit_usd if s.limit_usd > 0 else (0.0 if s.spent_usd == 0 else None)
        by_purpose = sorted(s.by_purpose.items(), key=lambda kv: -kv[1][1])
        return {
            "month": s.month,
            "spent_usd": round(s.spent_usd, 6),
            "limit_usd": s.limit_usd,
            "ratio": ratio,
            "exhausted": s.limit_usd <= 0 or s.spent_usd >= s.limit_usd,
            "calls": s.calls,
            "by_purpose": [
                {"purpose": p, "label": views.PURPOSE_LABELS.get(p, p), "calls": calls, "usd": round(usd, 6)}
                for p, (calls, usd) in by_purpose
            ],
        }

    return await _read(request, load)


# --- background jobs -----------------------------------------------------------------------------------------------


async def job_status(request: web.Request) -> web.Response:
    """A job the app started ("🕒 Время" by text, "🪄 Разбить на шаги"): status, why it waits, its result."""
    job_id = path_id(request)
    ctx = ctx_of(request)
    async with ctx.sessionmaker() as session:
        job = await session.get(Job, job_id)
    if job is None:
        raise ApiError(404, "not_found", "Нет такой задачи")
    return json_response(job_json(job))


def job_json(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "retry_at": serialize.iso(job.run_after) if job.status == "queued" and job.last_error else None,
        "reason": job_reason(job),
        "result": job.result,
    }


def job_reason(job: Job) -> str | None:
    """Why a job waits or died: budget | ai_unavailable | ai_config | failed; None while it is fine."""
    error = job.last_error or ""
    if job.status == "dead":
        return "failed"
    if job.status != "queued" or not error:
        return None
    if error == BUDGET_REASON:
        return "budget"
    if error.startswith("AIUnavailable"):
        return "ai_unavailable"
    if error.startswith("AI config"):
        return "ai_config"
    return "error"
