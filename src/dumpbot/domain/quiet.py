"""Quiet hours: automatic messages (default-time reminders, digests) are not sent inside the window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dumpbot.domain.prefs import Prefs
from dumpbot.domain.recurrence import localize


def in_quiet_hours(t: datetime, prefs: Prefs) -> bool:
    """True if ``t`` falls in [quiet_start, quiet_end) local time. The window may cross midnight;
    start == end or ``quiet_enabled=False`` means there are no quiet hours."""
    start, end = prefs.quiet_start, prefs.quiet_end
    if not prefs.quiet_enabled or start == end:
        return False
    local = _as_utc(t).astimezone(prefs.zone).time()
    if start < end:
        return start <= local < end
    return local >= start or local < end


def shift_out_of_quiet(t: datetime, prefs: Prefs) -> datetime:
    """``t`` (as UTC) if outside quiet hours, else the nearest following quiet_end (aware UTC)."""
    t_utc = _as_utc(t)
    if not in_quiet_hours(t_utc, prefs):
        return t_utc
    tz = prefs.zone
    local = t_utc.astimezone(tz)
    end_day = local.date()
    # Window crossing midnight, evening part (e.g. 23:30 in 23:00-08:00): the window ends tomorrow.
    if prefs.quiet_start > prefs.quiet_end and local.time() >= prefs.quiet_start:
        end_day += timedelta(days=1)
    return localize(end_day, prefs.quiet_end, tz).astimezone(UTC)


def _as_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
