"""Reminder / recurrence / digest loop (architecture.md §4.16). A DB-polling loop, no APScheduler.

``tick(now)``:
0. the time of the tick is stored (``app_state`` "scheduler:last_tick"), and again as the tick goes on when it is a
   long one (``_progress``: flood control may hold a send for minutes); a tick that finds the last stored time
   ``GAP_AFTER`` (15 min) or more in the past knows the bot was down (the PC slept, Docker stood still) — the first
   tick after a restart too — and keeps a ``Downtime`` for the notice about it (step 5). The open downtime is stored
   with the tick ("scheduler:downtime") until its notice is handled: a restart before that (a crash right after the
   PC woke up) picks it up again, so the missed evening digest is still folded and reported;
1. reminders stuck in ``sending`` for over 2 min (the process died mid-send) -> ``pending``;
2. claim due ``pending`` reminders (SKIP LOCKED, <= 20): item deleted / occurrence not open -> ``cancelled``;
   older than ``prefs.missed_grace_minutes`` -> ``missed`` (it shows up in the evening digest instead);
   inside quiet hours, one more than ``HOLD_AFTER`` (30 min) late, or an automatic one (``auto``) late at all (the
   bot was down) -> back to ``pending`` at the end of quiet hours with its own time in ``held_from``: it is sent
   silently then, whatever the grace says by that time (docs/features/iteration-3.md §5);
   otherwise send ``views.reminder_message`` -> ``sent`` + ``tg_message_id``. A transient send error (network,
   Telegram 5xx or flood control, a dropped DB connection) -> back to ``pending`` without using up an attempt, and
   the rest of the claimed batch waits for the next tick: it is retried every tick until it is sent or missed. Any
   other error -> attempts + 1 and back to ``pending`` (``failed`` after 5);
3. ``items.ensure_recurring`` (next instances of series);
4. digests: target = ``shift_out_of_quiet(local digest time)``, window [target, target + 3 h), never sent inside
   quiet hours, at most once per kind and day via ``once_keys`` ``digest:{kind}:{YYYY-MM-DD}``; an empty digest is
   not sent. An evening digest the bot was down for (no tick between its target and now) that cannot go out any more
   (quiet hours, or its window closed) goes into the next morning digest instead, when that day had something due or
   done (``digest.has_summary``; overdue items alone show up in the morning digest anyway): ``digest.folded_key``
   marks its day, and that morning digest shows the day's summary as "Вчерашний итог (не успел прислать)";
5. after a downtime, once the reminders it delayed are handled: one silent message, only if anything was affected
   (reminders sent late, held for the end of quiet hours or missed; a folded evening digest) — "😴 Меня не было с
   19:00 до 23:19 (компьютер спал?). Опоздало напоминаний: 1. Итог дня — в утреннем дайджесте."
   (``views.downtime_notice``);
6. heartbeat file (its mtime feeds the Docker healthcheck).

Every reminder sent and every line of a sent digest go into the events journal (docs/features/iteration-4.md §5,
``services.events``: ``reminder_sent``, ``digest_item``) after the message went out; a failed write is only logged.

``run`` registers with ``ctx.watchdog`` and beats before every tick, step and reminder. Silence for
``stall_limit_s`` means the loop is stuck (no single step legitimately takes that long): the process exits. A PC that
slept is no stall: the watchdog sees that the whole process was frozen and starts every limit afresh.

Delivery is at-least-once: a crash between Telegram's answer and the commit resends after recovery.
Each step is isolated: a failure is logged and the next step still runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from aiogram.exceptions import TelegramEntityTooLarge, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from sqlalchemy import func, select, update
from sqlalchemy.exc import InterfaceError, OperationalError

from dumpbot import clock
from dumpbot.bot import sender, views
from dumpbot.context import AppContext
from dumpbot.db.models import Item, Occurrence, OnceKey, Reminder
from dumpbot.domain import quiet, timeresolve
from dumpbot.domain.prefs import Prefs
from dumpbot.services import app_state, digest, events, items
from dumpbot.workers.jobs import describe_error, forget_once, safe_traceback, try_once

__all__ = [
    "CLAIM_LIMIT",
    "DIGEST_WINDOW",
    "DOWNTIME_KEY",
    "GAP_AFTER",
    "HOLD_AFTER",
    "LAST_TICK_KEY",
    "MAX_SEND_ATTEMPTS",
    "STALL_AFTER",
    "STUCK_AFTER",
    "Downtime",
    "Scheduler",
]

log = logging.getLogger(__name__)
_monotonic = time.monotonic  # the pace of a tick (tests replace it)

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
# Wall-clock silence between two ticks (normally <= scheduler_interval_s apart) that means the bot was down.
GAP_AFTER: Final = timedelta(minutes=15)
# A reminder later than this that comes due inside quiet hours waits for their end (and then comes silently).
HOLD_AFTER: Final = timedelta(minutes=30)
LAST_TICK_KEY: Final = "scheduler:last_tick"
DOWNTIME_KEY: Final = "scheduler:downtime"  # {"start", "end"} of the downtime whose notice is not handled yet
# How often a long tick stores how far it got (a slow tick is no downtime).
PROGRESS_EVERY: Final = timedelta(minutes=1)
# Errors that say nothing about the reminder itself: retried until it is sent or missed, never counted.
TRANSIENT_ERRORS: Final = (
    TelegramNetworkError,
    TelegramServerError,
    TelegramRetryAfter,
    OSError,
    OperationalError,
    InterfaceError,
)


@dataclass
class Downtime:
    """The bot was down from ``start`` (its last tick) to ``end`` (the first tick after it), and what that did — for
    the one notice about it (docs/features/iteration-3.md §5)."""

    start: datetime
    end: datetime
    late: set[int] = field(default_factory=set)  # reminders due in the gap, sent late
    held: set[int] = field(default_factory=set)  # ... waiting for the end of quiet hours
    missed: set[int] = field(default_factory=set)  # ... too old by now: not sent (/today lists their items)
    held_until: datetime | None = None  # when the held ones come
    folded: date | None = None  # the evening digest of this day goes into the next morning digest
    morning: bool = False  # ... and that morning digest is still to come (else /today lists the not-done items)

    def covers(self, fire_at: datetime) -> bool:
        """``fire_at`` fell into the gap: the reminder was due while the bot was down."""
        return self.start < fire_at <= self.end

    @property
    def affected(self) -> bool:
        return bool(self.late or self.held or self.missed or self.folded)


class Scheduler:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._backlog = False  # more due reminders than one claim takes: tick again right away
        self._unsent = False  # a due reminder waits for Telegram (a transient error): so does the downtime notice
        self.downtime: Downtime | None = None  # the last gap, until its notice is sent
        # The tick going on: its time, and the monotonic moments it started and last stored its progress (None: the
        # process may have been frozen during it, its progress is not stored any more).
        self._tick_at: datetime | None = None
        self._tick_mark = 0.0
        self._stored_mark: float | None = None
        self._resumed = False  # the stored downtime of an earlier process was looked at

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
        self._unsent = False
        try:
            prefs = await self._load_prefs()
            since = await self._note_tick(now)
            await self._step("recover", self._recover_stuck(now))
            await self._step("reminders", self._send_due(now, prefs))
            await self._step("recurring", self._ensure_recurring(now, prefs))
            await self._step("digests", self._digests(now, prefs, since))
            await self._step("downtime", self._report_downtime(now, prefs))
            await self._progress()
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

    async def _note_tick(self, now: datetime) -> datetime | None:
        """Store ``now`` as the last tick; return the last stored time before it, or the start of the downtime whose
        notice is still pending (None: unknown, e.g. the first tick ever). A gap of ``GAP_AFTER`` or more opens a
        ``Downtime``; another one before that notice went out extends it (one notice for both). The open downtime is
        stored in the same transaction, and a fresh process takes it over (``DOWNTIME_KEY``)."""
        self._alive()
        self._tick_at, self._tick_mark = now, _monotonic()
        self._stored_mark = self._tick_mark
        try:
            async with self._ctx.sessionmaker() as session:
                stored = await app_state.swap(session, LAST_TICK_KEY, now.isoformat())
                if not self._resumed:  # the process before this one may not have got to handle its downtime
                    if (taken := _stored_downtime(await app_state.get(session, DOWNTIME_KEY))) is not None:
                        self.downtime = taken
                        log.warning("downtime %s - %s taken over after a restart", taken.start, taken.end)
                prev = _parse_time(stored)
                if prev is not None and now - prev >= GAP_AFTER:
                    log.warning("no scheduler tick from %s to %s: the bot was down", prev.isoformat(), now.isoformat())
                    if self.downtime is None:
                        self.downtime = Downtime(start=prev, end=now)
                    else:
                        self.downtime.end = now
                    value = {"start": self.downtime.start.isoformat(), "end": self.downtime.end.isoformat()}
                    await app_state.put(session, DOWNTIME_KEY, value)
                await session.commit()
            self._resumed = True
        except Exception as e:
            log.error("last tick not stored: %s", describe_error(e))
            prev = None
        return self.downtime.start if self.downtime is not None else prev

    async def _progress(self) -> None:
        """A long tick stores how far it got (at most every ``PROGRESS_EVERY``), so that the next tick measures the
        gap from there: minutes of flood control are no downtime. A pause of ``GAP_AFTER`` or more since the last
        stored moment may be a frozen process (the PC slept mid-send): from then on this tick stores nothing, and the
        next one sees the gap."""
        if self._tick_at is None or self._stored_mark is None:
            return
        mark = _monotonic()
        if mark - self._stored_mark >= GAP_AFTER.total_seconds():
            self._stored_mark = None
            return
        if mark - self._stored_mark < PROGRESS_EVERY.total_seconds():
            return
        reached = self._tick_at + timedelta(seconds=mark - self._tick_mark)
        try:
            async with self._ctx.sessionmaker() as session:
                await app_state.put(session, LAST_TICK_KEY, reached.isoformat())
                await session.commit()
        except Exception as e:
            log.warning("tick progress not stored: %s", describe_error(e))
            return
        self._stored_mark = mark

    async def _close_downtime(self) -> None:
        """The downtime is handled (notice sent, or nothing to tell): forget it, the stored copy too."""
        self.downtime = None
        async with self._ctx.sessionmaker() as session:
            await app_state.delete(session, DOWNTIME_KEY)
            await session.commit()

    def _affect(self, outcome: str, reminder_id: int, fire_at: datetime, until: datetime | None = None) -> None:
        """Count a reminder that was due during the current downtime as ``late`` / ``held`` / ``missed``."""
        dt = self.downtime
        if dt is None or not dt.covers(fire_at):
            return
        for name in ("late", "held", "missed"):
            getattr(dt, name).discard(reminder_id)
        getattr(dt, outcome).add(reminder_id)
        if until is not None:
            dt.held_until = until

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
            await self._progress()
            if not await self._deliver(reminder_id, now, prefs):
                # Telegram is unreachable: the others would fail too (each send may take a minute to time out),
                # and an immediate backlog tick would spin. They wait for the next regular tick.
                self._backlog = False
                self._unsent = True
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
                fire_at, held = reminder.fire_at, reminder.held_from is not None
                own_time = reminder.held_from or fire_at
                if now - fire_at > timedelta(minutes=prefs.missed_grace_minutes):
                    log.info("reminder %s missed (fire_at %s)", reminder_id, fire_at.isoformat())
                    await self._finish(reminder_id, status="missed")
                    self._affect("missed", reminder_id, fire_at)
                    return True
                if not held and quiet.in_quiet_hours(now, prefs) and (reminder.auto or now - fire_at > HOLD_AFTER):
                    # Due late into quiet hours (downtime, a sleeping PC): it waits for their end and comes silently
                    # then, without a second look at the grace; an automatic one never rings in quiet hours.
                    later = quiet.shift_out_of_quiet(now, prefs)
                    await self._finish(reminder_id, status="pending", fire_at=later, held_from=fire_at)
                    self._affect("held", reminder_id, fire_at, until=later)
                    log.info("late reminder %s held until the end of quiet hours", reminder_id)
                    return True
                parent_title = None
                if item.parent_id is not None:  # a step of a checklist names its parent
                    parent_title = await session.scalar(select(Item.title).where(Item.id == item.parent_id))
                text, markup = views.reminder_message(item, occ, reminder, prefs, now, parent_title=parent_title)
                item_id, occ_id = item.id, occ.id
            # No transaction is held while waiting for Telegram.
            message_id = await sender.send(
                self._ctx, text, markup, purpose="reminder", item_id=item_id, occurrence_id=occ_id, silent=held
            )
        except Exception as e:
            return await self._send_failed(reminder_id, e)
        await self._finish(reminder_id, status="sent", sent_at=now, tg_message_id=message_id)
        if now - fire_at > views.LATE_AFTER:
            self._affect("late", reminder_id, fire_at)
        log.info("reminder %s sent", reminder_id)
        late_min = max(0, int((now - own_time).total_seconds() // 60))
        info: dict[str, Any] = {"reminder": reminder_id, "late_min": late_min}
        if held:
            info["held"] = True
        entry = events.Entry(
            events.REMINDER_SENT, source=events.SRC_REMINDER, item_id=item_id, occurrence_id=occ_id, data=info
        )
        await events.record(self._ctx.sessionmaker, entry)
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

    async def _digests(self, now: datetime, prefs: Prefs, since: datetime | None) -> None:
        await self._fold_missed_evening(now, prefs, since)  # before the morning digest that shows it
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
        await events.record(self._ctx.sessionmaker, *digest_entries(kind, data, now))

    async def _fold_missed_evening(self, now: datetime, prefs: Prefs, since: datetime | None) -> None:
        """An evening digest the bot was down for (no tick between ``since`` and its target) that can no longer go
        out (``now`` is in quiet hours, or its window has closed) goes into the next morning digest: its once-key is
        taken (it is never sent late) and, when the day had something due or done (``digest.has_summary``: what the
        morning digest's "Вчерашний итог" shows), its day is marked with ``digest.folded_key``. A fold done before a
        restart is found by that key and reported all the same."""
        if since is None or not prefs.digest_evening_enabled:
            return
        tz = prefs.zone
        today = now.astimezone(tz).date()
        for day in (today - timedelta(days=1), today):
            nominal = timeresolve.localize(day, prefs.digest_evening_time, tz).astimezone(UTC)
            target = quiet.shift_out_of_quiet(nominal, prefs)
            if not since < target <= now:
                continue
            if now < target + DIGEST_WINDOW and not quiet.in_quiet_hours(now, prefs):
                continue  # it still goes out now, late
            async with self._ctx.sessionmaker() as session:
                if await try_once(session, f"digest:evening:{day.isoformat()}"):
                    folded = digest.has_summary(await digest.day_summary(session, day, prefs))
                    if folded:
                        await try_once(session, digest.folded_key(day))
                    log.info("missed evening digest for %s: %s", day, "folded" if folded else "nothing to report")
                else:  # sent before the bot went down, or folded already (by the process before a restart)
                    folded = await session.get(OnceKey, digest.folded_key(day)) is not None
                await session.commit()
            if not folded:
                continue
            if self.downtime is not None:
                self.downtime.folded = day
                self.downtime.morning = _morning_ahead(day + timedelta(days=1), now, prefs)

    async def _report_downtime(self, now: datetime, prefs: Prefs) -> None:
        """The one silent notice after a downtime, once the reminders it delayed are handled (none waits for
        Telegram, no backlog left) and only if something was affected. A failed send is tried again on the next
        tick, for up to ``missed_grace_minutes`` after the bot came back."""
        dt = self.downtime
        if dt is None or self._backlog or self._unsent:
            return
        if not dt.affected:
            await self._close_downtime()
            log.info("the downtime %s - %s affected nothing", dt.start.isoformat(), dt.end.isoformat())
            return
        if now - dt.end > timedelta(minutes=prefs.missed_grace_minutes):
            await self._close_downtime()
            log.warning("downtime notice dropped: it could not be sent in time")
            return
        text = views.downtime_notice(
            dt.start,
            dt.end,
            prefs,
            late=len(dt.late) + len(dt.held),
            held=len(dt.held),
            held_until=dt.held_until,
            missed=len(dt.missed),
            folded=dt.folded,
            morning=dt.morning,
        )
        await sender.send(self._ctx, text, purpose="notice", silent=True)
        await self._close_downtime()
        log.info("downtime notice sent")

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


def digest_entries(kind: str, data: digest.DigestData, now: datetime) -> list[events.Entry]:
    """``digest_item`` events of a sent digest: a line per row of its sections as the view shows them
    (``views.morning_sections`` without the done ones / ``views.evening_sections``)."""
    if kind == "morning":
        sections = {s: rows for s, rows in views.morning_sections(data).items() if s != views.SEC_DONE}
    else:
        sections = views.evening_sections(data.agenda, now)
    return [
        events.Entry(
            events.DIGEST_ITEM,
            source=events.SRC_DIGEST,
            item_id=row.item_id,
            occurrence_id=row.occurrence_id,
            data={"digest": kind, "sec": section},
        )
        for section, rows in sections.items()
        for row in rows
    ]


def _parse_time(value: Any) -> datetime | None:
    """A stored ISO time as aware UTC; None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _stored_downtime(value: Any) -> Downtime | None:
    """A ``Downtime`` stored under ``DOWNTIME_KEY`` (None for anything else)."""
    if not isinstance(value, dict):
        return None
    start, end = _parse_time(value.get("start")), _parse_time(value.get("end"))
    return Downtime(start=start, end=end) if start is not None and end is not None and start < end else None


def _morning_ahead(day: date, now: datetime, prefs: Prefs) -> bool:
    """The morning digest of local ``day`` is still to come (or goes out in this very tick): on, its window open."""
    if not prefs.digest_morning_enabled:
        return False
    nominal = timeresolve.localize(day, prefs.digest_morning_time, prefs.zone).astimezone(UTC)
    return now < quiet.shift_out_of_quiet(nominal, prefs) + DIGEST_WINDOW


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
