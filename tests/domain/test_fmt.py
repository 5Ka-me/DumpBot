"""Russian formatting helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from dumpbot import clock
from dumpbot.domain.fmt import MONTHS_SHORT, fmt_date, fmt_day, fmt_dt, fmt_duration, fmt_time, plural_ru

TZ = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 6, 14, 5, tzinfo=TZ)  # Tuesday
TODAY = date(2026, 10, 6)


@pytest.mark.parametrize(("t", "text"), [(time(9, 5), "09:05"), (time(0, 0), "00:00"), (time(23, 59, 59), "23:59")])
def test_fmt_time(t: time, text: str):
    assert fmt_time(t) == text


@pytest.mark.parametrize(
    ("d", "text"),
    [
        (date(2026, 10, 6), "сегодня"),
        (date(2026, 10, 7), "завтра"),
        (date(2026, 10, 5), "вчера"),
        (date(2026, 10, 8), "чт 8 окт"),
        (date(2026, 10, 9), "пт 9 окт"),
        (date(2026, 10, 4), "вс 4 окт"),
        (date(2026, 5, 1), "пт 1 мая"),
        (date(2026, 12, 31), "чт 31 дек"),
        (date(2027, 10, 9), "9 окт 2027"),  # other year: no weekday, with year
        (date(2025, 1, 2), "2 янв 2025"),
        (date(2027, 1, 1), "1 янв 2027"),
    ],
)
def test_fmt_date_relative(d: date, text: str):
    assert fmt_date(d, today=TODAY) == text


def test_fmt_date_tomorrow_across_new_year():
    assert fmt_date(date(2027, 1, 1), today=date(2026, 12, 31)) == "завтра"


def test_fmt_date_without_today():
    assert fmt_date(date(2026, 10, 6)) == "вт 6 окт"
    assert fmt_date(date(2027, 10, 9)) == "сб 9 окт"


def test_fmt_date_accepts_datetimes():
    assert fmt_date(datetime(2026, 10, 7, 23, 0), today=datetime(2026, 10, 6, 1, 0)) == "завтра"


def test_month_abbreviations_are_genitive():
    assert MONTHS_SHORT == ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
    assert [fmt_date(date(2026, m, 15)).split()[-1] for m in range(1, 13)] == list(MONTHS_SHORT)


@pytest.mark.parametrize(
    ("dt", "text"),
    [
        (datetime(2026, 10, 6, 13, 0, tzinfo=TZ), "сегодня 13:00"),
        (datetime(2026, 10, 7, 9, 0, tzinfo=TZ), "завтра 09:00"),
        (datetime(2026, 10, 5, 18, 0, tzinfo=TZ), "вчера 18:00"),
        (datetime(2026, 10, 9, 15, 0, tzinfo=TZ), "пт 9 окт 15:00"),
        (datetime(2027, 10, 9, 15, 0, tzinfo=TZ), "9 окт 2027 15:00"),
        (datetime(2026, 10, 9, 13, 0, tzinfo=UTC), "пт 9 окт 15:00"),  # converted to local time
        (datetime(2026, 10, 6, 22, 30, tzinfo=UTC), "завтра 00:30"),  # local date differs from UTC date
        (datetime(2026, 11, 2, 8, 0, tzinfo=UTC), "пн 2 ноя 09:00"),  # winter time
        (datetime(2026, 10, 9, 13, 0), "пт 9 окт 15:00"),  # naive = UTC
    ],
)
def test_fmt_dt(dt: datetime, text: str):
    assert fmt_dt(dt, TZ, now=NOW) == text


def test_fmt_dt_today_is_local_today():
    now = datetime(2026, 10, 6, 23, 30, tzinfo=UTC)  # 7 Oct 01:30 in Warsaw
    assert fmt_dt(datetime(2026, 10, 7, 7, 0, tzinfo=UTC), TZ, now=now) == "сегодня 09:00"


def test_fmt_dt_default_now_uses_clock(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(clock, "utcnow", lambda: NOW.astimezone(UTC))
    assert fmt_dt(datetime(2026, 10, 7, 9, 0, tzinfo=TZ), TZ) == "завтра 09:00"


@pytest.mark.parametrize(
    ("minutes", "text"),
    [
        (0, "0 мин"),
        (1, "1 мин"),
        (5, "5 мин"),
        (59, "59 мин"),
        (60, "1 ч"),
        (90, "1 ч 30 мин"),
        (120, "2 ч"),
        (1439, "23 ч 59 мин"),
        (1440, "1 дн"),
        (1441, "1 дн 1 мин"),
        (1500, "1 дн 1 ч"),
        (2880, "2 дн"),
        (10080, "7 дн"),
        (-5, "-5 мин"),
    ],
)
def test_fmt_duration(minutes: int, text: str):
    assert fmt_duration(minutes) == text


@pytest.mark.parametrize(
    ("n", "word"),
    [
        (0, "задач"),
        (1, "задача"),
        (2, "задачи"),
        (4, "задачи"),
        (5, "задач"),
        (11, "задач"),
        (12, "задач"),
        (14, "задач"),
        (21, "задача"),
        (22, "задачи"),
        (25, "задач"),
        (101, "задача"),
        (111, "задач"),
        (112, "задач"),
        (1004, "задачи"),
        (-1, "задача"),
    ],
)
def test_plural_ru(n: int, word: str):
    assert plural_ru(n, "задача", "задачи", "задач") == word


@pytest.mark.parametrize(
    ("d", "text"),
    [
        (date(2026, 10, 6), "вт 6 окт"),
        (date(2026, 10, 7), "ср 7 окт"),
        (date(2026, 10, 11), "вс 11 окт"),
        (date(2027, 8, 31), "31 авг 2027"),
    ],
)
def test_fmt_date_without_relative_words(d: date, text: str):
    assert fmt_date(d, today=TODAY, relative=False) == text


@pytest.mark.parametrize(
    ("d", "text"),
    [
        (date(2026, 10, 6), "сегодня"),
        (date(2026, 10, 7), "завтра"),
        (date(2026, 10, 8), "чт"),
        (date(2026, 10, 9), "пт"),
        (date(2026, 10, 12), "пн"),  # six days ahead: still unambiguous
        (date(2026, 10, 13), "вт 13 окт"),  # a week ahead: the same weekday as today
        (date(2026, 10, 5), "пн 5 окт"),
        (date(2027, 6, 1), "1 июн 2027"),
        (datetime(2026, 10, 9, 19, 0), "пт"),
    ],
)
def test_fmt_day(d: date, text: str):
    assert fmt_day(d, today=TODAY) == text
