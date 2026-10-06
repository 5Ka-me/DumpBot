"""«Закинуть в свалку» from the Mini App (docs/features/miniapp-api.md): the same pipeline as a message to the bot.

``POST /api/inbox {text}`` saves a raw message with ``source="app"`` (``ingest.save_app_text``) and queues its job
like the capture router does; nothing about it appears in the chat. ``GET /api/inbox/{raw_id}`` tells how far it got:
the items it created, the note the chat would have shown, the answer to a question, the items an edit changed, a
clarifying question (answered by ``POST …/answer``) or the "Какую запись изменить?" candidates (``POST …/pick``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from aiohttp import web
from sqlalchemy import select

from dumpbot import clock
from dumpbot.db.models import Job, RawMessage
from dumpbot.services import ingest, query
from dumpbot.web import serialize
from dumpbot.web.api_lists import job_json
from dumpbot.web.http import ApiError, bad_request, conflict, ctx_of, json_response, path_id, read_json
from dumpbot.workers import jobs

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.domain.prefs import Prefs

TEXT_MAX: Final = 4096  # a Telegram message
_UNFINISHED: Final = ("new", "processing", "deferred")
_RUNNING: Final = ("queued", "running")

NOT_FOUND: Final = "Нет такого сообщения"
ALREADY: Final = "Ответ уже принят"
NO_QUESTION: Final = "Вопроса к этому сообщению нет"
NOT_LISTED: Final = "Этой записи нет в списке"


def routes() -> list[web.RouteDef]:
    return [
        web.post("/api/inbox", post_inbox),
        web.get("/api/inbox/{id}", get_inbox),
        web.post("/api/inbox/{id}/answer", answer),
        web.post("/api/inbox/{id}/pick", pick),
    ]


async def post_inbox(request: web.Request) -> web.Response:
    """``{text}`` -> a raw message from the app and its processing job (202 with the status)."""
    body = await read_json(request)
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise bad_request("Напиши, что сохранить", "text")
    if len(text) > TEXT_MAX:
        raise bad_request(f"Не длиннее {TEXT_MAX} символов", "text")
    ctx = ctx_of(request)
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        raw = await ingest.save_app_text(session, ctx.chat_id, text.strip(), now)
        await ingest.enqueue_processing(session, raw, now)
        await session.commit()
    ctx.jobs_wakeup.set()
    return json_response(await _status(request, raw.id), status=202)


async def get_inbox(request: web.Request) -> web.Response:
    return json_response(await _status(request, path_id(request)))


async def answer(request: web.Request) -> web.Response:
    """``{option}``: the index of the chosen answer to the clarifying question -> job "clarify" (as the 🤔 buttons)."""
    raw_id = path_id(request)
    body = await read_json(request)
    option = body.get("option")
    if not isinstance(option, int) or isinstance(option, bool):
        raise bad_request("option — номер варианта, с 0", "option")
    ctx = ctx_of(request)
    async with ctx.sessionmaker() as session:
        raw = await _raw(session, raw_id)
        options = ((raw.raw_json or {}).get(ingest.ASK_KEY) or {}).get("options") or []
        if not options:
            raise conflict("no_question", NO_QUESTION)
        if not 0 <= option < len(options):
            raise bad_request("Нет такого варианта", "option")
        payload = {"raw_id": raw.id, "option": option}
        if await jobs.enqueue(session, "clarify", payload, dedupe_key=f"clarify:{raw.id}") is None:
            raise conflict("already_answered", ALREADY)
        await session.commit()
    ctx.jobs_wakeup.set()
    return json_response(await _status(request, raw_id), status=202)


async def pick(request: web.Request) -> web.Response:
    """``{item_id}``: the item the edit was about, one of the candidates -> job "edit" (as the "1".."5" buttons)."""
    raw_id = path_id(request)
    body = await read_json(request)
    item_id = body.get("item_id")
    if not isinstance(item_id, int) or isinstance(item_id, bool):
        raise bad_request("item_id — номер записи", "item_id")
    ctx = ctx_of(request)
    async with ctx.sessionmaker() as session:
        raw = await _raw(session, raw_id)
        listed = (raw.raw_json or {}).get(ingest.CANDIDATES_KEY) or []
        if item_id not in listed:
            raise conflict("not_listed", NOT_LISTED)
        payload = {"raw_id": raw.id, "item_id": item_id}
        if await jobs.enqueue(session, "edit", payload, dedupe_key=f"pick:{raw.id}:{item_id}") is None:
            raise conflict("already_answered", ALREADY)
        await session.commit()
    ctx.jobs_wakeup.set()
    return json_response(await _status(request, raw_id), status=202)


async def _raw(session: AsyncSession, raw_id: int) -> RawMessage:
    """A raw message sent from the app (404 for anything else)."""
    raw = await session.get(RawMessage, raw_id)
    if raw is None or raw.source != ingest.APP_SOURCE:
        raise ApiError(404, "not_found", NOT_FOUND)
    return raw


async def _status(request: web.Request, raw_id: int) -> dict[str, Any]:
    ctx = ctx_of(request)
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        raw = await _raw(session, raw_id)
        data = await _status_of(session, raw, prefs, clock.utcnow())
        await session.commit()
    return data


async def _status_of(session: AsyncSession, raw: RawMessage, prefs: Prefs, now: datetime) -> dict[str, Any]:
    """Where the message is: ``pending`` while it (or an answer to its question) is processed, then its outcome."""
    stmt = select(Job).where(Job.payload["raw_id"].astext == str(raw.id)).order_by(Job.id)
    raw_jobs = list((await session.scalars(stmt)).all())
    kinds = {job.kind for job in raw_jobs}
    data = raw.raw_json or {}
    app = data.get(ingest.APP_KEY) or {}
    ask = data.get(ingest.ASK_KEY) if "clarify" not in kinds else None
    candidates = data.get(ingest.CANDIDATES_KEY) if "edit" not in kinds else None
    found = app.get("answer")

    async def rows_of(ids: Any) -> list[dict[str, Any]]:
        ids = [i for i in ids if isinstance(i, int)] if isinstance(ids, list) else []
        return await serialize.rows(session, await query.rows_by_ids(session, ids), prefs, now)

    return {
        "raw_id": raw.id,
        "text": raw.text,
        "status": raw.status,
        "pending": raw.status in _UNFINISHED or any(job.status in _RUNNING for job in raw_jobs),
        "created_at": serialize.iso(raw.tg_date),
        "job": job_json(raw_jobs[-1]) if raw_jobs else None,
        "items": await serialize.rows(session, await query.items_of_raw(session, [raw.id]), prefs, now),
        "note": app.get("note"),
        "ask": {"question": ask.get("q"), "options": list(ask.get("options") or [])} if isinstance(ask, dict) else None,
        "candidates": await rows_of(candidates) if candidates else None,
        "answer": (
            {"text": found.get("text"), "title": found.get("title"), "rows": await rows_of(found.get("item_ids"))}
            if isinstance(found, dict)
            else None
        ),
        "edited": await rows_of(app.get("edited") or []),
    }
