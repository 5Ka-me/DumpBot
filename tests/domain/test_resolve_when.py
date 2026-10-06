"""resolve_when + localize: every row of the rules table in docs/architecture.md §4.1."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from dumpbot.ai.schemas import When
from dumpbot.domain.prefs import Prefs
from dumpbot.domain.timeresolve import (
    WARN_BAD_TIME,
    WARN_MOVED_TOMORROW,
    WARN_PAST_DATE,
    localize,
    resolve_when,
)

TZ = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 6, 14, 5, tzinfo=TZ)  # Tuesday


def L(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


def resolve(now: datetime = NOW, prefs: Prefs | None = None, **when: object):
    return resolve_when(When(**when), now, prefs or Prefs())


# --- localize -----------------------------------------------------------------------------------------------


def test_localize_regular_time():
    dt = localize(date(2026, 10, 6), time(9, 0), TZ)
    assert dt.tzinfo is TZ
    assert dt.astimezone(UTC) == datetime(2026, 10, 6, 7, 0, tzinfo=UTC)


def test_localize_spring_gap_moves_forward():
    dt = localize(date(2027, 3, 28), time(2, 30), TZ)  # 02:00-03:00 does not exist
    assert (dt.hour, dt.minute) == (3, 30)
    assert dt.utcoffset() == timedelta(hours=2)
    assert dt.astimezone(UTC) == datetime(2027, 3, 28, 1, 30, tzinfo=UTC)


@pytest.mark.parametrize("fold", [0, 1])
def test_localize_autumn_ambiguous_takes_first(fold: int):
    dt = localize(date(2026, 10, 25), time(2, 30, fold=fold), TZ)  # 02:00-03:00 happens twice
    assert dt.utcoffset() == timedelta(hours=2)
    assert dt.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_localize_winter_offset():
    assert localize(date(2026, 12, 1), time(9, 0), TZ).astimezone(UTC) == datetime(2026, 12, 1, 8, 0, tzinfo=UTC)


# --- result shape -------------------------------------------------------------------------------------------


def test_due_is_aware_utc():
    due, _, _, _ = resolve(day="+1", time="09:00")
    assert due is not None
    assert due.tzinfo is UTC
    assert due == datetime(2026, 10, 7, 7, 0, tzinfo=UTC)


def test_now_in_any_timezone_is_accepted():
    now_utc = NOW.astimezone(UTC)
    assert resolve(now=now_utc, time="lunch") == resolve(now=NOW, time="lunch")


def test_empty_when_has_no_time():
    assert resolve(text="как-нибудь") == (None, "none", [], False)


# --- field priority -----------------------------------------------------------------------------------------


def test_at_wins_over_everything():
    due, precision, _, confirm = resolve(at="2026-10-20T10:00", in_min=30, day="+1", time="09:00")
    assert (due, precision, confirm) == (L(2026, 10, 20, 10, 0), "fallback", True)


def test_in_min_wins_over_day_and_time():
    due, precision, _, _ = resolve(in_min=30, day="+1", time="09:00")
    assert (due, precision) == (NOW + timedelta(minutes=30), "relative")


# --- in_min -------------------------------------------------------------------------------------------------


def test_in_min_relative():
    assert resolve(in_min=120) == (L(2026, 10, 6, 16, 5), "relative", [], False)


def test_in_min_is_elapsed_time_across_dst():
    now = L(2026, 10, 25, 1, 30)  # CEST; at 03:00 CEST clocks go back to 02:00 CET
    due, precision, _, _ = resolve(now=now, in_min=120)
    assert precision == "relative"
    assert due - now == timedelta(hours=2)
    local = due.astimezone(TZ)
    assert (local.hour, local.minute, local.utcoffset()) == (2, 30, timedelta(hours=1))


# --- time only ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        ("morning", L(2026, 10, 7, 9, 0)),  # 09:00 passed -> tomorrow
        ("lunch", L(2026, 10, 7, 13, 0)),  # "в обед" after lunch -> tomorrow
        ("afternoon", L(2026, 10, 6, 15, 0)),
        ("after_work", L(2026, 10, 6, 18, 30)),
        ("evening", L(2026, 10, 6, 19, 0)),
        ("night", L(2026, 10, 6, 22, 0)),
    ],
)
def test_slot_only(slot: str, expected: datetime):
    assert resolve(time=slot) == (expected, "slot", [], False)


def test_lunch_before_lunch_is_today():
    due, _, _, _ = resolve(now=L(2026, 10, 6, 12, 0), time="lunch")
    assert due == L(2026, 10, 6, 13, 0)


def test_custom_slot_times_from_prefs():
    prefs = Prefs(slot_lunch=time(14, 30))
    due, _, _, _ = resolve(prefs=prefs, time="lunch")
    assert due == L(2026, 10, 6, 14, 30)


@pytest.mark.parametrize(
    ("hhmm", "expected"),
    [
        ("14:06", L(2026, 10, 6, 14, 6)),
        ("14:05", L(2026, 10, 7, 14, 5)),  # equal to now counts as passed
        ("09:00", L(2026, 10, 7, 9, 0)),
        ("23:59", L(2026, 10, 6, 23, 59)),
    ],
)
def test_exact_time_only(hhmm: str, expected: datetime):
    assert resolve(time=hhmm, text="в " + hhmm) == (expected, "exact", [], False)


# --- day without / with time --------------------------------------------------------------------------------


def test_day_without_time_uses_bare_date_time():
    assert resolve(day="+1") == (L(2026, 10, 7, 10, 0), "date", [], False)


def test_bare_date_time_from_prefs():
    due, _, _, _ = resolve(prefs=Prefs(bare_date_time=time(8, 15)), day="+1")
    assert due == L(2026, 10, 7, 8, 15)


def test_weekend_without_time_uses_weekend_time():
    assert resolve(day="weekend") == (L(2026, 10, 10, 11, 0), "date", [], False)


def test_day_with_slot():
    assert resolve(day="+1", time="evening") == (L(2026, 10, 7, 19, 0), "slot", [], False)


def test_day_with_exact_time():
    assert resolve(day="FR", time="15:00") == (L(2026, 10, 9, 15, 0), "exact", [], False)


# --- +N / +Nd and night_cutoff ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("+1", date(2026, 10, 7)),
        ("+2", date(2026, 10, 8)),
        ("+3d", date(2026, 10, 9)),
        ("+30", date(2026, 11, 5)),
        ("+2w", date(2026, 10, 20)),
        ("+1mo", date(2026, 11, 6)),
        ("+3mo", date(2027, 1, 6)),
        ("+1y", date(2027, 10, 6)),
    ],
)
def test_offsets(day: str, expected: date):
    due, precision, warnings, confirm = resolve(day=day)
    assert due == localize(expected, time(10, 0), TZ)
    assert (precision, warnings, confirm) == ("date", [], False)


def test_month_offset_clamps_to_month_end():
    due, _, _, _ = resolve(now=L(2027, 1, 31, 12, 0), day="+1mo")
    assert due == L(2027, 2, 28, 10, 0)


def test_year_offset_from_leap_day():
    due, _, _, _ = resolve(now=L(2028, 2, 29, 12, 0), day="+1y")
    assert due == L(2029, 2, 28, 10, 0)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("+1", date(2026, 10, 7)),  # "завтра" at 01:30 = the day that has just started
        ("+1d", date(2026, 10, 7)),
        ("+2", date(2026, 10, 8)),
        ("+1w", date(2026, 10, 14)),  # weeks are not adjusted
    ],
)
def test_night_cutoff(day: str, expected: date):
    now = L(2026, 10, 7, 1, 30)
    due, _, warnings, confirm = resolve(now=now, day=day)
    assert due == localize(expected, time(10, 0), TZ)
    assert (warnings, confirm) == ([], False)


def test_night_cutoff_boundary_is_exclusive():
    due, _, _, _ = resolve(now=L(2026, 10, 7, 4, 0), day="+1")
    assert due == L(2026, 10, 8, 10, 0)


def test_night_cutoff_from_prefs():
    prefs = Prefs(night_cutoff=time(0, 0))  # disabled
    due, _, _, _ = resolve(now=L(2026, 10, 7, 1, 30), prefs=prefs, day="+1")
    assert due == L(2026, 10, 8, 10, 0)


def test_night_cutoff_does_not_touch_today():
    due, _, warnings, _ = resolve(now=L(2026, 10, 7, 1, 30), day="+0", time="23:00")
    assert (due, warnings) == (L(2026, 10, 7, 23, 0), [])


# --- +0 / today with a passed time -------------------------------------------------------------------------


def test_today_with_future_time():
    assert resolve(day="+0", time="15:00") == (L(2026, 10, 6, 15, 0), "exact", [], False)


@pytest.mark.parametrize("day", ["+0", "+0d", "+0w"])
def test_today_with_passed_time_moves_to_tomorrow(day: str):
    due, precision, warnings, confirm = resolve(day=day, time="10:00")
    assert (due, precision) == (L(2026, 10, 7, 10, 0), "exact")
    assert warnings == [WARN_MOVED_TOMORROW]
    assert confirm is True


def test_today_without_time_after_bare_time_moves_to_tomorrow():
    due, precision, warnings, confirm = resolve(day="+0")
    assert (due, precision, warnings, confirm) == (L(2026, 10, 7, 10, 0), "date", [WARN_MOVED_TOMORROW], True)


def test_today_slot_passed_moves_with_warning():
    due, _, warnings, _ = resolve(day="+0", time="lunch")
    assert (due, warnings) == (L(2026, 10, 7, 13, 0), [WARN_MOVED_TOMORROW])


# --- weekdays -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("WE", date(2026, 10, 7)),
        ("TH", date(2026, 10, 8)),
        ("FR", date(2026, 10, 9)),
        ("SA", date(2026, 10, 10)),
        ("SU", date(2026, 10, 11)),
        ("MO", date(2026, 10, 12)),
        ("TU", date(2026, 10, 13)),  # today, but 10:00 has passed -> in a week
    ],
)
def test_nearest_weekday(day: str, expected: date):
    due, precision, _, _ = resolve(day=day)
    assert (due, precision) == (localize(expected, time(10, 0), TZ), "date")


def test_weekday_today_before_time_is_today():
    due, _, warnings, confirm = resolve(day="TU", time="15:00")
    assert (due, warnings, confirm) == (L(2026, 10, 6, 15, 0), [], False)


def test_weekday_today_after_time_is_next_week_without_warning():
    due, _, warnings, confirm = resolve(day="TU", time="14:00")
    assert (due, warnings, confirm) == (L(2026, 10, 13, 14, 0), [], False)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("next MO", date(2026, 10, 12)),
        ("next TU", date(2026, 10, 13)),
        ("next WE", date(2026, 10, 14)),
        ("next FR", date(2026, 10, 16)),  # not this Friday (9 Oct)
        ("next SU", date(2026, 10, 18)),
    ],
)
def test_next_weekday_is_in_next_calendar_week(day: str, expected: date):
    due, _, _, _ = resolve(day=day)
    assert due == localize(expected, time(10, 0), TZ)


def test_next_weekday_from_sunday():
    sunday = L(2026, 10, 11, 12, 0)
    assert resolve(now=sunday, day="next MO")[0] == L(2026, 10, 12, 10, 0)
    assert resolve(now=sunday, day="next SU")[0] == L(2026, 10, 18, 10, 0)


def test_next_weekday_from_monday_morning():
    monday = L(2026, 10, 12, 8, 0)
    assert resolve(now=monday, day="next MO")[0] == L(2026, 10, 19, 10, 0)


# --- dN / d-1 -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "day", "time_", "expected"),
    [
        (NOW, "d20", None, L(2026, 10, 20, 10, 0)),
        (NOW, "d5", None, L(2026, 11, 5, 10, 0)),  # passed -> next month
        (NOW, "d6", None, L(2026, 11, 6, 10, 0)),  # today, 10:00 passed -> next month
        (NOW, "d6", "15:00", L(2026, 10, 6, 15, 0)),  # today, still ahead
        (NOW, "d31", None, L(2026, 10, 31, 10, 0)),
        (L(2026, 11, 3, 12, 0), "d31", None, L(2026, 11, 30, 10, 0)),  # 30-day month -> clamp
        (L(2026, 11, 30, 12, 0), "d31", None, L(2026, 12, 31, 10, 0)),  # clamped day passed -> next month
        (L(2027, 2, 2, 12, 0), "d30", None, L(2027, 2, 28, 10, 0)),
        (L(2027, 1, 31, 12, 0), "d31", None, L(2027, 2, 28, 10, 0)),
        (NOW, "d-1", None, L(2026, 10, 31, 10, 0)),
        (L(2026, 10, 31, 12, 0), "d-1", None, L(2026, 11, 30, 10, 0)),
        (L(2028, 2, 10, 12, 0), "d-1", None, L(2028, 2, 29, 10, 0)),
        (L(2026, 12, 15, 12, 0), "d1", None, L(2027, 1, 1, 10, 0)),
    ],
)
def test_month_day(now: datetime, day: str, time_: str | None, expected: datetime):
    due, precision, warnings, confirm = resolve(now=now, day=day, time=time_)
    assert due == expected
    assert (warnings, confirm) == ([], False)
    assert precision == ("exact" if time_ else "date")


# --- MM-DD and YYYY-MM-DD ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "time_", "expected"),
    [
        ("10-20", None, L(2026, 10, 20, 10, 0)),
        ("01-15", None, L(2027, 1, 15, 10, 0)),
        ("10-01", None, L(2027, 10, 1, 10, 0)),  # passed -> next year
        ("10-06", None, L(2027, 10, 6, 10, 0)),  # today, 10:00 passed
        ("10-06", "15:00", L(2026, 10, 6, 15, 0)),  # today, still ahead
        ("02-29", None, L(2028, 2, 29, 10, 0)),  # waits for a leap year
    ],
)
def test_month_and_day(day: str, time_: str | None, expected: datetime):
    due, _, warnings, confirm = resolve(day=day, time=time_)
    assert (due, warnings, confirm) == (expected, [], False)


def test_full_date_in_future():
    assert resolve(day="2026-12-01", time="18:00") == (L(2026, 12, 1, 18, 0), "exact", [], False)


def test_full_date_in_past_keeps_date_and_asks():
    due, precision, warnings, confirm = resolve(day="2026-10-01")
    assert (due, precision) == (L(2026, 10, 1, 10, 0), "date")
    assert warnings == [WARN_PAST_DATE]
    assert confirm is True


def test_full_date_today_with_passed_time_is_past():
    due, _, warnings, confirm = resolve(day="2026-10-06", time="09:00")
    assert (due, warnings, confirm) == (L(2026, 10, 6, 9, 0), [WARN_PAST_DATE], True)


@pytest.mark.parametrize("day", ["02-30", "13-01", "2026-02-30", "2026-00-10"])
def test_impossible_dates(day: str):
    assert resolve(day=day) == (None, "none", [WARN_BAD_TIME], True)


# --- weekend ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "time_", "expected"),
    [
        (NOW, None, L(2026, 10, 10, 11, 0)),
        (L(2026, 10, 10, 9, 0), None, L(2026, 10, 10, 11, 0)),  # Saturday morning -> today
        (L(2026, 10, 10, 12, 0), None, L(2026, 10, 17, 11, 0)),  # Saturday after 11:00 -> next Saturday
        (L(2026, 10, 10, 18, 0), "evening", L(2026, 10, 10, 19, 0)),  # Saturday evening still ahead
        (L(2026, 10, 10, 20, 0), "evening", L(2026, 10, 17, 19, 0)),
        (L(2026, 10, 11, 9, 0), None, L(2026, 10, 17, 11, 0)),  # Sunday -> next Saturday
    ],
)
def test_weekend(now: datetime, time_: str | None, expected: datetime):
    due, precision, warnings, confirm = resolve(now=now, day="weekend", time=time_)
    assert (due, warnings, confirm) == (expected, [], False)
    assert precision == ("slot" if time_ else "date")


def test_weekend_time_from_prefs():
    due, _, _, _ = resolve(prefs=Prefs(weekend_time=time(12, 0)), day="weekend")
    assert due == L(2026, 10, 10, 12, 0)


# --- at (fallback) ------------------------------------------------------------------------------------------


def test_at_with_time():
    assert resolve(at="2026-11-27T15:00") == (L(2026, 11, 27, 15, 0), "fallback", [], True)


def test_at_date_only_uses_bare_date_time():
    assert resolve(at="2026-11-27") == (L(2026, 11, 27, 10, 0), "fallback", [], True)


def test_at_is_local_wall_time_across_dst():
    due, _, _, _ = resolve(at="2026-11-02T09:00")
    assert due == datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


def test_at_in_past_warns():
    assert resolve(at="2026-10-01T15:00") == (L(2026, 10, 1, 15, 0), "fallback", [WARN_PAST_DATE], True)


@pytest.mark.parametrize("at", ["2026-02-30", "2026-10-06T25:00", "2026-10-06T10:61"])
def test_at_invalid(at: str):
    assert resolve(at=at) == (None, "none", [WARN_BAD_TIME], True)


# --- early hours (00-06) without "утра/ночи/am" ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("hhmm", "text", "confirm"),
    [
        ("05:00", "в 5", True),
        ("00:30", "в 00:30", True),
        ("06:59", "в 6:59", True),
        ("05:00", "в 5 утра", False),
        ("06:00", "завтра утром в 6", False),
        ("02:00", "в 2 ночи", False),
        ("00:00", "в полночь", False),
        ("05:00", "at 5 am", False),
        ("05:00", "at 5am", False),
        ("05:00", "at 5 a.m.", False),
        ("05:00", "at 5 AM", False),
        ("07:00", "в 7", False),
        ("15:00", "в 15", False),
    ],
)
def test_early_hour_needs_confirm(hhmm: str, text: str, confirm: bool):
    _, _, _, needs_confirm = resolve(day="+1", time=hhmm, text=text)
    assert needs_confirm is confirm


def test_early_hour_check_ignores_slots():
    _, precision, _, confirm = resolve(day="+1", time="night", text="ночью")
    assert (precision, confirm) == ("slot", False)
    prefs = Prefs(slot_morning=time(6, 0))
    _, _, _, confirm = resolve(prefs=prefs, day="+1", time="morning", text="с утра")
    assert confirm is False


# --- DST ----------------------------------------------------------------------------------------------------


def test_tomorrow_across_autumn_switch_keeps_wall_time():
    now = L(2026, 10, 24, 23, 30)
    due, _, _, _ = resolve(now=now, day="+1", time="09:00")
    assert due == datetime(2026, 10, 25, 8, 0, tzinfo=UTC)  # 09:00 CET


def test_ambiguous_time_on_autumn_switch_is_first():
    now = L(2026, 10, 24, 23, 30)
    due, _, _, _ = resolve(now=now, day="+1", time="02:30", text="в 2:30 ночи")
    assert due == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_nonexistent_time_on_spring_switch_moves_forward():
    now = L(2027, 3, 27, 12, 0)
    due, _, _, _ = resolve(now=now, day="+1", time="02:30", text="в 2:30 ночи")
    assert due == datetime(2027, 3, 28, 1, 30, tzinfo=UTC)
    assert due.astimezone(TZ).hour == 3


def test_time_only_at_night_of_switch():
    now = L(2027, 3, 28, 1, 0)  # just before the gap
    due, _, _, _ = resolve(now=now, time="09:00")
    assert due == datetime(2027, 3, 28, 7, 0, tzinfo=UTC)  # 09:00 CEST
