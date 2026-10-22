"""Incoming Telegram message -> ``RawMessage`` row -> processing job (architecture.md §4.13).

Saving is fast and AI-free: the capture router stores the message, reacts 👀 and enqueues a job; the job worker
does the rest. Only Telegram metadata is stored for files (``file_id`` etc.), never the bytes.

"Закинуть в свалку" from the Mini App (docs/features/iteration-2.md §D) is saved the same way by ``save_app_text``:
``source="app"``, a synthetic negative message id; the processor then keeps its replies with the raw message
(``APP_KEY``) instead of sending them to the chat.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from aiogram.types import (
    Message,
    MessageEntity,
    MessageOrigin,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot.db.models import Job, RawMessage
from dumpbot.workers import jobs

__all__ = [
    "ALBUM_DELAY",
    "APP_KEY",
    "APP_SOURCE",
    "ASK_KEY",
    "CANDIDATES_KEY",
    "EDIT_KEY",
    "UNDO_KEY",
    "UNHEARD_KEY",
    "enqueue_processing",
    "extract_urls",
    "message_raw_ids",
    "message_to_raw_fields",
    "normalize_origin",
    "picked_item",
    "save_app_text",
    "save_incoming",
    "text_urls",
    "was_asked",
]

log = logging.getLogger(__name__)

# Album parts arrive as separate updates; the album job waits this long after the latest part (debounce).
ALBUM_DELAY: Final = timedelta(seconds=2)
# RawMessage.raw_json keys written by the processor and read when the owner answers by replying with text:
ASK_KEY: Final = "_ask"  # the clarifying question asked about the message (AskCb only carries the option index)
CANDIDATES_KEY: Final = "_candidates"  # item ids of the "Какую запись изменить?" list, in list order
# The edit already read from the message (an EditPatch as JSON) that the picked candidate gets — no second AI call.
EDIT_KEY: Final = "_edit"
# [↩️ Отменить] of an edit applied by description: {item id: what the edit changed} (``services.undo``).
UNDO_KEY: Final = "_undo"
APP_KEY: Final = "_app"  # a message from the Mini App: what the owner was told about it ({note, answer, edited})
# A voice note whose transcript was not speech (docs/features/iteration-4.md §1): {"tries": [{stt, reason, text} …],
# "job": {"kind", "payload"}} — the job [🔁 Ещё раз] queues again; the rejected transcript stays in ``transcript``.
UNHEARD_KEY: Final = "_unheard"
APP_SOURCE: Final = "app"  # RawMessage.source of "закинуть в свалку" from the Mini App ("telegram" otherwise)
_TEXT_URL_RE: Final = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'«»]+")
_URL_TAIL: Final = ".,;:!?)]}…"
_FILE_FIELDS: Final = (
    "file_id",
    "file_unique_id",
    "mime_type",
    "file_name",
    "file_size",
    "duration",
    "width",
    "height",
)
_URL_SCHEMES: Final = ("http://", "https://", "tg://", "mailto:", "ftp://")


# --- Message -> fields ------------------------------------------------------------------------------------------


def message_to_raw_fields(message: Message) -> dict[str, Any]:
    """Column values of a ``RawMessage`` for ``message`` (everything except status / force_query / reply_item_id).

    ``content_type``: text | voice | audio | photo | image_document | document | video | video_note | animation
    | sticker | location | contact | other. ``text`` is the text or the caption. ``urls`` come from ``url`` and
    ``text_link`` entities (no duplicates). ``raw_json`` is the full message, JSON-serializable.
    """
    text = message.text if message.text is not None else message.caption
    fields: dict[str, Any] = {
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "media_group_id": message.media_group_id,
        "text": text or None,
        "urls": extract_urls(message),
        "forward_origin": normalize_origin(message.forward_origin),
        "reply_to_message_id": message.reply_to_message.message_id if message.reply_to_message else None,
        "tg_date": _aware(message.date),
        "raw_json": message.model_dump(mode="json", exclude_none=True),
    }
    fields.update(_content_fields(message))
    return fields


def _content_fields(m: Message) -> dict[str, Any]:
    """content_type + file metadata (all file columns present, None when not applicable)."""
    out: dict[str, Any] = dict.fromkeys(_FILE_FIELDS)
    if m.photo:
        # Sizes come smallest first, but pick explicitly: the largest gives the best OCR.
        p = max(m.photo, key=lambda s: (s.width * s.height, s.file_size or 0))
        _file(out, p, mime_type="image/jpeg", width=p.width, height=p.height)
        return {**out, "content_type": "photo"}
    if m.animation:  # checked before document: animations also carry a ``document`` for old clients
        a = m.animation
        _file(out, a, mime_type=a.mime_type, file_name=a.file_name, duration=a.duration, width=a.width, height=a.height)
        return {**out, "content_type": "animation"}
    if m.document:
        d = m.document
        _file(out, d, mime_type=d.mime_type, file_name=d.file_name)
        is_image = (d.mime_type or "").lower().startswith("image/")
        return {**out, "content_type": "image_document" if is_image else "document"}
    if m.voice:
        v = m.voice
        _file(out, v, mime_type=v.mime_type, duration=v.duration)
        return {**out, "content_type": "voice"}
    if m.audio:
        a = m.audio
        _file(out, a, mime_type=a.mime_type, file_name=a.file_name, duration=a.duration)
        return {**out, "content_type": "audio"}
    if m.video:
        v = m.video
        _file(out, v, mime_type=v.mime_type, file_name=v.file_name, duration=v.duration, width=v.width, height=v.height)
        return {**out, "content_type": "video"}
    if m.video_note:
        n = m.video_note
        _file(out, n, duration=n.duration, width=n.length, height=n.length)
        return {**out, "content_type": "video_note"}
    if m.sticker:
        s = m.sticker
        _file(out, s, width=s.width, height=s.height)
        return {**out, "content_type": "sticker"}
    if m.location:  # also venues (they carry a location)
        return {**out, "content_type": "location"}
    if m.contact:
        return {**out, "content_type": "contact"}
    if m.text is not None:
        return {**out, "content_type": "text"}
    return {**out, "content_type": "other"}


def _file(out: dict[str, Any], obj: Any, **extra: Any) -> None:
    out["file_id"] = obj.file_id
    out["file_unique_id"] = obj.file_unique_id
    out["file_size"] = getattr(obj, "file_size", None)
    out.update(extra)


# --- URLs -------------------------------------------------------------------------------------------------------


def extract_urls(message: Message) -> list[str]:
    """URLs from ``url`` entities (substring by UTF-16 offsets) and ``text_link`` entities, in order, unique."""
    if message.text is not None:
        text, entities = message.text, message.entities
    else:
        text, entities = message.caption or "", message.caption_entities
    out: list[str] = []
    for entity in entities or []:
        url = _entity_url(text, entity)
        if url and url not in out:
            out.append(url)
    return out


def _entity_url(text: str, entity: MessageEntity) -> str | None:
    if entity.type == "url":
        return _normalize_url(utf16_slice(text, entity.offset, entity.length))
    if entity.type == "text_link" and entity.url:
        return _normalize_url(entity.url)
    return None


def utf16_slice(text: str, offset: int, length: int) -> str:
    """Substring by UTF-16 code units (Telegram entity offsets), e.g. after an emoji (2 units) or a ZWJ sequence."""
    units = text.encode("utf-16-le")
    return units[offset * 2 : (offset + length) * 2].decode("utf-16-le", errors="ignore")


def _normalize_url(url: str) -> str | None:
    """Trim; a bare ``habr.com/x`` (Telegram links those) gets ``https://`` so it can be opened and fetched."""
    url = url.strip()
    if not url:
        return None
    if not url.lower().startswith(_URL_SCHEMES):
        url = "https://" + url
    return url


