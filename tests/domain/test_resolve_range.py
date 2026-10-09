"""resolve_range: QuerySpec.range -> [start, end) in UTC on the local calendar."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from dumpbot.ai.schemas import RANGE_RE
from dumpbot.domain.prefs import Prefs
from dumpbot.domain.timeresolve import localize, resolve_range, tonight

TZ = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 6, 14, 5, tzinfo=TZ)  # Tuesday


def midnight(y: int, m: int, d: int) -> datetime:
    return localize(date(y, m, d), time(0), TZ).astimezone(UTC)


def span(a: tuple[int, int, int], b: tuple[int, int, int]) -> tuple[datetime, datetime]:
    return midnight(*a), midnight(*b)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("today", span((2026, 10, 6), (2026, 10, 7))),
        ("tomorrow", span((2026, 10, 7), (2026, 10, 8))),
        ("yesterday", span((2026, 10, 5), (2026, 10, 6))),
        ("this_week", span((2026, 10, 5), (2026, 10, 12))),
        ("next_week", span((2026, 10, 12), (2026, 10, 19))),
        ("last_week", span((2026, 9, 28), (2026, 10, 5))),
        ("this_month", span((2026, 10, 1), (2026, 11, 1))),
        ("last_month", span((2026, 9, 1), (2026, 10, 1))),
        ("past:7", span((2026, 9, 30), (2026, 10, 7))),
        ("past:1", span((2026, 10, 6), (2026, 10, 7))),
        ("past:0", span((2026, 10, 6), (2026, 10, 7))),
        ("past:30", span((2026, 9, 7), (2026, 10, 7))),
        ("next:7", span((2026, 10, 6), (2026, 10, 14))),
        ("next:1", span((2026, 10, 6), (2026, 10, 8))),
        ("next:0", span((2026, 10, 6), (2026, 10, 7))),
        ("2026-10-20", span((2026, 10, 20), (2026, 10, 21))),
        ("2026-10-01..2026-10-03", span((2026, 10, 1), (2026, 10, 4))),
        ("2026-10-03..2026-10-01", span((2026, 10, 1), (2026, 10, 4))),  # reversed
        ("2026-10-05..2026-10-05", span((2026, 10, 5), (2026, 10, 6))),
    ],
)
def test_forms(spec: str, expected: tuple[datetime, datetime], prefs: Prefs):
    assert re.match(RANGE_RE, spec)  # every form the LLM schema allows
    assert resolve_range(spec, NOW, prefs) == expected


def test_today_in_utc_values(prefs: Prefs):
    start, end = resolve_range("today", NOW, prefs)
    assert (start, end) == (datetime(2026, 10, 5, 22, 0, tzinfo=UTC), datetime(2026, 10, 6, 22, 0, tzinfo=UTC))
    assert start.tzinfo is UTC and end.tzinfo is UTC


def test_dst_day_is_25_hours(prefs: Prefs):
    start, end = resolve_range("2026-10-25", NOW, prefs)
    assert start == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=25)


def test_this_month_spans_dst(prefs: Prefs):
    start, end = resolve_range("this_month", NOW, prefs)
    assert (start, end) == (datetime(2026, 9, 30, 22, 0, tzinfo=UTC), datetime(2026, 10, 31, 23, 0, tzinfo=UTC))


def test_local_date_not_utc_date(prefs: Prefs):
    now = datetime(2026, 10, 6, 23, 30, tzinfo=UTC)  # already 7 Oct 01:30 in Warsaw
    assert resolve_range("today", now, prefs) == span((2026, 10, 7), (2026, 10, 8))


@pytest.mark.parametrize(
    ("now", "spec", "expected"),
    [
        # Monday: the week starts today
        (datetime(2026, 10, 5, 0, 0, tzinfo=TZ), "this_week", span((2026, 10, 5), (2026, 10, 12))),
        (datetime(2026, 10, 5, 9, 0, tzinfo=TZ), "last_week", span((2026, 9, 28), (2026, 10, 5))),
        # Sunday late evening: still the week that started on Monday
        (datetime(2026, 10, 11, 23, 59, tzinfo=TZ), "this_week", span((2026, 10, 5), (2026, 10, 12))),
        (datetime(2026, 10, 11, 23, 59, tzinfo=TZ), "next_week", span((2026, 10, 12), (2026, 10, 19))),
        # week crossing a month / year boundary
        (datetime(2026, 12, 31, 12, 0, tzinfo=TZ), "this_week", span((2026, 12, 28), (2027, 1, 4))),
        # month edges
        (datetime(2026, 12, 15, 12, 0, tzinfo=TZ), "this_month", span((2026, 12, 1), (2027, 1, 1))),
        (datetime(2027, 1, 15, 12, 0, tzinfo=TZ), "last_month", span((2026, 12, 1), (2027, 1, 1))),
        (datetime(2027, 3, 31, 12, 0, tzinfo=TZ), "last_month", span((2027, 2, 1), (2027, 3, 1))),
        (datetime(2028, 2, 29, 12, 0, tzinfo=TZ), "this_month", span((2028, 2, 1), (2028, 3, 1))),
        (datetime(2026, 10, 31, 23, 59, tzinfo=TZ), "this_month", span((2026, 10, 1), (2026, 11, 1))),
        (datetime(2026, 11, 1, 0, 0, tzinfo=TZ), "this_month", span((2026, 11, 1), (2026, 12, 1))),
        (datetime(2027, 1, 1, 0, 30, tzinfo=TZ), "yesterday", span((2026, 12, 31), (2027, 1, 1))),
        (datetime(2026, 12, 31, 12, 0, tzinfo=TZ), "tomorrow", span((2027, 1, 1), (2027, 1, 2))),
    ],
)
def test_boundaries(now: datetime, spec: str, expected: tuple[datetime, datetime], prefs: Prefs):
    assert resolve_range(spec, now, prefs) == expected


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("tomorrow", span((2026, 10, 7), (2026, 10, 8))),  # the day "завтра в 10" was saved for at this hour
        ("today", span((2026, 10, 7), (2026, 10, 8))),  # calendar days, like the day spec "+0"
        ("yesterday", span((2026, 10, 6), (2026, 10, 7))),
    ],
)
def test_tomorrow_before_night_cutoff_is_the_day_that_has_just_started(
    spec: str, expected: tuple[datetime, datetime], prefs: Prefs
):
    now = datetime(2026, 10, 7, 1, 30, tzinfo=TZ)  # Wed 01:30, before night_cutoff (04:00)
    assert resolve_range(spec, now, prefs) == expected
    assert resolve_range("tomorrow", datetime(2026, 10, 7, 4, 0, tzinfo=TZ), prefs) == span(
        (2026, 10, 8), (2026, 10, 9)
    )


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 6, 10, 0, tzinfo=TZ), datetime(2026, 10, 6, 19, 0, tzinfo=TZ)),
        (
            datetime(2026, 10, 7, 1, 30, tzinfo=TZ),
            datetime(2026, 10, 7, 19, 0, tzinfo=TZ),
        ),  # the local date, not night_cutoff's
        (datetime(2026, 10, 6, 21, 0, tzinfo=TZ), datetime(2026, 10, 6, 19, 0, tzinfo=TZ)),  # already past
        (datetime(2026, 10, 25, 10, 0, tzinfo=TZ), datetime(2026, 10, 25, 19, 0, tzinfo=TZ)),  # DST ends that night
    ],
)
def test_tonight_is_the_evening_slot_of_the_local_day(now: datetime, expected: datetime, prefs: Prefs):
    # [🌙 Вечером] of a reminder (iteration 3, §4)
    assert tonight(now, prefs) == expected.astimezone(UTC)
    assert tonight(now, Prefs(slot_evening=time(20, 30))) == expected.replace(hour=20, minute=30).astimezone(UTC)


def test_other_timezone(prefs: Prefs):
    prefs = Prefs(tz="America/New_York")
    start, end = resolve_range("today", NOW, prefs)  # 08:05 in New York
    assert (start, end) == (datetime(2026, 10, 6, 4, 0, tzinfo=UTC), datetime(2026, 10, 7, 4, 0, tzinfo=UTC))


@pytest.mark.parametrize("spec", ["", "someday", "next:x", "2026-13-01", "2026-10-01..", "this_year"])
def test_invalid(spec: str, prefs: Prefs):
    with pytest.raises(ValueError):
        resolve_range(spec, NOW, prefs)
