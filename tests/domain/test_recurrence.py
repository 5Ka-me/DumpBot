"""RRULE normalization, next occurrence in local wall time (DST, COUNT, UNTIL) and Russian descriptions."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from dumpbot.ai.schemas import RRULE_RE
from dumpbot.domain.recurrence import (
    complete_times,
    describe_rrule_ru,
    next_occurrence,
    normalize_rrule,
    rule_times,
    with_times,
    without_times,
)

TZ = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 6, 14, 5, tzinfo=TZ)
START = datetime(2026, 10, 6, 9, 0)  # naive local DTSTART, as stored in items.rec_dtstart_local


def L(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


def chain(rule: str, start: datetime, after: datetime, n: int) -> list[datetime]:
    out: list[datetime] = []
    for _ in range(n):
        nxt = next_occurrence(rule, start, TZ, after)
        if nxt is None:
            break
        out.append(nxt)
        after = nxt
    return out


# --- normalize_rrule ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("FREQ=WEEKLY;BYDAY=MO", "FREQ=WEEKLY;BYDAY=MO"),
        ("RRULE:FREQ=WEEKLY;BYDAY=MO", "FREQ=WEEKLY;BYDAY=MO"),
        ("rrule:freq=weekly;byday=mo,th", "FREQ=WEEKLY;BYDAY=MO,TH"),
        (" FREQ = DAILY ; INTERVAL = 2 ", "FREQ=DAILY;INTERVAL=2"),
        ("BYDAY=MO;FREQ=WEEKLY", "FREQ=WEEKLY;BYDAY=MO"),
        ("FREQ=MONTHLY;BYMONTHDAY=-1;", "FREQ=MONTHLY;BYMONTHDAY=-1"),
        ("FREQ=MONTHLY;BYDAY=FR;BYSETPOS=-1", "FREQ=MONTHLY;BYDAY=FR;BYSETPOS=-1"),
        ("FREQ=MONTHLY;BYDAY=-1FR", "FREQ=MONTHLY;BYDAY=-1FR"),
        ("FREQ=WEEKLY;UNTIL=20261231T225959Z", "FREQ=WEEKLY;UNTIL=20261231T225959Z"),
        ("FREQ=WEEKLY;UNTIL=20261231", "FREQ=WEEKLY;UNTIL=20261231"),
        ("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29"),
        ("FREQ=WEEKLY;WKST=SU;BYDAY=SU,MO;COUNT=4", "FREQ=WEEKLY;WKST=SU;BYDAY=SU,MO;COUNT=4"),
        ("FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0", "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0"),  # iteration 4, §2
        ("freq=daily;byhour=8,20;byminute=0,30;bysetpos=2,3", "FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0,30;BYSETPOS=2,3"),
    ],
)
def test_normalize(raw: str, expected: str):
    assert normalize_rrule(raw) == expected
    assert re.match(RRULE_RE, expected)  # what the LLM schema accepts
    assert normalize_rrule(expected) == expected  # idempotent


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "garbage",
        "BYDAY=MO",  # no FREQ
        "FREQ=HOURLY",
        "FREQ=WEEKLY;BYSECOND=9",
        "FREQ=DAILY;BYHOUR=24",
        "FREQ=DAILY;BYHOUR=9,9",
        "FREQ=DAILY;BYMINUTE=60",
        "FREQ=DAILY;BYHOUR=-1",
        "FREQ=DAILY;BYHOUR=0,1,2,3,4,5,6,7,8,9,10,11,12;BYMINUTE=0,30",  # 26 times a day
        "DTSTART:20261006T090000\nRRULE:FREQ=DAILY",
        "FREQ=WEEKLY;FREQ=DAILY",
        "FREQ=WEEKLY;COUNT=3;UNTIL=20261231",
        "FREQ=WEEKLY;INTERVAL=0",
        "FREQ=WEEKLY;INTERVAL=-1",
        "FREQ=WEEKLY;COUNT=0",
        "FREQ=WEEKLY;BYDAY=XX",
        "FREQ=WEEKLY;BYDAY=",
        "FREQ=MONTHLY;BYDAY=0FR",
        "FREQ=MONTHLY;BYMONTHDAY=32",
        "FREQ=MONTHLY;BYMONTHDAY=0",
        "FREQ=YEARLY;BYMONTH=13",
        "FREQ=YEARLY;BYMONTH=-1",
        "FREQ=MONTHLY;BYDAY=FR;BYSETPOS=0",
        "FREQ=WEEKLY;UNTIL=2026",
        "FREQ=WEEKLY;UNTIL=20261340",
        "FREQ=WEEKLY;WKST=XX",
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30",  # never occurs
        "FREQ=WEEKLY;UNTIL=19991231",  # nothing left
    ],
)
def test_normalize_rejects(raw: str):
    with pytest.raises(ValueError):
        normalize_rrule(raw)


# --- next_occurrence ----------------------------------------------------------------------------------------


def test_weekly_keeps_local_time_across_dst():
    got = chain("FREQ=WEEKLY;BYDAY=MO", START, NOW, 4)
    assert got == [L(2026, 10, 12, 9, 0), L(2026, 10, 19, 9, 0), L(2026, 10, 26, 9, 0), L(2026, 11, 2, 9, 0)]
    assert [g.hour for g in got] == [7, 7, 8, 8]  # UTC hours change, local 09:00 stays
    assert all(g.tzinfo is UTC for g in got)


def test_dtstart_not_matching_byday_is_not_an_occurrence():
    assert next_occurrence("FREQ=WEEKLY;BYDAY=MO", START, TZ, L(2026, 10, 6, 0, 0)) == L(2026, 10, 12, 9, 0)


def test_dtstart_itself_when_it_matches():
    after = L(2026, 10, 6, 0, 0)
    assert next_occurrence("FREQ=DAILY", START, TZ, after) == L(2026, 10, 6, 9, 0)


def test_inclusive_flag():
    at = L(2026, 10, 12, 9, 0)
    assert next_occurrence("FREQ=WEEKLY;BYDAY=MO", START, TZ, at) == L(2026, 10, 19, 9, 0)
    assert next_occurrence("FREQ=WEEKLY;BYDAY=MO", START, TZ, at, inclusive=True) == at


def test_monthly_day_5():
    assert chain("FREQ=MONTHLY;BYMONTHDAY=5", START, NOW, 3) == [
        L(2026, 11, 5, 9, 0),
        L(2026, 12, 5, 9, 0),
        L(2027, 1, 5, 9, 0),
    ]


def test_last_day_of_month():
    start = datetime(2027, 1, 10, 19, 0)
    got = chain("FREQ=MONTHLY;BYMONTHDAY=-1", start, L(2027, 1, 10, 20, 0), 4)
    assert got == [L(2027, 1, 31, 19, 0), L(2027, 2, 28, 19, 0), L(2027, 3, 31, 19, 0), L(2027, 4, 30, 19, 0)]


def test_bymonthday_31_skips_short_months():
    got = chain("FREQ=MONTHLY;BYMONTHDAY=31", START, NOW, 2)
    assert got == [L(2026, 10, 31, 9, 0), L(2026, 12, 31, 9, 0)]


def test_last_friday_of_month():
    got = chain("FREQ=MONTHLY;BYDAY=FR;BYSETPOS=-1", START, NOW, 3)
    assert got == [L(2026, 10, 30, 9, 0), L(2026, 11, 27, 9, 0), L(2026, 12, 25, 9, 0)]


def test_count_exhaustion():
    rule = "FREQ=DAILY;COUNT=3"
    assert chain(rule, START, L(2026, 10, 1, 0, 0), 10) == [
        L(2026, 10, 6, 9, 0),
        L(2026, 10, 7, 9, 0),
        L(2026, 10, 8, 9, 0),
    ]
    assert next_occurrence(rule, START, TZ, L(2026, 10, 8, 9, 0)) is None


def test_until_date_is_inclusive_whole_day():
    rule = "FREQ=DAILY;UNTIL=20261010"
    assert next_occurrence(rule, START, TZ, L(2026, 10, 10, 8, 0)) == L(2026, 10, 10, 9, 0)
    assert next_occurrence(rule, START, TZ, L(2026, 10, 10, 9, 0)) is None


def test_until_utc_is_converted_to_local():
    # 07:00Z = 09:00 CEST: the 09:00 occurrence on 10 Oct is the last one
    assert next_occurrence("FREQ=DAILY;UNTIL=20261010T070000Z", START, TZ, L(2026, 10, 10, 0, 0)) == L(
        2026, 10, 10, 9, 0
    )
    assert next_occurrence("FREQ=DAILY;UNTIL=20261010T065959Z", START, TZ, L(2026, 10, 10, 0, 0)) is None


def test_until_naive_local():
    rule = "FREQ=DAILY;UNTIL=20261010T090000"
    assert next_occurrence(rule, START, TZ, L(2026, 10, 9, 9, 0)) == L(2026, 10, 10, 9, 0)
    assert next_occurrence(rule, START, TZ, L(2026, 10, 10, 9, 0)) is None


def test_daily_spring_gap_is_shifted_forward():
    start = datetime(2027, 3, 26, 2, 30)
    got = chain("FREQ=DAILY", start, L(2027, 3, 27, 3, 0), 2)
    assert got[0] == datetime(2027, 3, 28, 1, 30, tzinfo=UTC)  # 02:30 does not exist -> 03:30 CEST
    assert got[1] == datetime(2027, 3, 29, 0, 30, tzinfo=UTC)  # back to 02:30 CEST


def test_daily_autumn_ambiguous_takes_first():
    start = datetime(2026, 10, 20, 2, 30)
    got = chain("FREQ=DAILY", start, L(2026, 10, 24, 12, 0), 2)
    assert got == [datetime(2026, 10, 25, 0, 30, tzinfo=UTC), datetime(2026, 10, 26, 1, 30, tzinfo=UTC)]


def test_interval_two_weeks():
    got = chain("FREQ=WEEKLY;INTERVAL=2;BYDAY=FR", START, NOW, 3)
    assert got == [L(2026, 10, 9, 9, 0), L(2026, 10, 23, 9, 0), L(2026, 11, 6, 9, 0)]


def test_yearly():
    assert next_occurrence("FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1", START, TZ, NOW) == L(2027, 1, 1, 9, 0)


def test_old_dtstart():
    start = datetime(2020, 1, 6, 9, 0)  # a Monday six years ago
    assert next_occurrence("FREQ=WEEKLY", start, TZ, NOW) == L(2026, 10, 12, 9, 0)


def test_aware_dtstart_is_accepted():
    start = L(2026, 10, 6, 9, 0)
    assert next_occurrence("FREQ=WEEKLY;BYDAY=MO", start, TZ, NOW) == L(2026, 10, 12, 9, 0)


def test_after_in_utc_and_local_is_the_same():
    assert next_occurrence("FREQ=DAILY", START, TZ, NOW.astimezone(UTC)) == L(2026, 10, 7, 9, 0)


def test_invalid_rule_raises():
    with pytest.raises(ValueError):
        next_occurrence("FREQ=WEEKLY;BYSECOND=9", START, TZ, NOW)


# --- describe_rrule_ru ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "text"),
    [
        ("FREQ=DAILY", "каждый день"),
        ("FREQ=DAILY;INTERVAL=2", "каждые 2 дня"),
        ("FREQ=DAILY;INTERVAL=5", "каждые 5 дней"),
        ("FREQ=DAILY;INTERVAL=21", "каждый 21 день"),
        ("FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR", "по будням"),
        ("FREQ=WEEKLY", "каждую неделю"),
        ("FREQ=WEEKLY;INTERVAL=2", "каждые 2 недели"),
        ("FREQ=WEEKLY;INTERVAL=5", "каждые 5 недель"),
        ("FREQ=WEEKLY;BYDAY=MO", "каждый понедельник"),
        ("FREQ=WEEKLY;BYDAY=TU", "каждый вторник"),
        ("FREQ=WEEKLY;BYDAY=WE", "каждую среду"),
        ("FREQ=WEEKLY;BYDAY=TH", "каждый четверг"),
        ("FREQ=WEEKLY;BYDAY=FR", "каждую пятницу"),
        ("FREQ=WEEKLY;BYDAY=SA", "каждую субботу"),
        ("FREQ=WEEKLY;BYDAY=SU", "каждое воскресенье"),
        ("FREQ=WEEKLY;BYDAY=MO,TH", "по пн и чт"),
        ("FREQ=WEEKLY;BYDAY=TH,MO", "по пн и чт"),
        ("FREQ=WEEKLY;BYDAY=MO,WE,FR", "по пн, ср и пт"),
        ("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "по будням"),
        ("FREQ=WEEKLY;BYDAY=SA,SU", "по выходным"),
        ("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU", "каждый день"),
        ("FREQ=WEEKLY;INTERVAL=2;BYDAY=FR", "каждые 2 недели по пт"),
        ("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH", "каждые 2 недели по пн и чт"),
        ("FREQ=MONTHLY", "каждый месяц"),
        ("FREQ=MONTHLY;INTERVAL=3", "каждые 3 месяца"),
        ("FREQ=MONTHLY;BYMONTHDAY=5", "5-го числа каждого месяца"),
        ("FREQ=MONTHLY;BYMONTHDAY=1,15", "1-го и 15-го числа каждого месяца"),
        ("FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=10", "10-го числа раз в 2 месяца"),
        ("FREQ=MONTHLY;BYMONTHDAY=-1", "в последний день месяца"),
        ("FREQ=MONTHLY;BYMONTHDAY=-2", "в предпоследний день месяца"),
        ("FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY=-1", "в последний день месяца, раз в 3 месяца"),
        ("FREQ=MONTHLY;BYDAY=FR;BYSETPOS=-1", "в последнюю пятницу месяца"),
        ("FREQ=MONTHLY;BYDAY=-1FR", "в последнюю пятницу месяца"),
        ("FREQ=MONTHLY;BYDAY=1MO", "в первый понедельник месяца"),
        ("FREQ=MONTHLY;BYDAY=2TU", "во второй вторник месяца"),
        ("FREQ=MONTHLY;BYDAY=SU;BYSETPOS=3", "в третье воскресенье месяца"),
        ("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1", "в последний будний день месяца"),
        ("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1", "в первый будний день месяца"),
        ("FREQ=YEARLY", "каждый год"),
        ("FREQ=YEARLY;INTERVAL=2", "каждые 2 года"),
        ("FREQ=YEARLY;INTERVAL=5", "каждые 5 лет"),
        ("FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1", "каждый год 1 янв"),
        ("FREQ=YEARLY;BYMONTH=5;BYMONTHDAY=9", "каждый год 9 мая"),
        ("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-1", "каждый год в последний день февраля"),
        ("FREQ=YEARLY;BYMONTH=11", "каждый год в ноябре"),
        ("FREQ=YEARLY;BYMONTH=11;BYDAY=-1FR", "каждый год в последнюю пятницу ноября"),
        ("FREQ=WEEKLY;BYDAY=SA,SU;COUNT=5", "по выходным, 5 раз"),
        ("FREQ=DAILY;COUNT=1", "каждый день, 1 раз"),
        ("FREQ=DAILY;COUNT=3", "каждый день, 3 раза"),
        ("FREQ=MONTHLY;BYMONTHDAY=-1;UNTIL=20261231", "в последний день месяца, до 31 дек 2026"),
        ("FREQ=WEEKLY;BYDAY=MO;UNTIL=20270115T230000Z", "каждый понедельник, до 15 янв 2027"),
        ("RRULE:freq=weekly;byday=mo", "каждый понедельник"),
        ("FREQ=WEEKLY;WKST=MO;BYDAY=MO", "каждый понедельник"),
    ],
)
def test_describe(rule: str, text: str):
    assert describe_rrule_ru(rule) == text


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=MONTHLY;BYDAY=MO",  # every Monday of the month: valid but unusual
        "FREQ=MONTHLY;BYMONTHDAY=1,-1",
        "FREQ=YEARLY;BYMONTH=1,7;BYMONTHDAY=1",
        "FREQ=MONTHLY;BYDAY=1MO,-1FR",
        "FREQ=DAILY;INTERVAL=2;BYDAY=MO",
        "FREQ=MONTHLY;BYMONTH=1;BYMONTHDAY=5",
    ],
)
def test_describe_exotic_falls_back_to_rule(rule: str):
    assert describe_rrule_ru(rule) == normalize_rrule(rule)


def test_describe_invalid_returns_input():
    assert describe_rrule_ru("FREQ=HOURLY") == "FREQ=HOURLY"


# --- several times a day (docs/features/iteration-4.md §2) ----------------------------------------------------------


def test_two_times_a_day():
    rule = "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0"
    assert chain(rule, START, L(2026, 10, 6, 8, 0), 4) == [
        L(2026, 10, 6, 9, 0),
        L(2026, 10, 6, 18, 0),
        L(2026, 10, 7, 9, 0),
        L(2026, 10, 7, 18, 0),
    ]
    assert next_occurrence(rule, START, TZ, NOW) == L(2026, 10, 6, 18, 0)  # 14:05: this evening's


def test_times_keep_the_wall_clock_across_dst():
    rule = "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0"
    assert chain(rule, START, L(2026, 10, 24, 19, 0), 3) == [
        L(2026, 10, 25, 9, 0),  # the autumn switch: still 09:00 local
        L(2026, 10, 25, 18, 0),
        L(2026, 10, 26, 9, 0),
    ]


def test_times_with_different_minutes_picked_by_setpos():
    rule = "FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0,30;BYSETPOS=2,3"
    assert rule_times(rule) == (time(8, 30), time(20, 0))
    expected = [L(2026, 10, 6, 8, 30), L(2026, 10, 6, 20, 0), L(2026, 10, 7, 8, 30)]
    assert chain(rule, datetime(2026, 10, 6, 8, 30), L(2026, 10, 6, 8, 0), 3) == expected


def test_weekly_with_times():
    rule = "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9,18;BYMINUTE=0"
    assert chain(rule, START, NOW, 3) == [L(2026, 10, 12, 9, 0), L(2026, 10, 12, 18, 0), L(2026, 10, 19, 9, 0)]


@pytest.mark.parametrize(
    ("rule", "times"),
    [
        ("FREQ=DAILY", ()),
        ("FREQ=DAILY;BYHOUR=18,9;BYMINUTE=0", (time(9, 0), time(18, 0))),
        ("FREQ=DAILY;BYHOUR=9;BYMINUTE=0,30", (time(9, 0), time(9, 30))),
        ("FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0,30;BYSETPOS=-1,1", (time(8, 0), time(20, 30))),
        ("FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1", ()),  # BYSETPOS of days, no times of its own
        ("FREQ=HOURLY", ()),  # invalid
        (None, ()),
    ],
)
def test_rule_times(rule: str | None, times: tuple[time, ...]):
    assert rule_times(rule) == times


def test_complete_times_takes_the_missing_part_from_the_start():
    assert complete_times("FREQ=DAILY;BYHOUR=9,18", time(9, 30)) == "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=30"
    assert complete_times("FREQ=DAILY;BYMINUTE=0,30", time(9, 0)) == "FREQ=DAILY;BYMINUTE=0,30;BYHOUR=9"
    assert complete_times("rrule:freq=weekly;byday=mo", time(9, 0)) == "FREQ=WEEKLY;BYDAY=MO"


@pytest.mark.parametrize(
    ("rule", "times", "expected"),
    [
        ("FREQ=DAILY", [time(9), time(18)], "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0"),
        (
            "FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0,30",
            [time(8, 30), time(20)],
            "FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0,30;BYSETPOS=2,3",
        ),
        # a weekly rule every week is the daily one with its days, so that BYSETPOS picks per day
        (
            "FREQ=WEEKLY;BYDAY=MO,TH",
            [time(8, 30), time(20)],
            "FREQ=DAILY;BYDAY=MO,TH;BYHOUR=8,20;BYMINUTE=0,30;BYSETPOS=2,3",
        ),
        ("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO", [time(8, 30), time(20)], None),
        ("FREQ=MONTHLY;BYMONTHDAY=1", [time(8, 30), time(20)], None),
        ("FREQ=MONTHLY;BYMONTHDAY=1", [time(9), time(18)], "FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9,18;BYMINUTE=0"),
        ("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1", [time(9), time(18)], None),  # it would pick one of them
        (
            "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1",
            [time(9)],
            "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1;BYHOUR=9;BYMINUTE=0",
        ),
        ("FREQ=DAILY", [], None),
    ],
)
def test_with_times(rule: str, times: list[time], expected: str | None):
    got = with_times(rule, times)
    assert got == expected
    if got is not None and "BYSETPOS=1;" not in got:
        assert rule_times(got) == tuple(sorted(times))


def test_without_times():
    assert without_times("FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0,30;BYSETPOS=2,3") == "FREQ=DAILY"
    assert without_times("FREQ=MONTHLY;BYDAY=FR;BYSETPOS=-1") == "FREQ=MONTHLY;BYDAY=FR;BYSETPOS=-1"


@pytest.mark.parametrize(
    ("rule", "text"),
    [
        ("FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0", "каждый день в 09:00 и 18:00"),
        ("FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0,30;BYSETPOS=2,3", "каждый день в 08:30 и 20:00"),
        ("FREQ=DAILY;BYHOUR=8,13,20;BYMINUTE=0", "каждый день в 08:00, 13:00 и 20:00"),
        ("FREQ=WEEKLY;BYDAY=MO;BYHOUR=9,18;BYMINUTE=0", "каждый понедельник в 09:00 и 18:00"),
        ("FREQ=DAILY;BYDAY=MO,TH;BYHOUR=8,20;BYMINUTE=0,30;BYSETPOS=2,3", "по пн и чт в 08:30 и 20:00"),
        ("FREQ=DAILY;BYHOUR=9;BYMINUTE=0;COUNT=5", "каждый день в 09:00, 5 раз"),
    ],
)
def test_describe_times(rule: str, text: str):
    assert describe_rrule_ru(rule) == text