def text_urls(text: str) -> list[str]:
    """URLs typed in plain text (a message from the Mini App has no Telegram entities): ``http(s)://…`` and
    ``www.…``, trailing punctuation dropped, in order, unique."""
    out: list[str] = []
    for match in _TEXT_URL_RE.finditer(text or ""):
        url = _normalize_url(match.group(0).rstrip(_URL_TAIL))
        if url and url not in out:
            out.append(url)
    return out


# --- forward origin ---------------------------------------------------------------------------------------------


def normalize_origin(origin: MessageOrigin | None) -> dict[str, Any] | None:
    """MessageOrigin -> ``{type, title?, username?, chat_id?, user_id?, message_id?, url?, sender_name?,
    author_signature?, date}`` (no None values; ``date`` as ISO string)."""
    if origin is None:
        return None
    data: dict[str, Any] = {"type": getattr(origin, "type", "unknown")}
    if isinstance(origin, MessageOriginChannel):
        chat = origin.chat
        data.update(
            title=chat.title,
            username=chat.username,
            chat_id=chat.id,
            message_id=origin.message_id,
            url=_channel_post_url(chat.id, chat.username, origin.message_id),
            author_signature=origin.author_signature,
        )
    elif isinstance(origin, MessageOriginChat):
        chat = origin.sender_chat
        data.update(
            title=chat.title,
            username=chat.username,
            chat_id=chat.id,
            url=f"https://t.me/{chat.username}" if chat.username else None,
            author_signature=origin.author_signature,
        )
    elif isinstance(origin, MessageOriginUser):
        user = origin.sender_user
        data.update(
            sender_name=user.full_name,
            username=user.username,
            user_id=user.id,
            url=f"https://t.me/{user.username}" if user.username else None,
        )
    elif isinstance(origin, MessageOriginHiddenUser):
        data["sender_name"] = origin.sender_user_name
    date = getattr(origin, "date", None)
    if isinstance(date, datetime):
        data["date"] = _aware(date).isoformat()
    return {k: v for k, v in data.items() if v is not None}


