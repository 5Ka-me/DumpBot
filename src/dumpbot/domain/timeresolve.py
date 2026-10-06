"""LLM time description (``When``) -> UTC due time, reminder plan and query ranges (docs/architecture.md §4.1).

The LLM only describes time; everything here is deterministic and uses the user's prefs (slots, default times,
timezone). ``now`` is converted to ``prefs.zone``; dates are computed on the local calendar, then stored in UTC.

Fuzzy spans (docs/features/iteration-2.md §A): ``When.period`` names a calendar week / month / season, "на днях"
(soon) or "когда-нибудь" (someday). A period is due at its reminder (2) (last working day at ``task_default_time``)
and also reminds on the morning of its first day when it has not started yet; deadlines (role ``deadline``) remind
the evening before as well.
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
    "lead_applies",
    "localize",
    "period_bounds",
    "plan_reminders",
    "resolve_item",
    "resolve_range",
    "resolve_when",
    "tomorrow",
    "when_period",
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
_AUTO_PRECISIONS: Final[frozenset[str]] = frozenset({"date", "default", "fallback", "period", "soon"})
# A deadline with one of these also reminds the evening before (task_default_time of the previous day).
_DEADLINE_PRECISIONS: Final[frozenset[str]] = frozenset({"exact", "slot", "date", "fallback"})
_NO_DUE_PRECISIONS: Final[frozenset[str]] = frozenset({"none", "someday"})
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_SEASON_FIRST_MONTH: Final[dict[str, int]] = {"spring": 3, "summer": 6, "autumn": 9, "winter": 12}  # 3 months each
_PERIOD_KINDS: Final[dict[str, str]] = {
    "this_week": "week",
    "next_week": "week",
    "this_month": "month",
    "next_month": "month",
    **dict.fromkeys(_SEASON_FIRST_MONTH, "season"),
    "soon": "soon",
    "someday": "someday",
}
_WORKDAYS: Final = 5  # Monday..Friday: weekday() < 5
# A date without a time that is today, once its bare-date time and task_default_time have passed: due this long
# from now (but still today), so the item stays on today's list and gets one automatic nudge.
_LATER_TODAY: Final = timedelta(hours=1)

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
    period_kind, start, end = when_period(when, now_utc, prefs) if when is not None else (None, None, None)
    if precision == "someday":  # "когда-нибудь": no date and no default reminder, even for a task
        return Resolution(
            due_at=None, precision="someday", needs_confirm=confirm, warnings=tuple(warnings), period_kind="someday"
        )
    if due is None and item.kind in ACTIONABLE:
        due, precision = _default_due(now_utc, prefs), "default"
        period_kind = start = end = None

    reminders: tuple[ReminderPlan, ...] = ()
    if due is not None:
        span = (start, end) if start is not None and end is not None else None
        reminders = plan_reminders(due, precision, role, item.lead, prefs, now_utc, period=span)
    return Resolution(
        due_at=due,
        precision=precision,
        reminders=reminders,
        needs_confirm=confirm,
        warnings=tuple(warnings),
        period_kind=period_kind,
        period_start=start,
        period_end=end,
    )


def resolve_when(when: When, now: datetime, prefs: Prefs) -> WhenResult:
    """(due_at UTC | None, precision, warnings, needs_confirm) — without reminders; also used for edits.

    Field priority: ``at`` > ``in_min`` > ``period`` > (``day`` and/or ``time``). An empty When gives
    (None, "none", [], False); an impossible date (e.g. "02-30") gives (None, "none", [WARN_BAD_TIME], True).
    Periods: someday -> (None, "someday", …); soon -> ``soon_days`` ahead at ``when.time`` or ``task_default_time``,
    precision "soon"; a week / month / season -> its reminder (2), precision "period" (``_period_due``); with role
    ``deadline`` ("до конца недели") -> the last working day of the span as a plain date.
    """
    now_utc = _as_utc(now)
    if when.at:
        return _resolve_at(when.at, now_utc, prefs)
    if when.in_min:
        return now_utc + timedelta(minutes=when.in_min), "relative", [], False
    if when.period:
        return _resolve_period(when, now_utc, prefs)
    if not when.day and not when.time:
        return None, "none", [], False
    try:
        t, precision = _time_of(when, prefs)
        now_local = now_utc.astimezone(prefs.zone)
        due_local, warnings, confirm = _resolve_day(when.day, t, now_local, prefs, timed=precision != "date")
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
    *,
    period: tuple[date, date] | None = None,
) -> tuple[ReminderPlan, ...]:
    """Reminder plan for one occurrence (sorted, unique ``fire_at``; empty if ``due_at <= now``).

    - exact/slot + role event/deadline: ``lead`` minutes before (default ``[prefs.lead_minutes]``), auto=False;
    - exact/slot + role remind, and relative: at due unless ``lead`` is given explicitly, auto=False;
    - default, soon: exactly at due, auto=True;
    - date/fallback (time chosen automatically): at due, auto=True; whole-day leads (multiples of 1440, e.g.
      "за день") are honoured, other lead values collapse to "at due";
    - period (``period`` = its local [start, end]): (2) at due and (1) on the morning of the first day
      (``bare_date_time``) if the span starts after today, both auto=True;
    - role deadline with exact/slot/date/fallback: also the evening before (``task_default_time`` of the previous
      day), auto=True.
    ``lead`` is only ever explicit (the message, the ⏱ menu or an edit). Leads that are multiples of 1440 are
    subtracted in local wall-clock days, others in UTC. A lead reminder already in the past fires now, while the
    extra (1) / evening-before moments are skipped once past; auto reminders are moved out of quiet hours.
    """
    due = _as_utc(due_at)
    now_utc = _as_utc(now)
    if precision in _NO_DUE_PRECISIONS or due <= now_utc:
        return ()
    plans: dict[datetime, ReminderPlan] = {}

    def add(fire: datetime, minutes: int, auto: bool) -> None:
        if auto:
            fire = quiet.shift_out_of_quiet(fire, prefs)
        # Equal fire times collapse; the smallest lead wins (it is the closest to the real distance to due).
        plans.setdefault(fire, ReminderPlan(fire_at=fire, lead_minutes=minutes, auto=auto))

    auto = precision in _AUTO_PRECISIONS
    for minutes in _leads(precision, role, lead, prefs):
        add(max(_minus_lead(due, minutes, prefs.zone), now_utc), minutes, auto)
    for fire in _extra_fires(due, precision, role, period, prefs, now_utc):
        if fire > now_utc and quiet.shift_out_of_quiet(fire, prefs) < due:
            add(fire, int((due - fire).total_seconds() // 60), True)
    return tuple(sorted(plans.values(), key=lambda p: p.fire_at))


def _leads(precision: str, role: str, lead: list[int] | None, prefs: Prefs) -> list[int]:
    """Minutes before due of the lead-based reminders, ascending."""
    if precision in _LEAD_PRECISIONS and role != "remind":
        leads = lead or [prefs.lead_minutes]
    elif precision in ("date", "fallback"):
        leads = [m if m % 1440 == 0 else 0 for m in lead or [0]]
        if role == "deadline":  # the deadline's own morning stays, whatever "за день" adds
            leads.append(0)
    elif precision in ("default", "period", "soon"):
        leads = [0]
    else:  # role remind, relative: the moment itself, unless the user asked for a lead ("напомни за 30 минут")
        leads = lead or [0]
    return sorted(set(leads))


def _extra_fires(
    due: datetime, precision: str, role: str, period: tuple[date, date] | None, prefs: Prefs, now_utc: datetime
) -> list[datetime]:
    """Automatic reminders besides the lead ones: (1) of a period that has not started, the evening before a
    deadline. Moments in the past are dropped by the caller."""
    tz = prefs.zone
    if precision == "period":
        if period is None or period[0] <= now_utc.astimezone(tz).date():
            return []
        return [localize(period[0], prefs.bare_date_time, tz).astimezone(UTC)]
    if role == "deadline" and precision in _DEADLINE_PRECISIONS:
        eve = due.astimezone(tz).date() - timedelta(days=1)
        return [localize(eve, prefs.task_default_time, tz).astimezone(UTC)]
    return []


def lead_applies(precision: str, minutes: int) -> bool:
    """Whether an explicit "remind ``minutes`` before" changes the plan of an occurrence with this precision
    (see ``plan_reminders``): any lead for a time the user gave, only whole days for an automatic date."""
    if precision in _LEAD_PRECISIONS or precision == "relative":
        return True
    if precision in ("date", "fallback"):
        return minutes % 1440 == 0
    return minutes == 0


def tomorrow(now: datetime, prefs: Prefs) -> date:
    """Local date that "завтра" means at ``now``: before ``prefs.night_cutoff`` it is the calendar day that has just
    started (the owner has not slept yet), otherwise the next calendar day."""
    local = _as_utc(now).astimezone(prefs.zone)
    return local.date() if local.time() < prefs.night_cutoff else local.date() + timedelta(days=1)


def period_bounds(period: str, now: datetime, prefs: Prefs) -> tuple[date, date]:
    """Local dates [start, end] (both inclusive) of a fuzzy span at ``now``.

    this_week / next_week: Monday..Sunday; this_month / next_month; YYYY-MM: that month — the model names only the
    month, so a month already over (or an older year) means the next one; spring / summer / autumn / winter: the
    nearest season not over yet (Mar-May, Jun-Aug, Sep-Nov, Dec-Feb across the new year); soon: today .. the day
    "на днях" means (``prefs.soon_days`` ahead, counted like day "+N", so ``night_cutoff`` applies).
    Raises ValueError for someday (it has no dates) and anything else.
    """
    today = _as_utc(now).astimezone(prefs.zone).date()
    week = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    if period == "this_week":
        return week, week + timedelta(days=6)
    if period == "next_week":
        return week + timedelta(days=7), week + timedelta(days=13)
    if period == "this_month":
        return _month_span(month)
    if period == "next_month":
        return _month_span(month + relativedelta(months=1))
    if period == "soon":
        return today, tomorrow(now, prefs) + timedelta(days=prefs.soon_days - 1)
    if period in _SEASON_FIRST_MONTH:
        return _season_span(_SEASON_FIRST_MONTH[period], today)
    if m := _MONTH_RE.match(period):
        month_no = int(m[2])
        if not 1 <= month_no <= 12:
            raise ValueError(f"bad month: {period!r}")
        start, end = _month_span(date(max(int(m[1]), today.year), month_no, 1))
        return (start, end) if end >= today else _month_span(start + relativedelta(years=1))
    raise ValueError(f"no dates for period {period!r}")


def when_period(when: When, now: datetime, prefs: Prefs) -> tuple[str | None, date | None, date | None]:
    """(``PeriodKind``, start, end) the item keeps for a When whose time is a fuzzy span, else (None, None, None).

    week / month / season / soon come with their dates (``period_bounds``), someday without. A deadline
    "до конца недели" (role ``deadline``) is a plain date, and ``at`` / ``in_min`` win over a period.
    """
    period = when.period
    if not period or when.at or when.in_min:
        return None, None, None
    kind = _PERIOD_KINDS.get(period) or ("month" if _MONTH_RE.match(period) else None)
    if kind is None:
        return None, None, None
    if kind == "someday":
        return kind, None, None
    if when.role == "deadline" and kind != "soon":
        return None, None, None
    try:
        start, end = period_bounds(period, now, prefs)
    except ValueError:
        return None, None, None
    return kind, start, end


def resolve_range(spec_range: str, now: datetime, prefs: Prefs) -> tuple[datetime, datetime]:
    """QuerySpec.range -> [start, end) in UTC on the local calendar of ``prefs.tz``.

    today|tomorrow|yesterday|this_week|next_week|last_week (weeks start on Monday)|this_month|last_month
    |past:N (last N days incl. today)|next:N (today + N days)|YYYY-MM-DD|YYYY-MM-DD..YYYY-MM-DD (inclusive).
    "tomorrow" follows ``night_cutoff`` like the day spec "+1" (``tomorrow``). Raises ValueError for anything else.
    """
    tz = prefs.zone
    today = _as_utc(now).astimezone(tz).date()
    first, last = _range_days(spec_range.strip(), today, tomorrow(now, prefs))
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
    now_local = now_utc.astimezone(tz)
    start = now_local.date()
    if when is not None and when.day:
        # "с понедельника каждый день", "каждые 2 недели, начиная со следующей пятницы": the series starts there,
        # and an interval rule keeps the phase of DTSTART. A day in the past (or unreadable) starts today.
        try:
            first, _, _ = _resolve_day(when.day, t, now_local, prefs, timed=precision != "default")
            start = max(first.date(), start)
        except (ValueError, KeyError):
            pass
    dtstart_local = datetime.combine(start, t)
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
    return when is not None and bool(when.at or when.in_min or when.period or when.day or when.time)


# --- fuzzy spans --------------------------------------------------------------------------------------------------


def _resolve_period(when: When, now_utc: datetime, prefs: Prefs) -> WhenResult:
    """``resolve_when`` for a When with ``period`` (see there)."""
    period = when.period or ""
    if period == "someday":
        return None, "someday", [], False
    tz = prefs.zone
    try:
        start, end = period_bounds(period, now_utc, prefs)
        if period == "soon":
            t = _time_of(when, prefs)[0] if when.time else prefs.task_default_time
            return localize(end, t, tz).astimezone(UTC), "soon", [], False
    except (ValueError, KeyError):
        return None, "none", [WARN_BAD_TIME], True
    if when.role == "deadline":  # "до конца недели": the last working day of the span, as a plain date
        today = now_utc.astimezone(tz).date()
        last = _last_workday(start, end)
        day = last if last is not None and last >= today else end
        return resolve_when(when.model_copy(update={"period": None, "day": day.isoformat()}), now_utc, prefs)
    return _period_due(start, end, now_utc, prefs), "period", [], False


def _period_due(start: date, end: date, now_utc: datetime, prefs: Prefs) -> datetime:
    """Reminder (2) of a span: its last working day at ``task_default_time``; once that has passed, its last day
    at that time; if that has passed too, that moment anyway (the item has no reminder (2) then)."""
    tz = prefs.zone
    days = [d for d in (_last_workday(start, end), end) if d is not None]
    moments = [localize(d, prefs.task_default_time, tz).astimezone(UTC) for d in days]
    return next((m for m in moments if m > now_utc), moments[-1])


def _last_workday(start: date, end: date) -> date | None:
    """The last Monday..Friday in [start, end], or None."""
    d = end
    while d >= start:
        if d.weekday() < _WORKDAYS:
            return d
        d -= timedelta(days=1)
    return None


def _month_span(first: date) -> tuple[date, date]:
    return first, first + relativedelta(months=1) - timedelta(days=1)


def _season_span(first_month: int, today: date) -> tuple[date, date]:
    """The season starting in ``first_month`` that is not over at ``today`` (last winter included)."""
    for year in (today.year - 1, today.year):
        start = date(year, first_month, 1)
        end = start + relativedelta(months=3) - timedelta(days=1)
        if end >= today:
            return start, end
    start = date(today.year + 1, first_month, 1)
    return start, start + relativedelta(months=3) - timedelta(days=1)


def _time_of(when: When, prefs: Prefs) -> tuple[time, Precision]:
    """Clock time and precision: HH:MM -> exact, slot -> slot, no time -> bare date / weekend time."""
    if when.time:
        if m := _HHMM_RE.match(when.time):
            return time(int(m[1]), int(m[2])), "exact"
        return prefs.slot(when.time), "slot"
    if when.day == "weekend":
        return prefs.weekend_time, "date"
    return prefs.bare_date_time, "date"


def _resolve_day(
    day: str | None, t: time, now_local: datetime, prefs: Prefs, *, timed: bool = True
) -> tuple[datetime, list[str], bool]:
    """Local aware datetime for ``day`` at ``t`` (+ warnings, needs_confirm). Raises ValueError for bad dates.

    ``timed=False``: the user named no time (``t`` is the bare-date default), so an explicit date (+N, dN, MM-DD,
    YYYY-MM-DD) that is today stays today even after ``t`` — that cut-off is not the user's — at a time still
    ahead (``_later_today``). Weekdays and "weekend" keep comparing the moment itself.
    """
    tz: ZoneInfo = prefs.zone
    today = now_local.date()

    def at(d: date) -> datetime:
        return localize(d, t, tz)

    def upcoming(d: date) -> bool:  # the moment itself is still ahead
        return at(d) > now_local

    def current(d: date) -> bool:  # an explicit date still counts: ahead, or today without a time of its own
        return upcoming(d) or (not timed and d == today)

    def place(d: date) -> datetime:
        return _later_today(now_local, prefs) if not timed and d == today and not upcoming(d) else at(d)

    if day is None:  # time only: today, or tomorrow once it has passed (no warning)
        return at(today if upcoming(today) else today + timedelta(days=1)), [], False
    if m := _OFFSET_RE.match(day):
        d = _offset_date(int(m[1]), m[2] or "d", now_local, prefs)
        if d == today and timed and not upcoming(d):
            return at(today + timedelta(days=1)), [WARN_MOVED_TOMORROW], True
        return place(d), [], False
    if day == "weekend":
        return at(_nearest_weekday(_SATURDAY, today, upcoming)), [], False
    if m := _WEEKDAY_RE.match(day):
        weekday = _DAY_CODES.index(m[2])
        if m[1]:  # "next FR": that weekday of the next Monday-based week
            return at(today - timedelta(days=today.weekday()) + timedelta(days=7 + weekday)), [], False
        return at(_nearest_weekday(weekday, today, upcoming)), [], False
    if m := _MONTHDAY_RE.match(day):
        return place(_month_day(int(m[1]), today, current)), [], False
    if m := _DATE_RE.match(day):
        year, month, dom = m.groups()
        if year is None:
            return place(_upcoming_month_day(int(month), int(dom), today.year, current)), [], False
        d = date(int(year), int(month), int(dom))
        if not current(d):  # explicit past date: probably a fact, keep it but ask
            return at(d), [WARN_PAST_DATE], True
        return place(d), [], False
    raise ValueError(f"unknown day spec: {day!r}")


def _offset_date(n: int, unit: str, now_local: datetime, prefs: Prefs) -> date:
    if unit == "d" and n >= 1:  # before night_cutoff "завтра" still means the calendar day that has just started
        return tomorrow(now_local, prefs) + timedelta(days=n - 1)
    step = {
        "d": relativedelta(days=n),
        "w": relativedelta(weeks=n),
        "mo": relativedelta(months=n),  # clamps: Jan 31 + 1 month -> Feb 28
        "y": relativedelta(years=n),
    }[unit]
    return now_local.date() + step


def _later_today(now_local: datetime, prefs: Prefs) -> datetime:
    """Due time for a date without a time that is today, once its bare-date time has passed: ``task_default_time``
    if still ahead, otherwise in ``_LATER_TODAY`` (at the latest 23:59)."""
    tz: ZoneInfo = prefs.zone
    today = now_local.date()
    default = localize(today, prefs.task_default_time, tz)
    if default > now_local:
        return default
    last_minute = localize(today + timedelta(days=1), time(0), tz) - timedelta(minutes=1)
    return min(now_local + _LATER_TODAY, last_minute)


def _nearest_weekday(weekday: int, today: date, upcoming: Callable[[date], bool]) -> date:
    """Nearest such weekday; today only if the time is still ahead, otherwise in a week."""
    d = today + timedelta(days=(weekday - today.weekday()) % 7)
    return d if d != today or upcoming(d) else d + timedelta(days=7)


def _month_day(n: int, today: date, current: Callable[[date], bool]) -> date:
    """N-th day of this month (clamped to its end; -1 = last day), or of the next month if already passed."""
    dom = 31 if n == -1 else n
    if not 1 <= dom <= 31:
        raise ValueError(f"bad day of month: {n}")
    first = today.replace(day=1)
    d = first + relativedelta(day=dom)
    return d if current(d) else first + relativedelta(months=1, day=dom)


def _upcoming_month_day(month: int, dom: int, year: int, current: Callable[[date], bool]) -> date:
    """MM-DD this year if still ahead, else the next year that has it (02-29 waits for a leap year)."""
    for y in range(year, year + 9):
        try:
            d = date(y, month, dom)
        except ValueError:
            continue
        if current(d):
            return d
    raise ValueError(f"bad date: {month:02d}-{dom:02d}")


def _resolve_at(at: str, now_utc: datetime, prefs: Prefs) -> WhenResult:
    """``at`` = local "YYYY-MM-DD[THH:MM]" for phrasings the spec cannot express: always confirmed.

    A time in ``at`` is the user's own (precision ``exact``: lead, no quiet-hours shift); a date alone gets an
    automatic time (``fallback``) like a day without a time, and today stays today.
    """
    day_s, _, time_s = at.partition("T")
    try:
        d = date.fromisoformat(day_s)
        t = time.fromisoformat(time_s) if time_s else None
    except ValueError:
        return None, "none", [WARN_BAD_TIME], True
    tz = prefs.zone
    precision: Precision = "exact"
    if t is None:
        now_local = now_utc.astimezone(tz)
        due_local, precision = localize(d, prefs.bare_date_time, tz), "fallback"
        if d == now_local.date() and due_local <= now_local:
            due_local = _later_today(now_local, prefs)
    else:
        due_local = localize(d, t, tz)
    due = due_local.astimezone(UTC)
    return due, precision, [WARN_PAST_DATE] if due <= now_utc else [], True


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


def _range_days(spec: str, today: date, tmr: date) -> tuple[date, date]:
    """[first, last) local dates for a range spec (``tmr``: the date "tomorrow" means, see ``tomorrow``)."""
    day = timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    fixed = {
        "today": (today, today + day),
        "tomorrow": (tmr, tmr + day),
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
