"""Database schema. All timestamps are ``timestamptz`` stored in UTC.

Overview:
- raw_messages  — every incoming Telegram message, saved first (source of truth for re-processing)
- items         — what the AI extracted: tasks, events, ideas, notes, links, memes...
- occurrences   — dated instances of an item (one for one-off items, a rolling next one for recurring)
- reminders     — when to ping about an occurrence (several per occurrence possible: "за день и за час")
- bot_messages  — bot-sent message -> item/occurrence mapping (reply-to-edit, editing cards in place)
- jobs          — Postgres-backed work queue (FOR UPDATE SKIP LOCKED)
- ai_usage      — every AI call with tokens and cost (monthly budget)
- once_keys     — "send at most once" markers (digests per day, budget alerts per month)
- user_settings — Prefs JSONB per owner
- external_refs — links to items in external systems (Google Calendar, Notion...) for future sync
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dumpbot.db.base import Base

TZ = DateTime(timezone=True)


class UserSettings(Base):
    __tablename__ = "user_settings"

    owner_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    prefs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class RawMessage(Base):
    __tablename__ = "raw_messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_raw_messages_chat_msg"),
        Index("ix_raw_messages_media_group", "media_group_id"),
        Index("ix_raw_messages_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    media_group_id: Mapped[str | None] = mapped_column(String(64))
    # text | voice | audio | photo | image_document | document | video | video_note | animation
    # | sticker | location | contact | other
    content_type: Mapped[str] = mapped_column(String(32))
    text: Mapped[str | None] = mapped_column(Text)  # message text or caption
    urls: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=sa_text("'{}'"), default=list)

    file_id: Mapped[str | None] = mapped_column(Text)
    file_unique_id: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    file_name: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    duration: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)

    forward_origin: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # normalized MessageOrigin
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # Set when the message is a reply to a bot card -> processed as an edit of this item.
    reply_item_id: Mapped[int | None] = mapped_column(BigInteger)  # items.id (no FK: avoids a cycle)
    force_query: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"), default=False)

    tg_date: Mapped[datetime] = mapped_column(TZ)  # Telegram message.date — the "now" for time parsing
    transcript: Mapped[str | None] = mapped_column(Text)  # speech-to-text result
    image_text: Mapped[str | None] = mapped_column(Text)  # OCR text / description from the vision model
    link_titles: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # url -> page title

    # new | processing | done | deferred | failed | ignored
    status: Mapped[str] = mapped_column(String(16), server_default="new", default="new")
    error: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # full Telegram Message for re-processing
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(TZ)


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        Index("ix_items_search", "search_tsv", postgresql_using="gin"),
        Index("ix_items_tags", "tags", postgresql_using="gin"),
        Index("ix_items_status_kind", "status", "kind"),
        Index("ix_items_created", "created_at"),
        Index("ix_items_raw", "raw_message_id"),
        Index("ix_items_period", "period_start", "period_end"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    raw_message_id: Mapped[int | None] = mapped_column(ForeignKey("raw_messages.id", ondelete="SET NULL"))

    kind: Mapped[str] = mapped_column(String(16))  # domain.kinds.Kind
    title: Mapped[str] = mapped_column(Text)
    details: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=sa_text("'{}'"), default=list)
    source_text: Mapped[str | None] = mapped_column(Text)  # full text / transcript / OCR the item came from
    source_kind: Mapped[str] = mapped_column(String(16), server_default="text", default="text")
    source: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # forward origin / link info
    urls: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=sa_text("'{}'"), default=list)

    # open | done | deleted   (non-actionable kinds stay "open" = active)
    status: Mapped[str] = mapped_column(String(16), server_default="open", default="open")

    when_spec: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # the LLM's When, as returned
    lead_minutes: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))  # per-item override

    # Recurrence (RFC 5545 RRULE without DTSTART, expanded in local wall time of rec_tz)
    rrule: Mapped[str | None] = mapped_column(Text)
    rec_time_local: Mapped[time | None] = mapped_column(Time)
    rec_dtstart_local: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    rec_tz: Mapped[str | None] = mapped_column(String(64))
    rec_active: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"), default=False)

    # Fuzzy time (docs/features/iteration-2.md §A): week | month | season | soon | someday, with the local dates of
    # the span (inclusive; none for someday). ``someday`` items have no occurrence and are not in /tasks.
    period_kind: Mapped[str | None] = mapped_column(String(16))  # domain.kinds.PeriodKind
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    someday: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"), default=False)

    needs_confirm: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"), default=False)
    confidence: Mapped[str | None] = mapped_column(String(8))
    ai_model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))

    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())
    done_at: Mapped[datetime | None] = mapped_column(TZ)
    deleted_at: Mapped[datetime | None] = mapped_column(TZ)

    # 'russian' config stems Cyrillic words and maps ASCII words to the English stemmer -> mixed RU/EN.
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('russian'::regconfig, coalesce(title, '') || ' ' || coalesce(details, '') "
            "|| ' ' || coalesce(source_text, ''))",
            persisted=True,
        ),
    )

    occurrences: Mapped[list[Occurrence]] = relationship(
        back_populates="item", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )


class Occurrence(Base):
    __tablename__ = "occurrences"
    __table_args__ = (
        UniqueConstraint("item_id", "due_at", name="uq_occurrences_item_due"),
        Index("ix_occurrences_status_due", "status", "due_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    due_at: Mapped[datetime] = mapped_column(TZ)  # event / deadline / default time, UTC
    precision: Mapped[str] = mapped_column(String(16))  # domain.kinds.Precision
    # open | done | missed | cancelled
    status: Mapped[str] = mapped_column(String(16), server_default="open", default="open")
    done_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    item: Mapped[Item] = relationship(back_populates="occurrences", lazy="raise")
    reminders: Mapped[list[Reminder]] = relationship(
        back_populates="occurrence", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_status_fire", "status", "fire_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurrence_id: Mapped[int] = mapped_column(ForeignKey("occurrences.id", ondelete="CASCADE"))
    fire_at: Mapped[datetime] = mapped_column(TZ)
    lead_minutes: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    auto: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"), default=False)  # obeys quiet hours
    # pending | sending | sent | missed | cancelled | failed
    status: Mapped[str] = mapped_column(String(16), server_default="pending", default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(TZ)
    sent_at: Mapped[datetime | None] = mapped_column(TZ)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    occurrence: Mapped[Occurrence] = relationship(back_populates="reminders", lazy="raise")


class BotMessage(Base):
    __tablename__ = "bot_messages"
    __table_args__ = (Index("ix_bot_messages_item", "item_id"),)

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # card | reminder | digest | answer | ask | pick | notice | help | list | ... (see bot.sender callers)
    purpose: Mapped[str] = mapped_column(String(16))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    occurrence_id: Mapped[int | None] = mapped_column(ForeignKey("occurrences.id", ondelete="SET NULL"))
    raw_message_id: Mapped[int | None] = mapped_column(ForeignKey("raw_messages.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sa_text("'{}'::jsonb"), default=dict)
    # queued | running | done | dead
    status: Mapped[str] = mapped_column(String(16), server_default="queued", default="queued")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="10", default=10)
    run_after: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    locked_until: Mapped[datetime | None] = mapped_column(TZ)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class AiUsage(Base):
    __tablename__ = "ai_usage"
    __table_args__ = (Index("ix_ai_usage_created", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    purpose: Mapped[str] = mapped_column(String(16))  # extract | vision | stt | answer | models
    model: Mapped[str] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    audio_seconds: Mapped[float] = mapped_column(Numeric(10, 2), server_default="0", default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0", default=0)
    ok: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("true"), default=True)
    latency_ms: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    error: Mapped[str | None] = mapped_column(Text)


class OnceKey(Base):
    __tablename__ = "once_keys"

    key: Mapped[str] = mapped_column(Text, primary_key=True)  # e.g. "digest:morning:2026-10-06"
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class ExternalRef(Base):
    __tablename__ = "external_refs"
    __table_args__ = (UniqueConstraint("provider", "external_id", name="uq_external_refs_provider_ext"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))  # gcal | gdrive | notion | obsidian | gmail
    external_id: Mapped[str] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    sync_state: Mapped[str] = mapped_column(String(16), server_default="synced", default="synced")
    last_synced_at: Mapped[datetime | None] = mapped_column(TZ)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
