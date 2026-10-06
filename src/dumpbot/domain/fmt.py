"""Russian formatting of dates, times and durations for bot messages ("пт 9 окт 15:00")."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Final
from zoneinfo import ZoneInfo

from dumpbot import clock

WEEKDAYS_SHORT: Final[tuple[str, ...]] = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
# Genitive abbreviations: "9 окт", "1 мая".
MONTHS_SHORT: Final[tuple[str, ...]] = (
    "янв",
    "фев",
    "мар",
    "апр",
    "мая",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
)


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Pick the Russian plural form for ``n``: plural_ru(5, "задача", "задачи", "задач") -> "задач".

    Returns only the word, so callers write ``f"{n} {plural_ru(n, ...)}"``.
    """
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def fmt_time(t: time) -> str:
    """Clock time as "13:00"."""
    return f"{t.hour:02d}:{t.minute:02d}"


def fmt_date(d: date, *, today: date | None = None) -> str:
    """Local date: "сегодня" / "завтра" / "вчера" (only with ``today``), "пт 9 окт", "9 окт 2027" (other year)."""
    d = _as_date(d)
    if today is not None:
        today = _as_date(today)
        relative = {0: "сегодня", 1: "завтра", -1: "вчера"}.get((d - today).days)
        if relative:
            return relative
        if d.year != today.year:
            return f"{d.day} {MONTHS_SHORT[d.month - 1]} {d.year}"
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d.day} {MONTHS_SHORT[d.month - 1]}"


def fmt_dt(dt: datetime, tz: ZoneInfo, *, now: datetime | None = None) -> str:
    """Local date and time in ``tz``: "сегодня 13:00", "пт 9 окт 15:00", "9 окт 2027 15:00".

    "Today" is taken from ``now`` (default: the current time) converted to ``tz``.
    """
    local = _aware(dt).astimezone(tz)
    ref = _aware(now if now is not None else clock.utcnow()).astimezone(tz)
    return f"{fmt_date(local.date(), today=ref.date())} {fmt_time(local.time())}"


def fmt_duration(minutes: int) -> str:
    """Duration: "5 мин", "1 ч", "1 ч 30 мин", "1 дн", "2 дн 3 ч"."""
    sign = "-" if minutes < 0 else ""
    days, rest = divmod(abs(minutes), 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} дн" if days else "", f"{hours} ч" if hours else "", f"{mins} мин" if mins else ""]
    text = " ".join(p for p in parts if p)
    return sign + text if text else "0 мин"


def _as_date(d: date) -> date:
    # datetime is a subclass of date; arithmetic between the two would fail.
    return d.date() if isinstance(d, datetime) else d


def _aware(dt: datetime) -> datetime:
    """Naive datetimes are treated as UTC (the convention for every datetime in the API)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
