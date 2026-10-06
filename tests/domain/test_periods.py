"""Fuzzy time (docs/features/iteration-2.md §A): period bounds, period / soon / someday resolution, the reminders
(1) and (2) of a span, deadline double reminders, quiet hours and DST weeks."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from dumpbot.ai.schemas import ExtractedItem, When
from dumpbot.domain.prefs import Prefs
from dumpbot.domain.timeresolve import (
    WARN_BAD_TIME,
    lead_applies,
    period_bounds,
    plan_reminders,
    resolve_item,
    resolve_when,
    when_period,
)

TZ = ZoneInfo("Europe/Warsaw")
PREFS = Prefs(tz="Europe/Warsaw")


def L(*args: int) -> datetime:
    """Warsaw wall time (aware)."""
    return datetime(*args, tzinfo=TZ)


def D(y: int, m: int, d: int) -> date:
    return date(y, m, d)


def item(when: dict | None, kind: str = "task", **extra) -> ExtractedItem:
    return ExtractedItem.model_validate({"kind": kind, "title": "x", "when": when, **extra})


def fires(res_or_plans) -> list[tuple[datetime, bool]]:
    plans = getattr(res_or_plans, "reminders", res_or_plans)
    return [(p.fire_at, p.auto) for p in plans]


def resolve(when: dict, now: datetime, prefs: Prefs = PREFS, **extra):
    return resolve_item(item(when, **extra), now, prefs, source="text")


TUE = L(2026, 10, 6, 13, 36)  # the owner's live example: Tue 2026-10-06 13:36


# --- period_bounds: every form -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period", "now", "bounds"),
    [
        ("this_week", TUE, (D(2026, 10, 5), D(2026, 10, 11))),
        ("this_week", L(2026, 10, 5, 0, 1), (D(2026, 10, 5), D(2026, 10, 11))),  # Monday
        ("this_week", L(2026, 10, 11, 23, 59), (D(2026, 10, 5), D(2026, 10, 11))),  # Sunday
        ("next_week", TUE, (D(2026, 10, 12), D(2026, 10, 18))),
        ("next_week", L(2026, 10, 11, 22, 0), (D(2026, 10, 12), D(2026, 10, 18))),
        ("this_week", L(2026, 12, 30, 12, 0), (D(2026, 12, 28), D(2027, 1, 3))),  # across the new year
        ("next_week", L(2026, 12, 30, 12, 0), (D(2027, 1, 4), D(2027, 1, 10))),
        ("this_month", TUE, (D(2026, 10, 1), D(2026, 10, 31))),
        ("next_month", TUE, (D(2026, 11, 1), D(2026, 11, 30))),
        ("this_month", L(2027, 2, 10, 12, 0), (D(2027, 2, 1), D(2027, 2, 28))),
        ("this_month", L(2028, 2, 10, 12, 0), (D(2028, 2, 1), D(2028, 2, 29))),  # leap year
        ("next_month", L(2026, 12, 15, 12, 0), (D(2027, 1, 1), D(2027, 1, 31))),  # December -> January
        ("next_month", L(2027, 1, 31, 12, 0), (D(2027, 2, 1), D(2027, 2, 28))),  # no "Feb 31" clamping issue
    ],
)
def test_week_and_month_bounds(period: str, now: datetime, bounds: tuple[date, date]):
    assert period_bounds(period, now, PREFS) == bounds


@pytest.mark.parametrize(
    ("period", "bounds"),
    [
        ("2026-10", (D(2026, 10, 1), D(2026, 10, 31))),  # the current month
        ("2026-12", (D(2026, 12, 1), D(2026, 12, 31))),  # later this year
        ("2026-09", (D(2027, 9, 1), D(2027, 9, 30))),  # "в сентябре" in October: next year
        ("2026-01", (D(2027, 1, 1), D(2027, 1, 31))),
        ("2025-11", (D(2026, 11, 1), D(2026, 11, 30))),  # a stale year from the model: the month counts
        ("2025-03", (D(2027, 3, 1), D(2027, 3, 31))),
        ("2028-03", (D(2028, 3, 1), D(2028, 3, 31))),  # an explicit future year is kept
        ("2027-02", (D(2027, 2, 1), D(2027, 2, 28))),
    ],
)
def test_named_month_bounds(period: str, bounds: tuple[date, date]):
    assert period_bounds(period, TUE, PREFS) == bounds


def test_named_month_on_its_last_day_is_still_this_one():
    assert period_bounds("2026-10", L(2026, 10, 31, 23, 0), PREFS) == (D(2026, 10, 1), D(2026, 10, 31))
    assert period_bounds("2026-10", L(2026, 11, 1, 0, 30), PREFS) == (D(2027, 10, 1), D(2027, 10, 31))


@pytest.mark.parametrize(
    ("period", "now", "bounds"),
    [
        ("spring", TUE, (D(2027, 3, 1), D(2027, 5, 31))),
        ("summer", TUE, (D(2027, 6, 1), D(2027, 8, 31))),
        ("autumn", TUE, (D(2026, 9, 1), D(2026, 11, 30))),  # the current season
        ("winter", TUE, (D(2026, 12, 1), D(2027, 2, 28))),  # across the year boundary
        ("winter", L(2027, 1, 15, 12, 0), (D(2026, 12, 1), D(2027, 2, 28))),  # running winter, started last year
        ("winter", L(2027, 2, 28, 23, 0), (D(2026, 12, 1), D(2027, 2, 28))),  # its last day
        ("winter", L(2027, 3, 1, 0, 30), (D(2027, 12, 1), D(2028, 2, 29))),  # over -> next one, leap February
        ("winter", L(2026, 12, 1, 9, 0), (D(2026, 12, 1), D(2027, 2, 28))),
        ("summer", L(2027, 8, 31, 22, 0), (D(2027, 6, 1), D(2027, 8, 31))),
        ("summer", L(2027, 9, 1, 8, 0), (D(2028, 6, 1), D(2028, 8, 31))),
        ("spring", L(2027, 4, 1, 12, 0), (D(2027, 3, 1), D(2027, 5, 31))),
        ("autumn", L(2026, 12, 1, 0, 30), (D(2027, 9, 1), D(2027, 11, 30))),
    ],
)
def test_season_bounds(period: str, now: datetime, bounds: tuple[date, date]):
    assert period_bounds(period, now, PREFS) == bounds


def test_soon_bounds_follow_soon_days_and_night_cutoff():
    assert period_bounds("soon", TUE, PREFS) == (D(2026, 10, 6), D(2026, 10, 8))
    assert period_bounds("soon", TUE, Prefs(soon_days=5)) == (D(2026, 10, 6), D(2026, 10, 11))
    assert period_bounds("soon", TUE, Prefs(soon_days=1)) == (D(2026, 10, 6), D(2026, 10, 7))
    # 01:30 Wednesday: the owner's "today" is still Tuesday, so "на днях" is Thursday (like "+2")
    assert period_bounds("soon", L(2026, 10, 7, 1, 30), PREFS) == (D(2026, 10, 7), D(2026, 10, 8))


def test_local_calendar_decides_the_week_not_utc():
    # Sunday 22:30 UTC is already Monday 00:30 in Warsaw: a new week
    now = datetime(2026, 10, 11, 22, 30, tzinfo=UTC)
    assert period_bounds("this_week", now, PREFS) == (D(2026, 10, 12), D(2026, 10, 18))
    # 31 Oct 23:30 UTC is 1 Nov 00:30 CET
    assert period_bounds("this_month", datetime(2026, 10, 31, 23, 30, tzinfo=UTC), PREFS) == (
        D(2026, 11, 1),
        D(2026, 11, 30),
    )


@pytest.mark.parametrize("period", ["someday", "2026-13", "2026-00", "fortnight", ""])
def test_period_bounds_rejects_forms_without_dates(period: str):
    with pytest.raises(ValueError):
        period_bounds(period, TUE, PREFS)


# --- the live example and week periods ---------------------------------------------------------------------------


def test_this_week_on_tuesday_reminds_on_friday_evening():
    """ "На этой неделе посмотреть расбери пай", Tue 13:36 -> due and the only reminder Fri 19:00."""
    res = resolve({"text": "на этой неделе", "period": "this_week"}, TUE)
    assert (res.precision, res.due_at) == ("period", L(2026, 10, 9, 19, 0))
    assert (res.period_kind, res.period_start, res.period_end) == ("week", D(2026, 10, 5), D(2026, 10, 11))
    assert fires(res) == [(L(2026, 10, 9, 19, 0), True)]
    assert res.reminders[0].lead_minutes == 0
    assert not res.needs_confirm and res.warnings == ()


@pytest.mark.parametrize(
    ("now", "due", "reminders"),
    [
        (L(2026, 10, 5, 8, 0), L(2026, 10, 9, 19, 0), [L(2026, 10, 9, 19, 0)]),  # Monday: started, no (1)
        (L(2026, 10, 9, 18, 59), L(2026, 10, 9, 19, 0), [L(2026, 10, 9, 19, 0)]),  # Friday before 19:00
        (L(2026, 10, 9, 19, 30), L(2026, 10, 11, 19, 0), [L(2026, 10, 11, 19, 0)]),  # Friday after 19:00 -> Sunday
        (L(2026, 10, 10, 12, 0), L(2026, 10, 11, 19, 0), [L(2026, 10, 11, 19, 0)]),  # Saturday -> Sunday
        (L(2026, 10, 11, 18, 0), L(2026, 10, 11, 19, 0), [L(2026, 10, 11, 19, 0)]),
        (L(2026, 10, 11, 20, 0), L(2026, 10, 11, 19, 0), []),  # Sunday evening: both moments passed, no (2)
    ],
)
def test_reminder_two_falls_back_to_the_last_day_then_disappears(now: datetime, due: datetime, reminders):
    res = resolve({"text": "на этой неделе", "period": "this_week"}, now)
    assert res.due_at == due
    assert [f for f, _ in fires(res)] == reminders
    assert (res.period_start, res.period_end) == (D(2026, 10, 5), D(2026, 10, 11))


def test_next_week_reminds_on_its_monday_morning_and_friday_evening():
    res = resolve({"text": "на следующей неделе", "period": "next_week"}, TUE)
    assert (res.precision, res.due_at, res.period_kind) == ("period", L(2026, 10, 16, 19, 0), "week")
    assert fires(res) == [(L(2026, 10, 12, 10, 0), True), (L(2026, 10, 16, 19, 0), True)]
    assert res.reminders[0].lead_minutes == (4 * 24 + 9) * 60  # informative: 4 days 9 hours before due


def test_next_week_said_late_on_sunday_still_gets_the_monday_morning_reminder():
    res = resolve({"text": "на следующей неделе", "period": "next_week"}, L(2026, 10, 11, 23, 30))
    assert fires(res) == [(L(2026, 10, 12, 10, 0), True), (L(2026, 10, 16, 19, 0), True)]


# --- months, named months, seasons ---------------------------------------------------------------------------------


def test_this_month_reminds_on_its_last_working_day():
    res = resolve({"text": "в этом месяце", "period": "this_month"}, TUE)
    assert (res.period_kind, res.period_start, res.period_end) == ("month", D(2026, 10, 1), D(2026, 10, 31))
    assert fires(res) == [(L(2026, 10, 30, 19, 0), True)]  # Fri 30 Oct (31 Oct is a Saturday)


def test_this_month_on_its_last_saturday_falls_back_to_the_last_day():
    res = resolve({"text": "в этом месяце", "period": "this_month"}, L(2026, 10, 31, 10, 0))
    assert fires(res) == [(L(2026, 10, 31, 19, 0), True)]


def test_next_month_reminds_on_the_first_morning_and_the_last_working_day():
    res = resolve({"text": "в следующем месяце", "period": "next_month"}, TUE)
    # 1 Nov is a Sunday: (1) is still that morning; 30 Nov is a Monday
    assert fires(res) == [(L(2026, 11, 1, 10, 0), True), (L(2026, 11, 30, 19, 0), True)]


def test_february_ending_on_sunday_reminds_on_friday():
    res = resolve({"text": "в феврале", "period": "2027-02"}, TUE)
    assert (res.period_start, res.period_end) == (D(2027, 2, 1), D(2027, 2, 28))
    assert fires(res) == [(L(2027, 2, 1, 10, 0), True), (L(2027, 2, 26, 19, 0), True)]


def test_current_named_month_behaves_like_this_month():
    res = resolve({"text": "в октябре", "period": "2026-10"}, TUE)
    assert (res.period_kind, res.period_start, res.period_end) == ("month", D(2026, 10, 1), D(2026, 10, 31))
    assert fires(res) == [(L(2026, 10, 30, 19, 0), True)]


def test_past_named_month_is_next_year():
    res = resolve({"text": "в сентябре", "period": "2026-09"}, TUE)
    assert (res.period_start, res.period_end) == (D(2027, 9, 1), D(2027, 9, 30))
    assert fires(res) == [(L(2027, 9, 1, 10, 0), True), (L(2027, 9, 30, 19, 0), True)]


def test_summer_said_in_october_is_next_summer():
    res = resolve({"text": "летом", "period": "summer"}, TUE)
    assert (res.period_kind, res.period_start, res.period_end) == ("season", D(2027, 6, 1), D(2027, 8, 31))
    assert fires(res) == [(L(2027, 6, 1, 10, 0), True), (L(2027, 8, 31, 19, 0), True)]


def test_winter_across_the_year_boundary():
    res = resolve({"text": "зимой", "period": "winter"}, TUE)
    assert (res.period_start, res.period_end) == (D(2026, 12, 1), D(2027, 2, 28))
    assert fires(res) == [(L(2026, 12, 1, 10, 0), True), (L(2027, 2, 26, 19, 0), True)]


def test_running_winter_said_in_january_has_no_first_day_reminder():
    res = resolve({"text": "зимой", "period": "winter"}, L(2027, 1, 15, 12, 0))
    assert (res.period_start, res.period_end) == (D(2026, 12, 1), D(2027, 2, 28))
    assert fires(res) == [(L(2027, 2, 26, 19, 0), True)]


def test_current_autumn():
    res = resolve({"text": "осенью", "period": "autumn"}, TUE)
    assert (res.period_start, res.period_end) == (D(2026, 9, 1), D(2026, 11, 30))
    assert fires(res) == [(L(2026, 11, 30, 19, 0), True)]


# --- soon / someday ----------------------------------------------------------------------------------------------


def test_soon_is_one_automatic_reminder_in_two_days_at_19():
    res = resolve({"text": "на днях", "period": "soon"}, TUE)
    assert (res.precision, res.due_at) == ("soon", L(2026, 10, 8, 19, 0))
    assert (res.period_kind, res.period_start, res.period_end) == ("soon", D(2026, 10, 6), D(2026, 10, 8))
    assert fires(res) == [(L(2026, 10, 8, 19, 0), True)]


def test_soon_uses_soon_days_and_a_named_time_of_day():
    assert resolve({"text": "на днях", "period": "soon"}, TUE, Prefs(soon_days=3)).due_at == L(2026, 10, 9, 19, 0)
    res = resolve({"text": "на днях утром", "period": "soon", "time": "morning"}, TUE)
    assert (res.precision, res.due_at) == ("soon", L(2026, 10, 8, 9, 0))


def test_soon_after_midnight_counts_from_the_owners_today():
    res = resolve({"text": "на днях", "period": "soon"}, L(2026, 10, 7, 1, 30))
    assert res.due_at == L(2026, 10, 8, 19, 0)


@pytest.mark.parametrize("kind", ["task", "event", "purchase", "idea"])
def test_someday_has_no_date_and_no_default_reminder(kind: str):
    res = resolve({"text": "когда-нибудь", "period": "someday"}, TUE, kind=kind)
    assert (res.due_at, res.precision, res.reminders) == (None, "someday", ())
    assert (res.period_kind, res.period_start, res.period_end) == ("someday", None, None)
    assert not res.needs_confirm


def test_someday_with_low_confidence_asks_to_confirm():
    res = resolve({"text": "когда-нибудь", "period": "someday"}, TUE, conf="low")
    assert res.needs_confirm and res.precision == "someday"


def test_someday_resolve_when():
    assert resolve_when(When(text="когда-нибудь", period="someday"), TUE, PREFS) == (None, "someday", [], False)


def test_someday_as_a_deadline_is_still_someday():
    res = resolve({"text": "когда-нибудь", "period": "someday", "role": "deadline"}, TUE)
    assert (res.precision, res.period_kind, res.due_at) == ("someday", "someday", None)


# --- field priority and bad values ---------------------------------------------------------------------------------


def test_period_wins_over_day_and_time():
    res = resolve({"text": "на этой неделе", "period": "this_week", "day": "+1", "time": "09:00"}, TUE)
    assert (res.precision, res.due_at) == ("period", L(2026, 10, 9, 19, 0))


def test_at_and_in_min_win_over_period():
    res = resolve({"text": "через час", "period": "this_week", "in_min": 60}, TUE)
    assert (res.precision, res.period_kind) == ("relative", None)
    res = resolve({"text": "9 окт 15:00", "period": "this_week", "at": "2026-10-09T15:00"}, TUE)
    assert (res.precision, res.period_kind, res.due_at) == ("exact", None, L(2026, 10, 9, 15, 0))


def test_impossible_month_is_a_bad_time():
    assert resolve_when(When(text="в 13-м месяце", period="2026-13"), TUE, PREFS) == (
        None,
        "none",
        [WARN_BAD_TIME],
        True,
    )
    res = resolve({"text": "в 13-м месяце", "period": "2026-13"}, TUE)
    assert res.precision == "default" and res.period_kind is None  # a task still gets its 19:00 reminder
    assert res.needs_confirm and WARN_BAD_TIME in res.warnings


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (When(period="this_week"), ("week", D(2026, 10, 5), D(2026, 10, 11))),
        (When(period="next_week"), ("week", D(2026, 10, 12), D(2026, 10, 18))),
        (When(period="this_month"), ("month", D(2026, 10, 1), D(2026, 10, 31))),
        (When(period="next_month"), ("month", D(2026, 11, 1), D(2026, 11, 30))),
        (When(period="2026-09"), ("month", D(2027, 9, 1), D(2027, 9, 30))),
        (When(period="winter"), ("season", D(2026, 12, 1), D(2027, 2, 28))),
        (When(period="soon"), ("soon", D(2026, 10, 6), D(2026, 10, 8))),
        (When(period="someday"), ("someday", None, None)),
        (When(period="this_week", role="deadline"), (None, None, None)),  # "до конца недели" is a date
        (When(period="soon", role="deadline"), ("soon", D(2026, 10, 6), D(2026, 10, 8))),
        (When(period="this_week", in_min=30), (None, None, None)),
        (When(period="this_week", at="2026-10-09"), (None, None, None)),
        (When(period="2026-13"), (None, None, None)),
        (When(day="+1"), (None, None, None)),
    ],
)
def test_when_period(when: When, expected):
    assert when_period(when, TUE, PREFS) == expected


# --- deadlines: the evening before + the day itself ----------------------------------------------------------------


def test_deadline_date_reminds_the_evening_before_and_in_the_morning():
    """ "до пятницы сдать отчёт" on Tuesday: Thu 19:00 and Fri 10:00, both automatic."""
    res = resolve({"text": "до пятницы", "day": "FR", "role": "deadline"}, TUE)
    assert (res.precision, res.due_at, res.period_kind) == ("date", L(2026, 10, 9, 10, 0), None)
    assert fires(res) == [(L(2026, 10, 8, 19, 0), True), (L(2026, 10, 9, 10, 0), True)]
    assert [p.lead_minutes for p in res.reminders] == [15 * 60, 0]


def test_deadline_with_a_time_reminds_the_evening_before_and_lead_before_it():
    res = resolve({"text": "до пятницы 15:00", "day": "FR", "time": "15:00", "role": "deadline"}, TUE)
    assert res.precision == "exact"
    assert fires(res) == [(L(2026, 10, 8, 19, 0), True), (L(2026, 10, 9, 14, 55), False)]
    res = resolve({"text": "до пятницы 15:00", "day": "FR", "time": "15:00", "role": "deadline"}, TUE, lead=[60])
    assert fires(res) == [(L(2026, 10, 8, 19, 0), True), (L(2026, 10, 9, 14, 0), False)]


def test_deadline_with_a_slot():
    res = resolve({"text": "до пятницы вечером", "day": "FR", "time": "evening", "role": "deadline"}, TUE)
    assert fires(res) == [(L(2026, 10, 8, 19, 0), True), (L(2026, 10, 9, 18, 55), False)]


def test_deadline_whole_day_lead_adds_to_the_evening_before():
    res = resolve({"text": "до пятницы", "day": "FR", "role": "deadline"}, TUE, lead=[1440])
    assert fires(res) == [
        (L(2026, 10, 8, 10, 0), True),
        (L(2026, 10, 8, 19, 0), True),
        (L(2026, 10, 9, 10, 0), True),
    ]


def test_deadline_tomorrow_said_after_19_skips_the_evening_before():
    res = resolve({"text": "до завтра", "day": "+1", "role": "deadline"}, L(2026, 10, 6, 20, 0))
    assert fires(res) == [(L(2026, 10, 7, 10, 0), True)]


def test_deadline_tomorrow_said_before_19_keeps_tonights_reminder():
    res = resolve({"text": "до завтра", "day": "+1", "role": "deadline"}, L(2026, 10, 6, 18, 30))
    assert fires(res) == [(L(2026, 10, 6, 19, 0), True), (L(2026, 10, 7, 10, 0), True)]


def test_deadline_today_with_a_time_only_has_the_lead_reminder():
    res = resolve({"text": "до 18:00", "day": "+0", "time": "18:00", "role": "deadline"}, L(2026, 10, 6, 14, 5))
    assert fires(res) == [(L(2026, 10, 6, 17, 55), False)]


def test_deadline_lead_already_past_fires_now_but_the_evening_before_is_skipped():
    now = L(2026, 10, 6, 17, 58)
    res = resolve({"text": "до 18:00", "day": "+0", "time": "18:00", "role": "deadline"}, now)
    assert fires(res) == [(now.astimezone(UTC), False)]


def test_deadline_end_of_week_is_its_last_working_day():
    res = resolve({"text": "до конца недели", "period": "this_week", "role": "deadline"}, TUE)
    assert (res.precision, res.due_at, res.period_kind, res.period_start) == ("date", L(2026, 10, 9, 10, 0), None, None)
    assert fires(res) == [(L(2026, 10, 8, 19, 0), True), (L(2026, 10, 9, 10, 0), True)]


def test_deadline_end_of_week_on_friday_is_today():
    res = resolve({"text": "до конца недели", "period": "this_week", "role": "deadline"}, L(2026, 10, 9, 12, 0))
    assert res.due_at == L(2026, 10, 9, 19, 0)  # today, at a time still ahead (like "сегодня" without a time)
    assert fires(res) == [(L(2026, 10, 9, 19, 0), True)]


def test_deadline_end_of_week_on_saturday_is_sunday():
    res = resolve({"text": "до конца недели", "period": "this_week", "role": "deadline"}, L(2026, 10, 10, 12, 0))
    assert res.due_at == L(2026, 10, 11, 10, 0)
    assert fires(res) == [(L(2026, 10, 10, 19, 0), True), (L(2026, 10, 11, 10, 0), True)]


def test_deadline_end_of_month():
    res = resolve({"text": "до конца месяца", "period": "this_month", "role": "deadline"}, TUE)
    assert res.due_at == L(2026, 10, 30, 10, 0)
    assert fires(res) == [(L(2026, 10, 29, 19, 0), True), (L(2026, 10, 30, 10, 0), True)]


def test_deadline_end_of_next_week():
    res = resolve({"text": "до конца следующей недели", "period": "next_week", "role": "deadline"}, TUE)
    assert fires(res) == [(L(2026, 10, 15, 19, 0), True), (L(2026, 10, 16, 10, 0), True)]


def test_event_and_remind_roles_get_no_evening_before():
    for role in ("event", "remind"):
        res = resolve({"text": "в пятницу", "day": "FR", "role": role}, TUE)
        assert fires(res) == [(L(2026, 10, 9, 10, 0), True)]


def test_relative_deadline_has_no_evening_before():
    plans = plan_reminders(L(2026, 10, 9, 15, 0), "relative", "deadline", None, PREFS, TUE)
    assert fires(plans) == [(L(2026, 10, 9, 15, 0), False)]


# --- quiet hours: automatic reminders move, explicit ones stay -----------------------------------------------------


def test_period_reminders_are_shifted_out_of_quiet_hours():
    prefs = Prefs(bare_date_time=time(7, 0), task_default_time=time(23, 30))
    res = resolve({"text": "на следующей неделе", "period": "next_week"}, TUE, prefs)
    assert res.due_at == L(2026, 10, 16, 23, 30)
    # (1) Mon 07:00 -> 08:00 (end of quiet hours), (2) Fri 23:30 -> Sat 08:00
    assert fires(res) == [(L(2026, 10, 12, 8, 0), True), (L(2026, 10, 17, 8, 0), True)]


def test_deadline_evening_before_is_shifted_and_dropped_when_it_lands_after_the_deadline():
    prefs = Prefs(task_default_time=time(23, 30))
    res = resolve({"text": "до пятницы", "day": "FR", "role": "deadline"}, TUE, prefs)
    # Thu 23:30 -> Fri 08:00, still before the 10:00 deadline reminder
    assert fires(res) == [(L(2026, 10, 9, 8, 0), True), (L(2026, 10, 9, 10, 0), True)]
    res = resolve({"text": "до пятницы 7:30 утра", "day": "FR", "time": "07:30", "role": "deadline"}, TUE, prefs)
    # Fri 08:00 would be after the 07:30 deadline: only the explicit lead reminder (not shifted: the owner's time)
    assert fires(res) == [(L(2026, 10, 9, 7, 25), False)]


def test_explicit_deadline_time_inside_quiet_hours_is_not_moved():
    res = resolve({"text": "до пятницы 23:45", "day": "FR", "time": "23:45", "role": "deadline"}, TUE)
    assert fires(res) == [(L(2026, 10, 8, 19, 0), True), (L(2026, 10, 9, 23, 40), False)]


def test_no_shift_when_quiet_hours_are_off():
    prefs = Prefs(bare_date_time=time(7, 0), quiet_enabled=False)
    res = resolve({"text": "на следующей неделе", "period": "next_week"}, TUE, prefs)
    assert fires(res) == [(L(2026, 10, 12, 7, 0), True), (L(2026, 10, 16, 19, 0), True)]


# --- DST weeks -----------------------------------------------------------------------------------------------------


def test_week_ending_on_the_autumn_switch():
    # Mon 19 - Sun 25 Oct 2026; clocks go back on Sunday 25th
    res = resolve({"text": "на этой неделе", "period": "this_week"}, L(2026, 10, 20, 9, 0))
    assert (res.period_start, res.period_end) == (D(2026, 10, 19), D(2026, 10, 25))
    assert res.due_at == datetime(2026, 10, 23, 17, 0, tzinfo=UTC)  # Fri 19:00 CEST
    res = resolve({"text": "на этой неделе", "period": "this_week"}, L(2026, 10, 24, 9, 0))
    assert res.due_at == datetime(2026, 10, 25, 18, 0, tzinfo=UTC)  # Sun 19:00 CET, after the switch


def test_week_after_the_autumn_switch_said_before_it():
    res = resolve({"text": "на следующей неделе", "period": "next_week"}, L(2026, 10, 20, 9, 0))
    assert (res.period_start, res.period_end) == (D(2026, 10, 26), D(2026, 11, 1))
    assert [f for f, _ in fires(res)] == [
        datetime(2026, 10, 26, 9, 0, tzinfo=UTC),  # Mon 10:00 CET
        datetime(2026, 10, 30, 18, 0, tzinfo=UTC),  # Fri 19:00 CET
    ]


def test_week_of_the_spring_switch():
    # Mon 22 - Sun 28 Mar 2027; clocks go forward on Sunday 28th
    res = resolve({"text": "на следующей неделе", "period": "next_week"}, L(2027, 3, 17, 12, 0))
    assert [f for f, _ in fires(res)] == [
        datetime(2027, 3, 22, 9, 0, tzinfo=UTC),  # Mon 10:00 CET
        datetime(2027, 3, 26, 18, 0, tzinfo=UTC),  # Fri 19:00 CET
    ]
    res = resolve({"text": "на этой неделе", "period": "this_week"}, L(2027, 3, 27, 20, 0))
    assert res.due_at == datetime(2027, 3, 28, 17, 0, tzinfo=UTC)  # Sun 19:00 CEST


def test_deadline_evening_before_across_the_autumn_switch():
    # deadline Mon 26 Oct (CET), the evening before is Sun 25 Oct 19:00 CET
    res = resolve({"text": "до понедельника", "day": "2026-10-26", "role": "deadline"}, L(2026, 10, 20, 9, 0))
    assert [f for f, _ in fires(res)] == [
        datetime(2026, 10, 25, 18, 0, tzinfo=UTC),
        datetime(2026, 10, 26, 9, 0, tzinfo=UTC),
    ]


# --- plan_reminders directly / lead_applies ------------------------------------------------------------------------


def test_period_plan_without_span_is_reminder_two_only():
    due = L(2026, 10, 16, 19, 0)
    assert fires(plan_reminders(due, "period", "event", None, PREFS, TUE)) == [(due, True)]
    span = (D(2026, 10, 12), D(2026, 10, 18))
    assert fires(plan_reminders(due, "period", "event", None, PREFS, TUE, period=span)) == [
        (L(2026, 10, 12, 10, 0), True),
        (due, True),
    ]


def test_period_plan_ignores_leads():
    due = L(2026, 10, 9, 19, 0)
    span = (D(2026, 10, 5), D(2026, 10, 11))
    assert fires(plan_reminders(due, "period", "event", [30, 1440], PREFS, TUE, period=span)) == [(due, True)]


def test_period_starting_today_has_no_first_day_reminder():
    now = L(2026, 10, 12, 8, 0)
    span = (D(2026, 10, 12), D(2026, 10, 18))
    due = L(2026, 10, 16, 19, 0)
    assert fires(plan_reminders(due, "period", "event", None, PREFS, now, period=span)) == [(due, True)]


def test_someday_plan_is_empty():
    assert plan_reminders(L(2026, 10, 9, 19, 0), "someday", "event", None, PREFS, TUE) == ()


@pytest.mark.parametrize("precision", ["period", "soon"])
def test_lead_applies_only_at_the_moment_for_automatic_spans(precision: str):
    assert lead_applies(precision, 0) is True
    assert lead_applies(precision, 30) is False
    assert lead_applies(precision, 1440) is False