def _channel_post_url(chat_id: int, username: str | None, message_id: int) -> str:
    """Public: ``t.me/<username>/<id>``; private: ``t.me/c/<id without -100>/<id>`` (works for members)."""
    if username:
        return f"https://t.me/{username}/{message_id}"
    internal = str(chat_id)
    internal = internal[4:] if internal.startswith("-100") else internal.lstrip("-")
    return f"https://t.me/c/{internal}/{message_id}"


def _aware(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# --- persistence ------------------------------------------------------------------------------------------------


async def save_incoming(
    session: AsyncSession, message: Message, *, force_query: bool = False, reply_item_id: int | None = None
) -> tuple[RawMessage, bool]:
    """INSERT … ON CONFLICT (chat_id, message_id) DO NOTHING -> ``(raw, created)``. Does not commit.

    A redelivered update (after a crash Telegram may send it again) returns the existing row with ``False``.
    """
    fields = message_to_raw_fields(message)
    fields.update(force_query=force_query, reply_item_id=reply_item_id, status="new")
    stmt = (
        pg_insert(RawMessage)
        .values(**fields)
        .on_conflict_do_nothing(constraint="uq_raw_messages_chat_msg")
        .returning(RawMessage)
    )
    raw = (await session.scalars(stmt)).first()
    if raw is not None:
        log.info("saved message %s (%s)", raw.message_id, raw.content_type)
        return raw, True
    existing = await session.scalar(
        select(RawMessage).where(RawMessage.chat_id == fields["chat_id"], RawMessage.message_id == fields["message_id"])
    )
    if existing is None:  # pragma: no cover - conflicting row vanished between the statements
        raise RuntimeError(f"message {fields['message_id']} neither inserted nor found")
    log.info("message %s already saved", existing.message_id)
    return existing, False


async def save_app_text(session: AsyncSession, chat_id: int, text: str, now: datetime) -> RawMessage:
    """Text sent from the Mini App as a raw message (``source="app"``, status new). Its message id is the negative
    of its own id, so it can never clash with a Telegram message; "?" at the start makes it a question, like in the
    chat. Does not commit."""
    raw_id = int(await session.scalar(select(func.nextval(func.pg_get_serial_sequence("raw_messages", "id")))))
    raw = RawMessage(
        id=raw_id,
        chat_id=chat_id,
        message_id=-raw_id,
        content_type="text",
        text=text,
        urls=text_urls(text),
        force_query=text.lstrip().startswith("?"),
        tg_date=_aware(now),
        status="new",
        raw_json={},
        source=APP_SOURCE,
    )
    session.add(raw)
    await session.flush()
    log.info("saved app message %s", raw.id)
    return raw


async def message_raw_ids(session: AsyncSession, chat_id: int, message_id: int) -> list[int]:
    """Raw ids of a saved message, or of every part of its album (the album's items belong to its first part);
    [] when the message was never saved."""
    raw = await session.scalar(
        select(RawMessage).where(RawMessage.chat_id == chat_id, RawMessage.message_id == message_id)
    )
    if raw is None:
        return []
    if not raw.media_group_id:
        return [raw.id]
    stmt = select(RawMessage.id).where(RawMessage.chat_id == chat_id, RawMessage.media_group_id == raw.media_group_id)
    return list((await session.scalars(stmt.order_by(RawMessage.message_id))).all())


async def was_asked(session: AsyncSession, raw_id: int) -> bool:
    """True when the bot asked a clarifying question about raw ``raw_id`` (``ASK_KEY`` in its raw_json)."""
    raw = await session.get(RawMessage, raw_id)
    return raw is not None and ASK_KEY in (raw.raw_json or {})


async def picked_item(session: AsyncSession, raw_id: int, text: str) -> int | None:
    """The item a reply "2" to the "Какую запись изменить?" list of raw ``raw_id`` picks (``CANDIDATES_KEY``);
    None for anything but a listed number."""
    text = text.strip().rstrip(".)")
    if not text.isdecimal():
        return None
    raw = await session.get(RawMessage, raw_id)
    ids = (raw.raw_json or {}).get(CANDIDATES_KEY) if raw is not None else None
    number = int(text)
    if not isinstance(ids, list) or not 1 <= number <= len(ids):
        return None
    return int(ids[number - 1])


async def enqueue_processing(session: AsyncSession, raw: RawMessage, now: datetime) -> None:
    """Queue the processing job of a saved message. Does not commit.

    - reply to a bot card (``reply_item_id``) -> ``edit {raw_id, item_id}``, dedupe ``edit:{raw_id}``;
    - album part -> ``album {media_group_id, chat_id}``, dedupe ``album:{chat_id}:{gid}``, ``run_after`` =
      latest part + 2 s (each new part pushes it back while the job is still queued); a part that arrives after the
      album job started is processed on its own (``message``);
    - otherwise ``message {raw_id}``, dedupe ``msg:{raw_id}``.
    """
    if raw.reply_item_id is not None:
        await jobs.enqueue(
            session, "edit", {"raw_id": raw.id, "item_id": raw.reply_item_id}, dedupe_key=f"edit:{raw.id}"
        )
    elif raw.media_group_id:
        if await _enqueue_album(session, raw, now) is None:
            log.info("album %s is already being processed; message %s goes alone", raw.media_group_id, raw.id)
            await jobs.enqueue(session, "message", {"raw_id": raw.id}, dedupe_key=f"msg:{raw.id}")
    else:
        await jobs.enqueue(session, "message", {"raw_id": raw.id}, dedupe_key=f"msg:{raw.id}")


async def _enqueue_album(session: AsyncSession, raw: RawMessage, now: datetime) -> int | None:
    """Insert the album job or push back its ``run_after`` while it is still queued; None if it already ran."""
    run_after = now + ALBUM_DELAY
    stmt = pg_insert(Job).values(
        kind="album",
        payload={"media_group_id": raw.media_group_id, "chat_id": raw.chat_id},
        dedupe_key=f"album:{raw.chat_id}:{raw.media_group_id}",
        status="queued",
        attempts=0,
        run_after=run_after,
        created_at=now,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Job.dedupe_key],
        set_={"run_after": func.greatest(Job.run_after, stmt.excluded.run_after), "updated_at": now},
        where=Job.status == "queued",
    ).returning(Job.id)
    return (await session.execute(stmt)).scalar_one_or_none()
