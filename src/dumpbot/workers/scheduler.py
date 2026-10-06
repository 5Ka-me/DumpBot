"""Reminder / recurrence / digest loop (architecture.md §4.16). A DB-polling loop, no APScheduler.

``tick(now)``:
1. reminders stuck in ``sending`` for over 2 min (the process died mid-send) -> ``pending``;
2. claim due ``pending`` reminders (SKIP LOCKED, <= 20): item deleted / occurrence not open -> ``cancelled``;
   older than ``prefs.missed_grace_minutes`` -> ``missed`` (it shows up in the evening digest instead);
   an automatic one (``auto``) that comes due late, inside quiet hours (the bot was down) -> back to ``pending`` at
   the end of quiet hours, or ``missed`` if that is beyond the grace;
   otherwise send ``views.reminder_message`` -> ``sent`` + ``tg_message_id``. A transient send error (network,
   Telegram 5xx or flood control, a dropped DB connection) -> back to ``pending`` without using up an attempt, and
   the rest of the claimed batch waits for the next tick: it is retried every tick until it is sent or missed. Any
   other error -> attempts + 1 and back to ``pending`` (``failed`` after 5);
3. ``items.ensure_recurring`` (next instances of series);
4. digests: target = ``shift_out_of_quiet(local digest time)``, window [target, target + 3 h), never sent inside
   quiet hours (a digest that is late into them is dropped once its window closes), at most once per kind and
   day via ``once_keys`` ``digest:{kind}:{YYYY-MM-DD}``; an empty digest is not sent;
5. heartbeat file (its mtime feeds the Docker healthcheck).

``run`` registers with ``ctx.watchdog`` and beats before every tick, step and reminder. Silence for
``stall_limit_s`` means the loop is stuck (no single step legitimately takes that long): the process exits.

Delivery is at-least-once: a crash between Telegram's answer and the commit resends after recovery.
Each step is isolated: a failure is logged and the next step still runs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from aiogram.exceptions import TelegramEntityTooLarge, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from sqlalchemy import func, select, update
from sqlalchemy.exc import InterfaceError, OperationalError

from dumpbot import clock
from dumpbot.bot import sender, views
from dumpbot.context import AppContext
from dumpbot.db.models import Item, Occurrence, Reminder
from dumpbot.domain import quiet, timeresolve
from dumpbot.domain.prefs import Prefs
from dumpbot.services import digest, items
from dumpbot.workers.jobs import describe_error, forget_once, safe_traceback, try_once

__all__ = ["CLAIM_LIMIT", "DIGEST_WINDOW", "MAX_SEND_ATTEMPTS", "STALL_AFTER", "STUCK_AFTER", "Scheduler"]

log = logging.getLogger(__name__)

STUCK_AFTER: Final = timedelta(minutes=2)
CLAIM_LIMIT: Final = 20
MAX_SEND_ATTEMPTS: Final = 5
DIGEST_WINDOW: Final = timedelta(hours=3)
DIGEST_KINDS: Final = ("morning", "evening")
MIN_SLEEP_S: Final = 0.2
# Watchdog limit (at least 3 intervals). The longest legitimate step is one send under flood control: 4 requests
# that may each take 60 s to time out, with flood waits of up to 60 s between them (about 7 min).
STALL_AFTER: Final = timedelta(minutes=15)
WATCH_NAME: Final = "scheduler"
# Errors that say nothing about the reminder itself: retried until it is sent or missed, never counted.
TRANSIENT_ERRORS: Final = (
    TelegramNetworkError,
    TelegramServerError,
    TelegramRetryAfter,
    OSError,
    OperationalError,
    InterfaceError,
)


class Scheduler:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._backlog = False  # more due reminders than one claim takes: tick again right away

    @property
    def stall_limit_s(self) -> float:
        return max(STALL_AFTER.total_seconds(), 3 * float(self._ctx.settings.scheduler_interval_s))

    async def run(self) -> None:
        """Forever: tick, then sleep until min(interval, next pending fire_at) or ``ctx.scheduler_wakeup``."""
        log.info("scheduler started")
        self._ctx.watchdog.watch(WATCH_NAME, self.stall_limit_s)
        while True:
            self._alive()
            self._ctx.scheduler_wakeup.clear()  # before the tick: a wakeup during it is not lost
            try:
                await self.tick(clock.utcnow())
            except Exception as e:
                log.error("scheduler tick failed: %s\n%s", describe_error(e), safe_traceback(e))
            if self._backlog:
                await asyncio.sleep(0)
                continue
            delay = await self._sleep_for(clock.utcnow())
            try:
                await asyncio.wait_for(self._ctx.scheduler_wakeup.wait(), delay)
            except TimeoutError:
                pass

    async def tick(self, now: datetime) -> None:
        self._backlog = False
        try:
            prefs = await self._load_prefs()
            await self._step("recover", self._recover_stuck(now))
            await self._step("reminders", self._send_due(now, prefs))
            await self._step("recurring", self._ensure_recurring(now, prefs))
            await self._step("digests", self._digests(now, prefs))
        finally:
            _write_heartbeat(self._ctx.settings.heartbeat_file, now)

    # --- steps ------------------------------------------------------------------------------------------------

    def _alive(self) -> None:
        self._ctx.watchdog.beat(WATCH_NAME)

    async def _step(self, name: str, step: Awaitable[None]) -> None:
        self._alive()
        try:
            await step
        except Exception as e:
            log.error("scheduler step %s failed: %s\n%s", name, describe_error(e), safe_traceback(e))

    async def _load_prefs(self) -> Prefs:
        async with self._ctx.sessionmaker() as session:
            prefs = await self._ctx.prefs(session)
            await session.commit()
        return prefs

    async def _recover_stuck(self, now: datetime) -> None:
        stmt = (
            update(Reminder)
            .where(Reminder.status == "sending", Reminder.claimed_at < now - STUCK_AFTER)
            .values(status="pending", claimed_at=None)
            .execution_options(synchronize_session=False)
        )
        async with self._ctx.sessionmaker() as session:
            result = await session.execute(stmt)
            await session.commit()
        if result.rowcount:  # type: ignore[attr-defined]
            log.warning("recovered %d reminder(s) stuck in sending", result.rowcount)  # type: ignore[attr-defined]

    async def _send_due(self, now: datetime, prefs: Prefs) -> None:
        ids = await self._claim(now)
        self._backlog = len(ids) >= CLAIM_LIMIT
        for n, reminder_id in enumerate(ids):
            self._alive()
            if not await self._deliver(reminder_id, now, prefs):
                # Telegram is unreachable: the others would fail too (each send may take a minute to time out),
                # and an immediate backlog tick would spin. They wait for the next regular tick.
                self._backlog = False
                await self._unclaim(ids[n + 1 :])
                return

    async def _unclaim(self, ids: list[int]) -> None:
        if not ids:
            return
        async with self._ctx.sessionmaker() as session:
            await session.execute(
                update(Reminder)
                .where(Reminder.id.in_(ids), Reminder.status == "sending")
                .values(status="pending", claimed_at=None)
                .execution_options(synchronize_session=False)
            )
            await session.commit()

    async def _claim(self, now: datetime) -> list[int]:
        due = (
            select(Reminder.id)
            .where(Reminder.status == "pending", Reminder.fire_at <= now)
            .order_by(Reminder.fire_at, Reminder.id)
            .limit(CLAIM_LIMIT)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        stmt = (
            update(Reminder)
            .where(Reminder.id.in_(due))
            .values(status="sending", claimed_at=now)
            .returning(Reminder.id, Reminder.fire_at)
            .execution_options(synchronize_session=False)
        )
        async with self._ctx.sessionmaker() as session:
            rows = (await session.execute(stmt)).all()
            await session.commit()
        return [row.id for row in sorted(rows, key=lambda r: (r.fire_at, r.id))]

    async def _deliver(self, reminder_id: int, now: datetime, prefs: Prefs) -> bool:
        """One claimed reminder: cancel / miss / send. Returns False after a transient send failure.

        Only raises when the outcome cannot be recorded (the DB is down): the reminder then stays ``sending`` and
        is recovered after ``STUCK_AFTER``.
        """
        try:
            async with self._ctx.sessionmaker() as session:
                stmt = (
                    select(Reminder, Occurrence, Item)
                    .join(Occurrence, Occurrence.id == Reminder.occurrence_id)
                    .join(Item, Item.id == Occurrence.item_id)
                    .where(Reminder.id == reminder_id)
                )
                row = (await session.execute(stmt)).one_or_none()
                if row is None:
                    return True
                reminder, occ, item = row
                if item.status == "deleted" or occ.status != "open":
                    await self._finish(reminder_id, status="cancelled")
                    return True
                grace = timedelta(minutes=prefs.missed_grace_minutes)
                if now - reminder.fire_at > grace:
                    log.info("reminder %s missed (fire_at %s)", reminder_id, reminder.fire_at.isoformat())
                    await self._finish(reminder_id, status="missed")
                    return True
                if reminder.auto and quiet.in_quiet_hours(now, prefs):
                    # Planned outside quiet hours but due late (downtime, a sleeping PC): automatic messages
                    # wait for the end of quiet hours, unless they would be too stale by then.
                    later = quiet.shift_out_of_quiet(now, prefs)
                    if later - reminder.fire_at > grace:
                        await self._finish(reminder_id, status="missed")
                    else:
                        await self._finish(reminder_id, status="pending", fire_at=later)
                    log.info("automatic reminder %s not sent in quiet hours", reminder_id)
                    return True
                parent_title = None
                if item.parent_id is not None:  # a step of a checklist names its parent
                    parent_title = await session.scalar(select(Item.title).where(Item.id == item.parent_id))
                text, markup = views.reminder_message(item, occ, reminder, prefs, now, parent_title=parent_title)
                item_id, occ_id = item.id, occ.id
            # No transaction is held while waiting for Telegram.
            message_id = await sender.send(
                self._ctx, text, markup, purpose="reminder", item_id=item_id, occurrence_id=occ_id
            )
        except Exception as e:
            return await self._send_failed(reminder_id, e)
        await self._finish(reminder_id, status="sent", sent_at=now, tg_message_id=message_id)
        log.info("reminder %s sent", reminder_id)
        return True

    async def _send_failed(self, reminder_id: int, error: Exception) -> bool:
        """Back to ``pending``. Only an error that is not transient uses up an attempt (``failed`` after
        ``MAX_SEND_ATTEMPTS``): outages are waited out up to ``missed_grace_minutes``. Returns False if transient."""
        transient = _is_transient(error)
        async with self._ctx.sessionmaker() as session:
            reminder = await session.get(Reminder, reminder_id)
            if reminder is None:
                return not transient
            if not transient:
                reminder.attempts = (reminder.attempts or 0) + 1
            reminder.status = "failed" if reminder.attempts >= MAX_SEND_ATTEMPTS else "pending"
            reminder.claimed_at = None
            attempts, status = reminder.attempts, reminder.status
            await session.commit()
        if transient:
            log.warning("reminder %s: send failed, retrying on the next tick: %s", reminder_id, describe_error(error))
        else:
            log.warning(
                "reminder %s: send failed (attempt %d, now %s): %s",
                reminder_id,
                attempts,
                status,
                describe_error(error),
            )
        return not transient

    async def _finish(self, reminder_id: int, **values: object) -> None:
        async with self._ctx.sessionmaker() as session:
            await session.execute(
                update(Reminder)
                .where(Reminder.id == reminder_id)
                .values(claimed_at=None, **values)
                .execution_options(synchronize_session=False)
            )
            await session.commit()

    async def _ensure_recurring(self, now: datetime, prefs: Prefs) -> None:
        async with self._ctx.sessionmaker() as session:
            created = await items.ensure_recurring(session, prefs, now)
            await session.commit()
        if created:
            log.info("created %d next occurrence(s) of recurring items", created)

    async def _digests(self, now: datetime, prefs: Prefs) -> None:
        tz = prefs.zone
        today = now.astimezone(tz).date()
        for kind in DIGEST_KINDS:
            if not getattr(prefs, f"digest_{kind}_enabled"):
                continue
            at = getattr(prefs, f"digest_{kind}_time")
            # Yesterday too: a time inside quiet hours may be shifted past midnight.
            for day in (today, today - timedelta(days=1)):
                nominal = timeresolve.localize(day, at, tz).astimezone(UTC)
                target = quiet.shift_out_of_quiet(nominal, prefs)
                if target <= now < target + DIGEST_WINDOW:
                    if not quiet.in_quiet_hours(now, prefs):  # late into quiet hours (the bot was down): dropped
                        await self._send_digest(kind, day.isoformat(), nominal, now, prefs)
                    break

    async def _send_digest(self, kind: str, day: str, nominal: datetime, now: datetime, prefs: Prefs) -> None:
        """Only the caller that inserts ``digest:{kind}:{day}`` sends; the key is dropped again if sending fails."""
        key = f"digest:{kind}:{day}"
        async with self._ctx.sessionmaker() as session:
            if not await try_once(session, key):
                return
            await session.commit()
            try:
                data = await digest.build_digest(session, kind, prefs, nominal)
                if data is None:
                    log.info("%s digest for %s skipped: nothing to report", kind, day)
                    return
                render = views.morning_digest if kind == "morning" else views.evening_digest
                text, markup = render(data, prefs, now)
                await sender.send(self._ctx, text, markup, purpose="digest")
            except Exception:
                await session.rollback()
                await forget_once(session, key)
                await session.commit()
                raise
        log.info("%s digest for %s sent", kind, day)

    async def _sleep_for(self, now: datetime) -> float:
        """Seconds until the next pending reminder, capped by ``settings.scheduler_interval_s``.

        Only future reminders count: overdue pending ones (failed sends) wait for the regular interval.
        """
        interval = float(self._ctx.settings.scheduler_interval_s)
        try:
            async with self._ctx.sessionmaker() as session:
                nxt = await session.scalar(
                    select(func.min(Reminder.fire_at)).where(Reminder.status == "pending", Reminder.fire_at > now)
                )
        except Exception as e:
            log.debug("next fire_at lookup failed: %s", describe_error(e))
            return interval
        if nxt is None:
            return interval
        return max(MIN_SLEEP_S, min(interval, (nxt - now).total_seconds()))


def _is_transient(error: BaseException) -> bool:
    """Telegram, the network or the DB connection is temporarily unavailable (a 413 is about the content)."""
    return isinstance(error, TRANSIENT_ERRORS) and not isinstance(error, TelegramEntityTooLarge)


def _write_heartbeat(path: str, now: datetime) -> None:
    """Best effort: a missing directory is created, write errors are only logged."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(now.isoformat(), encoding="utf-8")
    except OSError as e:
        log.warning("heartbeat %s not written: %s", path, type(e).__name__)
