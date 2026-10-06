"""Quiet hours: window across midnight, same-day window, disabled, start == end, shifting to quiet_end."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from dumpbot.domain.prefs import Prefs
from dumpbot.domain.quiet import in_quiet_hours, shift_out_of_quiet

TZ = ZoneInfo("Europe/Warsaw")


def L(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


@pytest.mark.parametrize(
    ("hh", "mm", "quiet"),
    [
        (22, 59, False),
        (23, 0, True),  # start is inclusive
        (23, 30, True),
        (0, 0, True),
        (3, 0, True),
        (7, 59, True),
        (8, 0, False),  # end is exclusive
        (12, 0, False),
    ],
)
def test_default_window_crosses_midnight(hh: int, mm: int, quiet: bool, prefs: Prefs):
    assert in_quiet_hours(L(2026, 10, 6, hh, mm), prefs) is quiet


@pytest.mark.parametrize(("hh", "mm", "quiet"), [(12, 59, False), (13, 0, True), (14, 59, True), (15, 0, False)])
def test_same_day_window(hh: int, mm: int, quiet: bool):
    prefs = Prefs(quiet_start=time(13, 0), quiet_end=time(15, 0))
    assert in_quiet_hours(L(2026, 10, 6, hh, mm), prefs) is quiet


@pytest.mark.parametrize("hh", [0, 3, 13, 23])
def test_disabled(hh: int):
    prefs = Prefs(quiet_enabled=False)
    assert in_quiet_hours(L(2026, 10, 6, hh, 0), prefs) is False


@pytest.mark.parametrize("hh", [0, 8, 13, 23])
def test_start_equals_end_means_no_quiet_hours(hh: int):
    prefs = Prefs(quiet_start=time(8, 0), quiet_end=time(8, 0))
    assert in_quiet_hours(L(2026, 10, 6, hh, 0), prefs) is False


def test_uses_local_time_of_prefs_tz(prefs: Prefs):
    assert in_quiet_hours(datetime(2026, 10, 6, 21, 30, tzinfo=UTC), prefs) is True  # 23:30 CEST
    assert in_quiet_hours(datetime(2026, 12, 6, 21, 30, tzinfo=UTC), prefs) is False  # 22:30 CET
    assert in_quiet_hours(datetime(2026, 10, 6, 21, 30, tzinfo=UTC), Prefs(tz="Europe/London")) is False


def test_naive_is_utc(prefs: Prefs):
    assert in_quiet_hours(datetime(2026, 10, 6, 21, 30), prefs) is True


# --- shift_out_of_quiet --------------------------------------------------------------------------------------


def test_evening_part_shifts_to_next_morning(prefs: Prefs):
    shifted = shift_out_of_quiet(L(2026, 10, 6, 23, 30), prefs)
    assert shifted == L(2026, 10, 7, 8, 0)
    assert shifted.tzinfo is UTC


def test_after_midnight_shifts_same_day(prefs: Prefs):
    assert shift_out_of_quiet(L(2026, 10, 7, 2, 0), prefs) == L(2026, 10, 7, 8, 0)


def test_quiet_start_shifts_to_end(prefs: Prefs):
    assert shift_out_of_quiet(L(2026, 10, 6, 23, 0), prefs) == L(2026, 10, 7, 8, 0)


def test_outside_is_unchanged_but_utc(prefs: Prefs):
    t = L(2026, 10, 6, 12, 0)
    shifted = shift_out_of_quiet(t, prefs)
    assert shifted == t
    assert shifted.tzinfo is UTC


def test_quiet_end_itself_is_unchanged(prefs: Prefs):
    assert shift_out_of_quiet(L(2026, 10, 7, 8, 0), prefs) == L(2026, 10, 7, 8, 0)


def test_same_day_window_shift():
    prefs = Prefs(quiet_start=time(13, 0), quiet_end=time(15, 0))
    assert shift_out_of_quiet(L(2026, 10, 6, 14, 0), prefs) == L(2026, 10, 6, 15, 0)


def test_disabled_never_shifts():
    prefs = Prefs(quiet_enabled=False)
    assert shift_out_of_quiet(L(2026, 10, 6, 23, 30), prefs) == L(2026, 10, 6, 23, 30)


def test_shift_on_autumn_dst_night(prefs: Prefs):
    shifted = shift_out_of_quiet(L(2026, 10, 25, 1, 0), prefs)
    assert shifted == datetime(2026, 10, 25, 7, 0, tzinfo=UTC)  # 08:00 CET


def test_shift_on_spring_dst_night(prefs: Prefs):
    shifted = shift_out_of_quiet(L(2027, 3, 27, 23, 30), prefs)
    assert shifted == datetime(2027, 3, 28, 6, 0, tzinfo=UTC)  # 08:00 CEST


def test_quiet_end_in_dst_gap_moves_forward():
    prefs = Prefs(quiet_start=time(23, 0), quiet_end=time(2, 30))
    shifted = shift_out_of_quiet(L(2027, 3, 28, 1, 0), prefs)
    assert shifted == datetime(2027, 3, 28, 1, 30, tzinfo=UTC)  # 03:30 CEST
