"""RRULE support: validation/normalization, next occurrence in local wall time, description in Russian.

Rules are RFC 5545 RRULEs without DTSTART. They are expanded with a *naive* local DTSTART so a 09:00 rule stays
09:00 across DST switches, and every occurrence is then mapped to UTC with ``localize``.

``localize`` lives here (and is re-exported by ``domain.timeresolve``, its contract home) because both this module
and ``domain.quiet`` need it while ``timeresolve`` imports them.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from dateutil.rrule import rrule, rrulestr

from dumpbot.domain.fmt import MONTHS_SHORT, WEEKDAYS_SHORT, plural_ru

ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "COUNT", "UNTIL", "WKST"}
)
DAY_CODES: Final[tuple[str, ...]] = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

_INT_RE = re.compile(r"^[+-]?\d{1,4}$")
_BYDAY_RE = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")
_UNTIL_RE = re.compile(r"^(\d{8})(?:T(\d{6})(Z?))?$")
# Validation expands the rule from a fixed start up to this horizon: a rule without any occurrence in 40 years
# (e.g. BYMONTH=2;BYMONTHDAY=30) never fires, and dateutil would otherwise search for one until year 9999.
_PROBE_START = datetime(2000, 1, 1)
_PROBE_END = datetime(2040, 1, 1)


def localize(d: date, t: time, tz: ZoneInfo) -> datetime:
    """Aware datetime in ``tz`` for a local wall date+time, DST-safe.

    Non-existent times (spring gap) are shifted forward by the gap; ambiguous ones (autumn) take the first
    instance (fold=0), as in RFC 5545 §3.3.5.
    """
    naive = datetime.combine(d, t.replace(tzinfo=None, fold=0))
    # The UTC round trip normalizes gap times: 02:30 on 2027-03-28 becomes 03:30 CEST.
    return naive.replace(tzinfo=tz).astimezone(UTC).astimezone(tz)


def normalize_rrule(rule: str) -> str:
    """Canonical RRULE: no "RRULE:" prefix, upper case, no spaces, FREQ first, known keys only.

    Raises ValueError for unknown/duplicate keys, bad values, COUNT together with UNTIL, unparsable rules and
    rules that never produce an occurrence.
    """
    text = re.sub(r"\s+", "", rule or "").upper()
    text = text.removeprefix("RRULE:")
    parts: dict[str, str] = {}
    for chunk in text.split(";"):
        if not chunk:
            continue
        key, sep, value = chunk.partition("=")
        if not sep or not value:
            raise ValueError(f"bad RRULE part: {chunk!r}")
        if key not in ALLOWED_KEYS:
            raise ValueError(f"RRULE key not allowed: {key}")
        if key in parts:
            raise ValueError(f"duplicate RRULE key: {key}")
        _check_value(key, value)
        parts[key] = value
    if "FREQ" not in parts:
        raise ValueError("RRULE without FREQ")
    if "COUNT" in parts and "UNTIL" in parts:
        raise ValueError("RRULE with both COUNT and UNTIL")
    normalized = _join({"FREQ": parts.pop("FREQ"), **parts})
    _ensure_occurs(normalized)
    return normalized


def next_occurrence(
    rule: str, dtstart_local: datetime, tz: ZoneInfo, after: datetime, *, inclusive: bool = False
) -> datetime | None:
    """Next occurrence (aware UTC) strictly after ``after`` (or at it when ``inclusive``).

    The rule is expanded in naive local time from ``dtstart_local`` (wall clock is kept across DST), then each
    occurrence is ``localize``-d. UNTIL is converted to naive local time. None when COUNT/UNTIL is exhausted.
    Raises ValueError for an invalid rule.
    """
    parts = _split(normalize_rrule(rule))
    start = _naive_local(dtstart_local, tz)
    after_utc = _as_utc(after)
    # Wall clock and UTC differ by a few hours at most, so a day of margin cannot skip a candidate.
    probe = after_utc.astimezone(tz).replace(tzinfo=None) - timedelta(days=1)
    for occ in _build(parts, start, tz).xafter(probe, inc=True):
        at = localize(occ.date(), occ.time(), tz).astimezone(UTC)
        if at > after_utc or (inclusive and at == after_utc):
            return at
    return None


def describe_rrule_ru(rule: str) -> str:
    """Human description: "каждый день", "каждый понедельник", "по пн и чт", "каждые 2 недели",
    "5-го числа каждого месяца", "в последнюю пятницу месяца", "каждый год 1 янв", "…, 5 раз", "…, до 31 дек 2026".

    Rules this function cannot phrase are returned as the (normalized) RRULE text.
    """
    try:
        normalized = normalize_rrule(rule)
    except ValueError:
        return (rule or "").strip()
    parts = _split(normalized)
    try:
        text = _describe_base(parts)
    except _Exotic:
        return normalized
    if "COUNT" in parts:
        n = int(parts["COUNT"])
        text += f", {n} {plural_ru(n, 'раз', 'раза', 'раз')}"
    if "UNTIL" in parts:
        d = datetime.strptime(parts["UNTIL"][:8], "%Y%m%d").date()
        text += f", до {d.day} {MONTHS_SHORT[d.month - 1]} {d.year}"
    return text


# --- parsing / validation ---------------------------------------------------------------------------------


def _split(rule: str) -> dict[str, str]:
    return dict(chunk.split("=", 1) for chunk in rule.split(";") if chunk)


def _join(parts: dict[str, str]) -> str:
    return ";".join(f"{k}={v}" for k, v in parts.items())


def _ints(value: str) -> list[int]:
    out = []
    for item in value.split(","):
        if not _INT_RE.match(item):
            raise ValueError(f"not an integer: {item!r}")
        out.append(int(item))
    return out


def _check_range(values: list[int], low: int, high: int, *, signed: bool) -> None:
    for v in values:
        if not (low <= abs(v) <= high) or (v < 0 and not signed):
            raise ValueError(f"value out of range: {v}")


def _check_value(key: str, value: str) -> None:
    match key:
        case "FREQ":
            if value not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
                raise ValueError(f"FREQ not allowed: {value}")
        case "INTERVAL":
            _check_range(_one_int(value), 1, 999, signed=False)
        case "COUNT":
            _check_range(_one_int(value), 1, 9999, signed=False)
        case "BYMONTHDAY":
            _check_range(_ints(value), 1, 31, signed=True)
        case "BYMONTH":
            _check_range(_ints(value), 1, 12, signed=False)
        case "BYSETPOS":
            _check_range(_ints(value), 1, 366, signed=True)
        case "BYDAY":
            for item in value.split(","):
                m = _BYDAY_RE.match(item)
                if not m or (m[1] is not None and not 1 <= abs(int(m[1])) <= 53):
                    raise ValueError(f"bad BYDAY: {item!r}")
        case "WKST":
            if value not in DAY_CODES:
                raise ValueError(f"bad WKST: {value}")
        case "UNTIL":
            _parse_until(value)


def _one_int(value: str) -> list[int]:
    values = _ints(value)
    if len(values) != 1:
        raise ValueError(f"expected one integer: {value!r}")
    return values


def _parse_until(value: str) -> tuple[datetime, bool]:
    """(naive datetime, is_utc). A date-only UNTIL means the whole day (inclusive)."""
    m = _UNTIL_RE.match(value)
    if not m:
        raise ValueError(f"bad UNTIL: {value!r}")
    if m[2] is None:
        return datetime.combine(datetime.strptime(m[1], "%Y%m%d").date(), time(23, 59, 59)), False
    return datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S"), m[3] == "Z"


def _until_local(value: str, tz: ZoneInfo) -> datetime:
    """UNTIL as naive local wall time: dateutil refuses a UTC UNTIL with a naive DTSTART."""
    until, is_utc = _parse_until(value)
    if is_utc:
        return until.replace(tzinfo=UTC).astimezone(tz).replace(tzinfo=None)
    return until


def _build(parts: dict[str, str], dtstart: datetime, tz: ZoneInfo) -> rrule:
    parts = dict(parts)
    if "UNTIL" in parts:
        parts["UNTIL"] = _until_local(parts["UNTIL"], tz).strftime("%Y%m%dT%H%M%S")
    try:
        return rrulestr(_join(parts), dtstart=dtstart)
    except Exception as e:  # dateutil raises assorted errors for odd input
        raise ValueError(f"unparsable RRULE: {e}") from e


def _ensure_occurs(rule: str) -> None:
    parts = _split(rule)
    parts.pop("COUNT", None)  # COUNT only limits; it cannot make an impossible rule possible
    until = _until_local(parts["UNTIL"], UTC) if "UNTIL" in parts else _PROBE_END
    parts["UNTIL"] = min(until, _PROBE_END).strftime("%Y%m%dT%H%M%S")
    if next(iter(_build(parts, _PROBE_START, UTC)), None) is None:
        raise ValueError("RRULE never occurs")


def _naive_local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _as_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# --- Russian description --------------------------------------------------------------------------------------


class _Exotic(Exception):
    """The rule is valid but too unusual to phrase; the raw rule is shown instead."""


# Accusative weekday names with grammatical gender (for "каждую пятницу", "в последнее воскресенье").
_DAY_ACC: Final[dict[str, tuple[str, str]]] = {
    "MO": ("понедельник", "m"),
    "TU": ("вторник", "m"),
    "WE": ("среду", "f"),
    "TH": ("четверг", "m"),
    "FR": ("пятницу", "f"),
    "SA": ("субботу", "f"),
    "SU": ("воскресенье", "n"),
}
_EACH: Final[dict[str, str]] = {"m": "каждый", "f": "каждую", "n": "каждое"}
# Accusative ordinals by gender; the preposition is "во" before "втор…".
_ORDINALS: Final[dict[int, tuple[str, str, str]]] = {
    1: ("первый", "первую", "первое"),
    2: ("второй", "вторую", "второе"),
    3: ("третий", "третью", "третье"),
    4: ("четвёртый", "четвёртую", "четвёртое"),
    5: ("пятый", "пятую", "пятое"),
    -1: ("последний", "последнюю", "последнее"),
    -2: ("предпоследний", "предпоследнюю", "предпоследнее"),
}
_GENDER_INDEX: Final[dict[str, int]] = {"m": 0, "f": 1, "n": 2}
_MONTHS_GEN: Final[tuple[str, ...]] = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_MONTHS_PREP: Final[tuple[str, ...]] = (
    "январе",
    "феврале",
    "марте",
    "апреле",
    "мае",
    "июне",
    "июле",
    "августе",
    "сентябре",
    "октябре",
    "ноябре",
    "декабре",
)
_WEEKDAYS: Final[frozenset[str]] = frozenset({"MO", "TU", "WE", "TH", "FR"})
_WEEKEND: Final[frozenset[str]] = frozenset({"SA", "SU"})
# Unit forms: (one, few, many) and the "каждый" form agreeing with the unit.
_UNITS: Final[dict[str, tuple[tuple[str, str, str], str]]] = {
    "DAILY": (("день", "дня", "дней"), "каждый"),
    "WEEKLY": (("неделю", "недели", "недель"), "каждую"),
    "MONTHLY": (("месяц", "месяца", "месяцев"), "каждый"),
    "YEARLY": (("год", "года", "лет"), "каждый"),
}


def _every(freq: str, n: int) -> str:
    """Interval phrase: "каждый день" / "каждые 2 недели" / "каждые 5 лет" / "каждый 21 день"."""
    forms, each_one = _UNITS[freq]
    if n == 1:
        return f"{each_one} {forms[0]}"
    word = plural_ru(n, *forms)
    return f"{each_one if word == forms[0] else 'каждые'} {n} {word}"


def _byday(parts: dict[str, str]) -> list[tuple[int | None, str]]:
    out: list[tuple[int | None, str]] = []
    for item in parts.get("BYDAY", "").split(","):
        if m := _BYDAY_RE.match(item):
            out.append((int(m[1]) if m[1] else None, m[2]))
    return out


def _list_ru(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " и " + words[-1]


def _days_po(codes: list[str]) -> str:
    """Weekday list: "по будням", "по выходным", "по пн и чт"."""
    found = set(codes)
    if found == _WEEKDAYS:
        return "по будням"
    if found == _WEEKEND:
        return "по выходным"
    ordered = [c for c in DAY_CODES if c in found]
    return "по " + _list_ru([WEEKDAYS_SHORT[DAY_CODES.index(c)] for c in ordered])


def _days_each(codes: list[str]) -> str:
    """Weekly with interval 1: "каждый понедельник", "каждый день", or the "по …" form."""
    found = set(codes)
    if len(found) == 7:
        return "каждый день"
    if len(found) == 1:
        name, gender = _DAY_ACC[codes[0]]
        return f"{_EACH[gender]} {name}"
    return _days_po(codes)


def _nth_day(pos: int, code: str) -> str:
    """Ordinal weekday: "в последнюю пятницу", "во второй вторник"."""
    if pos not in _ORDINALS:
        raise _Exotic
    name, gender = _DAY_ACC[code]
    ordinal = _ORDINALS[pos][_GENDER_INDEX[gender]]
    return f"{'во' if pos == 2 else 'в'} {ordinal} {name}"


def _nth_weekday_phrase(parts: dict[str, str]) -> str | None:
    """BYDAY with an ordinal (-1FR) or BYDAY+BYSETPOS -> "в последнюю пятницу", "в первый будний день"."""
    days = _byday(parts)
    setpos = _ints(parts["BYSETPOS"]) if "BYSETPOS" in parts else []
    if not days or len(setpos) > 1:
        raise _Exotic
    if setpos:
        if any(pos is not None for pos, _ in days):
            raise _Exotic
        codes = {c for _, c in days}
        if len(codes) == 1:
            return _nth_day(setpos[0], days[0][1])
        if codes == _WEEKDAYS and setpos[0] in _ORDINALS:
            prep = "во" if setpos[0] == 2 else "в"
            return f"{prep} {_ORDINALS[setpos[0]][0]} будний день"
        raise _Exotic
    if len(days) == 1 and days[0][0] is not None:
        return _nth_day(days[0][0], days[0][1])
    return None


def _monthdays_phrase(values: list[int]) -> str:
    """Month days: "5-го числа", "1-го и 15-го числа", "в последний день"."""
    if values == [-1]:
        return "в последний день"
    if values == [-2]:
        return "в предпоследний день"
    if any(v < 0 for v in values):
        raise _Exotic
    return _list_ru([f"{v}-го" for v in sorted(set(values))]) + " числа"


def _describe_base(parts: dict[str, str]) -> str:
    freq = parts["FREQ"]
    n = int(parts.get("INTERVAL", "1"))
    keys = {k for k in parts if k.startswith("BY")}
    if not keys:
        return _every(freq, n)
    if freq in ("DAILY", "WEEKLY"):
        return _describe_weekly(parts, keys, freq, n)
    if freq == "MONTHLY":
        return _describe_monthly(parts, keys, n)
    return _describe_yearly(parts, keys, n)


def _describe_weekly(parts: dict[str, str], keys: set[str], freq: str, n: int) -> str:
    days = _byday(parts)
    if keys != {"BYDAY"} or any(pos is not None for pos, _ in days):
        raise _Exotic
    codes = [c for _, c in days]
    if n == 1:
        return _days_each(codes)
    if freq == "DAILY":  # "every 2 days, only on Mondays" is not worth phrasing
        raise _Exotic
    return f"{_every(freq, n)} {_days_po(codes)}"


def _describe_monthly(parts: dict[str, str], keys: set[str], n: int) -> str:
    every = "каждого месяца" if n == 1 else f"раз в {n} {plural_ru(n, 'месяц', 'месяца', 'месяцев')}"
    if keys == {"BYMONTHDAY"}:
        values = _ints(parts["BYMONTHDAY"])
        phrase = _monthdays_phrase(values)
        if values[0] < 0:
            return f"{phrase} месяца" + ("" if n == 1 else f", {every}")
        return f"{phrase} {every}"
    if keys in ({"BYDAY"}, {"BYDAY", "BYSETPOS"}):
        phrase = _nth_weekday_phrase(parts)
        if phrase is None:
            raise _Exotic
        return f"{phrase} месяца" + ("" if n == 1 else f", {every}")
    raise _Exotic


def _describe_yearly(parts: dict[str, str], keys: set[str], n: int) -> str:
    every = _every("YEARLY", n)
    months = _ints(parts["BYMONTH"]) if "BYMONTH" in parts else []
    if len(months) != 1:
        raise _Exotic
    month = months[0]
    if keys == {"BYMONTH"}:
        return f"{every} в {_MONTHS_PREP[month - 1]}"
    if keys == {"BYMONTH", "BYMONTHDAY"}:
        values = _ints(parts["BYMONTHDAY"])
        if values == [-1]:
            return f"{every} в последний день {_MONTHS_GEN[month - 1]}"
        if len(values) == 1 and values[0] > 0:
            return f"{every} {values[0]} {MONTHS_SHORT[month - 1]}"
        raise _Exotic
    if keys in ({"BYMONTH", "BYDAY"}, {"BYMONTH", "BYDAY", "BYSETPOS"}):
        phrase = _nth_weekday_phrase(parts)
        if phrase is None:
            raise _Exotic
        return f"{every} {phrase} {_MONTHS_GEN[month - 1]}"
    raise _Exotic
