"""Reminder / recurrence / digest loop (architecture.md §4.16). A DB-polling loop, no APScheduler.

``tick(now)``:
1. reminders stuck in ``sending`` for over 2 min (the process died mid-send) -> ``pending``;
2. claim due ``pending`` reminders (SKIP LOCKED, <= 20): item deleted / occurrence not open -> ``cancelled``;
   older than ``prefs.missed_grace_minutes`` -> ``missed`` (it shows up in the evening digest instead);
   otherwise send ``views.reminder_message`` -> ``sent`` + ``tg_message_id``; a send error -> attempts + 1 and back
   to ``pending`` (``failed`` after 5);
3. ``items.ensure_recurring`` (next instances of series);
4. digests: target = ``shift_out_of_quiet(local digest time)``, window [target, target + 3 h), at most once per
   kind and day via ``once_keys`` ``digest:{kind}:{YYYY-MM-DD}``; an empty digest is not sent;
5. heartbeat file (its mtime feeds the Docker healthcheck).

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

from sqlalchemy import func, select, update

from dumpbot import clock
from dumpbot.bot import sender, views
from dumpbot.context import AppContext
from dumpbot.db.models import Item, Occurrence, Reminder
from dumpbot.domain import quiet, timeresolve
from dumpbot.domain.prefs import Prefs
from dumpbot.services import digest, items
from dumpbot.workers.jobs import describe_error, forget_once, safe_traceback, try_once

__all__ = ["CLAIM_LIMIT", "DIGEST_WINDOW", "MAX_SEND_ATTEMPTS", "STUCK_AFTER", "Scheduler"]

log = logging.getLogger(__name__)

STUCK_AFTER: Final = timedelta(minutes=2)
CLAIM_LIMIT: Final = 20
MAX_SEND_ATTEMPTS: Final = 5
DIGEST_WINDOW: Final = timedelta(hours=3)
DIGEST_KINDS: Final = ("morning", "evening")
MIN_SLEEP_S: Final = 0.2


class Scheduler:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._backlog = False  # more due reminders than one claim takes: tick again right away

    async def run(self) -> None:
        """Forever: tick, then sleep until min(interval, next pending fire_at) or ``ctx.scheduler_wakeup``."""
        log.info("scheduler started")
        while True:
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

    async def _step(self, name: str, step: Awaitable[None]) -> None:
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
        for reminder_id in ids:
            await self._deliver(reminder_id, now, prefs)

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

    async def _deliver(self, reminder_id: int, now: datetime, prefs: Prefs) -> None:
        """One claimed reminder: cancel / miss / send. Never raises (a failure counts as a send attempt)."""
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
                    return
                reminder, occ, item = row
                if item.status == "deleted" or occ.status != "open":
                    await self._finish(reminder_id, status="cancelled")
                    return
                if now - reminder.fire_at > timedelta(minutes=prefs.missed_grace_minutes):
                    log.info("reminder %s missed (fire_at %s)", reminder_id, reminder.fire_at.isoformat())
                    await self._finish(reminder_id, status="missed")
                    return
                text, markup = views.reminder_message(item, occ, reminder, prefs, now)
                item_id, occ_id = item.id, occ.id
            # No transaction is held while waiting for Telegram.
            message_id = await sender.send(
                self._ctx, text, markup, purpose="reminder", item_id=item_id, occurrence_id=occ_id
            )
        except Exception as e:
            await self._send_failed(reminder_id, e)
            return
        await self._finish(reminder_id, status="sent", sent_at=now, tg_message_id=message_id)
        log.info("reminder %s sent", reminder_id)

    async def _send_failed(self, reminder_id: int, error: Exception) -> None:
        async with self._ctx.sessionmaker() as session:
            reminder = await session.get(Reminder, reminder_id)
            if reminder is None:
                return
            reminder.attempts = (reminder.attempts or 0) + 1
            reminder.status = "failed" if reminder.attempts >= MAX_SEND_ATTEMPTS else "pending"
            reminder.claimed_at = None
            attempts, status = reminder.attempts, reminder.status
            await session.commit()
        log.warning(
            "reminder %s: send failed (attempt %d, now %s): %s", reminder_id, attempts, status, describe_error(error)
        )

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


def _write_heartbeat(path: str, now: datetime) -> None:
    """Best effort: a missing directory is created, write errors are only logged."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(now.isoformat(), encoding="utf-8")
    except OSError as e:
        log.warning("heartbeat %s not written: %s", path, type(e).__name__)
