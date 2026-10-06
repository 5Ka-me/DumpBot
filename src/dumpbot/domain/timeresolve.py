"""LLM time description (``When``) -> UTC due time, reminder plan and query ranges (docs/architecture.md §4.1).

The LLM only describes time; everything here is deterministic and uses the user's prefs (slots, default times,
timezone). ``now`` is converted to ``prefs.zone``; dates are computed on the local calendar, then stored in UTC.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final

from dateutil.relativedelta import relativedelta

from dumpbot.domain import quiet, recurrence
from dumpbot.domain.kinds import ACTIONABLE, Precision, SourceKind
from dumpbot.domain.recurrence import localize
from dumpbot.dto import ReminderPlan, Resolution

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from dumpbot.ai.schemas import ExtractedItem, When
    from dumpbot.domain.prefs import Prefs

__all__ = [
    "WARN_BAD_RRULE",
    "WARN_BAD_TIME",
    "WARN_MOVED_TOMORROW",
    "WARN_PAST_DATE",
    "ReminderPlan",
    "Resolution",
    "localize",
    "plan_reminders",
    "resolve_item",
    "resolve_range",
    "resolve_when",
]

WARN_PAST_DATE: Final = "дата в прошлом"
WARN_MOVED_TOMORROW: Final = "время уже прошло — перенёс на завтра"
WARN_BAD_RRULE: Final = "не понял правило повтора"
WARN_BAD_TIME: Final = "не понял дату или время"

_DAY_CODES: Final = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_SATURDAY: Final = 5
_OFFSET_RE = re.compile(r"^\+(\d{1,3})(d|w|mo|y)?$")
_WEEKDAY_RE = re.compile(r"^(next )?(MO|TU|WE|TH|FR|SA|SU)$")
_MONTHDAY_RE = re.compile(r"^d(-1|\d{1,2})$")
_DATE_RE = re.compile(r"^(?:(\d{4})-)?(\d{2})-(\d{2})$")
_HHMM_RE = re.compile(r"^(\d{2}):(\d{2})$")
_RANGE_N_RE = re.compile(r"^(past|next):(\d{1,3})$")
# Words that make an hour 0-6 unambiguous ("в 5 утра", "в 2 ночи", "в полночь", "5 am").
_EARLY_HOUR_OK_RE = re.compile(r"утр|ноч|(?<![a-z])a\.?\s?m\b\.?", re.IGNORECASE)
# Sources whose dates are often someone else's (screenshots, forwarded posts) -> always confirm.
_CONFIRM_SOURCES: Final[frozenset[str]] = frozenset({"photo", "forward"})
_LEAD_PRECISIONS: Final[frozenset[str]] = frozenset({"exact", "slot"})
_AUTO_PRECISIONS: Final[frozenset[str]] = frozenset({"date", "default", "fallback"})

WhenResult = tuple[datetime | None, Precision, list[str], bool]


# --- public API -----------------------------------------------------------------------------------------------


def resolve_item(item: ExtractedItem, now: datetime, prefs: Prefs, *, source: SourceKind) -> Resolution:
    """Due time, precision, reminder plan and recurrence for one extracted item."""
    now_utc = _as_utc(now)
    when = item.when if _has_time(item.when) else None
    role = item.when.role if item.when is not None else "event"
    warnings: list[str] = []
    confirm = item.conf == "low"

    if item.rrule:
        rec = _resolve_recurring(item, when, role, now_utc, prefs, source)
        if rec is not None:
            return replace(rec, needs_confirm=rec.needs_confirm or confirm)
        warnings.append(WARN_BAD_RRULE)
        confirm = True

    due: datetime | None = None
    precision: Precision = "none"
    if when is not None:
        due, precision, when_warnings, when_confirm = resolve_when(when, now_utc, prefs)
        warnings += when_warnings
        confirm = confirm or when_confirm or (due is not None and source in _CONFIRM_SOURCES)
    if due is None and item.kind in ACTIONABLE:
        due, precision = _default_due(now_utc, prefs), "default"

    reminders = plan_reminders(due, precision, role, item.lead, prefs, now_utc) if due is not None else ()
    return Resolution(
        due_at=due, precision=precision, reminders=reminders, needs_confirm=confirm, warnings=tuple(warnings)
    )


def resolve_when(when: When, now: datetime, prefs: Prefs) -> WhenResult:
    """(due_at UTC | None, precision, warnings, needs_confirm) — without reminders; also used for edits.

    Field priority: ``at`` > ``in_min`` > (``day`` and/or ``time``). An empty When gives (None, "none", [], False);
    an impossible date (e.g. "02-30") gives (None, "none", [WARN_BAD_TIME], True).
    """
    now_utc = _as_utc(now)
    if when.at:
        return _resolve_at(when.at, now_utc, prefs)
    if when.in_min:
        return now_utc + timedelta(minutes=when.in_min), "relative", [], False
    if not when.day and not when.time:
        return None, "none", [], False
    try:
        t, precision = _time_of(when, prefs)
        due_local, warnings, confirm = _resolve_day(when.day, t, now_utc.astimezone(prefs.zone), prefs)
    except (ValueError, KeyError):
        return None, "none", [WARN_BAD_TIME], True
    if precision == "exact" and _ambiguous_early_hour(t, when.text):
        confirm = True
    return due_local.astimezone(UTC), precision, warnings, confirm


def plan_reminders(
    due_at: datetime,
    precision: Precision,
    role: str,
    lead: list[int] | None,
    prefs: Prefs,
    now: datetime,
) -> tuple[ReminderPlan, ...]:
    """Reminder plan for one occurrence (sorted, unique ``fire_at``; empty if ``due_at <= now``).

    - exact/slot + role event/deadline: ``lead`` minutes before (default ``[prefs.lead_minutes]``), auto=False;
    - exact/slot + role remind, and relative: exactly at due, auto=False;
    - default: exactly at due, auto=True;
    - date/fallback (time chosen automatically): at due, auto=True; whole-day leads (multiples of 1440, e.g.
      "за день") are honoured, other lead values collapse to "at due".
    Leads that are multiples of 1440 are subtracted in local wall-clock days, others in UTC. A fire time already
    in the past fires now; auto reminders are moved out of quiet hours.
    """
    due = _as_utc(due_at)
    now_utc = _as_utc(now)
    if precision == "none" or due <= now_utc:
        return ()
    auto = precision in _AUTO_PRECISIONS
    if precision in _LEAD_PRECISIONS and role != "remind":
        leads = lead or [prefs.lead_minutes]
    elif precision in ("date", "fallback"):
        leads = [m if m % 1440 == 0 else 0 for m in lead or [0]]
    else:
        leads = [0]

    plans: dict[datetime, ReminderPlan] = {}
    for minutes in sorted(set(leads)):
        fire = max(_minus_lead(due, minutes, prefs.zone), now_utc)
        if auto:
            fire = quiet.shift_out_of_quiet(fire, prefs)
        # Equal fire times collapse; the smallest lead wins (it is the closest to the real distance to due).
        plans.setdefault(fire, ReminderPlan(fire_at=fire, lead_minutes=minutes, auto=auto))
    return tuple(sorted(plans.values(), key=lambda p: p.fire_at))


def resolve_range(spec_range: str, now: datetime, prefs: Prefs) -> tuple[datetime, datetime]:
    """QuerySpec.range -> [start, end) in UTC on the local calendar of ``prefs.tz``.

    today|tomorrow|yesterday|this_week|next_week|last_week (weeks start on Monday)|this_month|last_month
    |past:N (last N days incl. today)|next:N (today + N days)|YYYY-MM-DD|YYYY-MM-DD..YYYY-MM-DD (inclusive).
    Raises ValueError for anything else.
    """
    tz = prefs.zone
    today = _as_utc(now).astimezone(tz).date()
    first, last = _range_days(spec_range.strip(), today)
    return localize(first, time(0), tz).astimezone(UTC), localize(last, time(0), tz).astimezone(UTC)


# --- recurring items ----------------------------------------------------------------------------------------


def _resolve_recurring(
    item: ExtractedItem, when: When | None, role: str, now_utc: datetime, prefs: Prefs, source: SourceKind
) -> Resolution | None:
    """Resolution for an item with an RRULE, or None if the rule is invalid or already exhausted."""
    try:
        rule = recurrence.normalize_rrule(item.rrule or "")
    except ValueError:
        return None
    t, precision = prefs.task_default_time, "default"
    if when is not None and when.time:
        try:
            t, precision = _time_of(when, prefs)
        except (ValueError, KeyError):
            pass  # keep the default time; the rule itself is fine
    tz = prefs.zone
    dtstart_local = datetime.combine(now_utc.astimezone(tz).date(), t)
    if ";COUNT=" in f";{rule}" and localize(dtstart_local.date(), t, tz) < now_utc:
        # COUNT is counted from DTSTART: today's instance, already past, must not use one up
        # ("каждый день в 9, 2 раза" sent at 10:00 means tomorrow and the day after).
        dtstart_local += timedelta(days=1)
    due = recurrence.next_occurrence(rule, dtstart_local, tz, now_utc, inclusive=True)
    if due is None:
        return None
    confirm = source in _CONFIRM_SOURCES or (
        precision == "exact" and when is not None and _ambiguous_early_hour(t, when.text)
    )
    return Resolution(
        due_at=due,
        precision=precision,
        reminders=plan_reminders(due, precision, role, item.lead, prefs, now_utc),
        rrule=rule,
        rec_time_local=t,
        rec_dtstart_local=dtstart_local,
        needs_confirm=confirm,
    )


# --- When -> local datetime -------------------------------------------------------------------------------------


def _has_time(when: When | None) -> bool:
    return when is not None and bool(when.at or when.in_min or when.day or when.time)


def _time_of(when: When, prefs: Prefs) -> tuple[time, Precision]:
    """Clock time and precision: HH:MM -> exact, slot -> slot, no time -> bare date / weekend time."""
    if when.time:
        if m := _HHMM_RE.match(when.time):
            return time(int(m[1]), int(m[2])), "exact"
        return prefs.slot(when.time), "slot"
    if when.day == "weekend":
        return prefs.weekend_time, "date"
    return prefs.bare_date_time, "date"


def _resolve_day(day: str | None, t: time, now_local: datetime, prefs: Prefs) -> tuple[datetime, list[str], bool]:
    """Local aware datetime for ``day`` at ``t`` (+ warnings, needs_confirm). Raises ValueError for bad dates."""
    tz: ZoneInfo = prefs.zone
    today = now_local.date()

    def at(d: date) -> datetime:
        return localize(d, t, tz)

    def upcoming(d: date) -> bool:
        return at(d) > now_local

    if day is None:  # time only: today, or tomorrow once it has passed (no warning)
        return at(today if upcoming(today) else today + timedelta(days=1)), [], False
    if m := _OFFSET_RE.match(day):
        return _offset_day(int(m[1]), m[2] or "d", today, now_local, prefs, at)
    if day == "weekend":
        return at(_nearest_weekday(_SATURDAY, today, upcoming)), [], False
    if m := _WEEKDAY_RE.match(day):
        weekday = _DAY_CODES.index(m[2])
        if m[1]:  # "next FR": that weekday of the next Monday-based week
            return at(today - timedelta(days=today.weekday()) + timedelta(days=7 + weekday)), [], False
        return at(_nearest_weekday(weekday, today, upcoming)), [], False
    if m := _MONTHDAY_RE.match(day):
        return at(_month_day(int(m[1]), today, upcoming)), [], False
    if m := _DATE_RE.match(day):
        year, month, dom = m.groups()
        if year is None:
            return at(_upcoming_month_day(int(month), int(dom), today.year, upcoming)), [], False
        d = date(int(year), int(month), int(dom))
        if not upcoming(d):  # explicit past date: probably a fact, keep it but ask
            return at(d), [WARN_PAST_DATE], True
        return at(d), [], False
    raise ValueError(f"unknown day spec: {day!r}")


def _offset_day(
    n: int, unit: str, today: date, now_local: datetime, prefs: Prefs, at: Callable[[date], datetime]
) -> tuple[datetime, list[str], bool]:
    # Before night_cutoff "завтра" still means the calendar day that has just started.
    if unit == "d" and n >= 1 and now_local.time() < prefs.night_cutoff:
        n -= 1
    step = {
        "d": relativedelta(days=n),
        "w": relativedelta(weeks=n),
        "mo": relativedelta(months=n),  # clamps: Jan 31 + 1 month -> Feb 28
        "y": relativedelta(years=n),
    }[unit]
    d = today + step
    if d == today and at(d) <= now_local:
        return at(today + timedelta(days=1)), [WARN_MOVED_TOMORROW], True
    return at(d), [], False


def _nearest_weekday(weekday: int, today: date, upcoming: Callable[[date], bool]) -> date:
    """Nearest such weekday; today only if the time is still ahead, otherwise in a week."""
    d = today + timedelta(days=(weekday - today.weekday()) % 7)
    return d if d != today or upcoming(d) else d + timedelta(days=7)


def _month_day(n: int, today: date, upcoming: Callable[[date], bool]) -> date:
    """N-th day of this month (clamped to its end; -1 = last day), or of the next month if already passed."""
    dom = 31 if n == -1 else n
    if not 1 <= dom <= 31:
        raise ValueError(f"bad day of month: {n}")
    first = today.replace(day=1)
    d = first + relativedelta(day=dom)
    return d if upcoming(d) else first + relativedelta(months=1, day=dom)


def _upcoming_month_day(month: int, dom: int, year: int, upcoming: Callable[[date], bool]) -> date:
    """MM-DD this year if still ahead, else the next year that has it (02-29 waits for a leap year)."""
    for y in range(year, year + 9):
        try:
            d = date(y, month, dom)
        except ValueError:
            continue
        if upcoming(d):
            return d
    raise ValueError(f"bad date: {month:02d}-{dom:02d}")


def _resolve_at(at: str, now_utc: datetime, prefs: Prefs) -> WhenResult:
    """``at`` = local "YYYY-MM-DD[THH:MM]" for phrasings the spec cannot express: always confirmed."""
    day_s, _, time_s = at.partition("T")
    try:
        d = date.fromisoformat(day_s)
        t = time.fromisoformat(time_s) if time_s else prefs.bare_date_time
    except ValueError:
        return None, "none", [WARN_BAD_TIME], True
    due = localize(d, t, prefs.zone).astimezone(UTC)
    return due, "fallback", [WARN_PAST_DATE] if due <= now_utc else [], True


def _ambiguous_early_hour(t: time, text: str | None) -> bool:
    """00:00-06:59 without "утра/ночи/am" in the phrase is probably a misread PM time."""
    return t.hour <= 6 and not _EARLY_HOUR_OK_RE.search(text or "")


def _default_due(now_utc: datetime, prefs: Prefs) -> datetime:
    """Next ``task_default_time``: today if still ahead, otherwise tomorrow."""
    tz = prefs.zone
    today = now_utc.astimezone(tz).date()
    due = localize(today, prefs.task_default_time, tz)
    if due <= now_utc:
        due = localize(today + timedelta(days=1), prefs.task_default_time, tz)
    return due.astimezone(UTC)


# --- reminders / ranges ---------------------------------------------------------------------------------------


def _minus_lead(due: datetime, minutes: int, tz: ZoneInfo) -> datetime:
    """Whole days are wall-clock days ("за день" before 19:00 is 19:00 even across DST); the rest is UTC."""
    if minutes and minutes % 1440 == 0:
        local = due.astimezone(tz)
        return localize(local.date() - timedelta(days=minutes // 1440), local.time(), tz).astimezone(UTC)
    return due - timedelta(minutes=minutes)


def _range_days(spec: str, today: date) -> tuple[date, date]:
    """[first, last) local dates for a range spec."""
    day = timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    fixed = {
        "today": (today, today + day),
        "tomorrow": (today + day, today + 2 * day),
        "yesterday": (today - day, today),
        "this_week": (week_start, week_start + 7 * day),
        "next_week": (week_start + 7 * day, week_start + 14 * day),
        "last_week": (week_start - 7 * day, week_start),
        "this_month": (month_start, month_start + relativedelta(months=1)),
        "last_month": (month_start - relativedelta(months=1), month_start),
    }
    if spec in fixed:
        return fixed[spec]
    if m := _RANGE_N_RE.match(spec):
        n = int(m[2])
        if m[1] == "past":
            return today - (max(n, 1) - 1) * day, today + day
        return today, today + (n + 1) * day
    first_s, sep, last_s = spec.partition("..")
    first = date.fromisoformat(first_s)
    last = date.fromisoformat(last_s) if sep else first
    first, last = min(first, last), max(first, last)
    return first, last + day


def _as_utc(dt: datetime) -> datetime:
    """Aware UTC; naive input is taken as UTC (the API convention)."""
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
