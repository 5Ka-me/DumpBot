"""plan_reminders: leads, auto/quiet hours, wall-clock days across DST, clamping, de-duplication."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from dumpbot.domain.prefs import Prefs
from dumpbot.domain.timeresolve import ReminderPlan, lead_applies, plan_reminders

TZ = ZoneInfo("Europe/Warsaw")


def L(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


def fires(plans: tuple[ReminderPlan, ...]) -> list[tuple[datetime, int, bool]]:
    return [(p.fire_at, p.lead_minutes, p.auto) for p in plans]


DUE = L(2026, 10, 9, 15, 0)


@pytest.mark.parametrize("precision", ["exact", "slot"])
def test_default_lead_for_exact_and_slot(precision: str, now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, precision, "event", None, prefs, now)) == [(L(2026, 10, 9, 14, 55), 5, False)]


def test_default_lead_from_prefs(now: datetime):
    prefs = Prefs(lead_minutes=15)
    assert fires(plan_reminders(DUE, "exact", "event", None, prefs, now)) == [(L(2026, 10, 9, 14, 45), 15, False)]


def test_empty_lead_list_means_default(now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, "exact", "event", [], prefs, now)) == [(L(2026, 10, 9, 14, 55), 5, False)]


def test_zero_lead(now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, "exact", "event", [0], prefs, now)) == [(DUE, 0, False)]


def test_lead_list_sorted_by_time(now: datetime, prefs: Prefs):
    plans = plan_reminders(DUE, "exact", "event", [30, 1440, 60], prefs, now)
    assert fires(plans) == [
        (L(2026, 10, 8, 15, 0), 1440, False),
        (L(2026, 10, 9, 14, 0), 60, False),
        (L(2026, 10, 9, 14, 30), 30, False),
    ]


@pytest.mark.parametrize("precision", ["exact", "slot"])
def test_remind_role_fires_at_due(precision: str, now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, precision, "remind", None, prefs, now)) == [(DUE, 0, False)]


def test_relative_fires_at_due(now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, "relative", "event", None, prefs, now)) == [(DUE, 0, False)]


@pytest.mark.parametrize(("precision", "role"), [("relative", "event"), ("exact", "remind"), ("slot", "remind")])
def test_explicit_lead_is_honoured_for_relative_and_remind(precision: str, role: str, now: datetime, prefs: Prefs):
    # "через 2 часа созвон, напомни за 30 минут": the stated lead wins over "at the moment itself".
    assert fires(plan_reminders(DUE, precision, role, [30], prefs, now)) == [(L(2026, 10, 9, 14, 30), 30, False)]


@pytest.mark.parametrize(
    ("precision", "minutes", "applies"),
    [
        ("exact", 30, True),
        ("slot", 30, True),
        ("relative", 30, True),
        ("date", 30, False),
        ("date", 1440, True),
        ("date", 0, True),
        ("fallback", 60, False),
        ("default", 30, False),
        ("default", 1440, False),
        ("default", 0, True),
    ],
)
def test_lead_applies_matches_the_plan(precision: str, minutes: int, applies: bool, now: datetime, prefs: Prefs):
    assert lead_applies(precision, minutes) is applies
    leads = [p.lead_minutes for p in plan_reminders(DUE, precision, "event", [minutes], prefs, now)]
    assert (minutes in leads) is applies


@pytest.mark.parametrize("precision", ["date", "default", "fallback"])
@pytest.mark.parametrize("role", ["event", "remind"])
def test_automatic_precisions_fire_at_due_auto(precision: str, role: str, now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, precision, role, None, prefs, now)) == [(DUE, 0, True)]


def test_default_deadline_has_no_evening_before(now: datetime, prefs: Prefs):
    # A deadline without any date (a task "до …" the model could not place) keeps the single default reminder.
    assert fires(plan_reminders(DUE, "default", "deadline", None, prefs, now)) == [(DUE, 0, True)]


@pytest.mark.parametrize("precision", ["date", "fallback"])
def test_automatic_precisions_ignore_minute_leads(precision: str, now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, precision, "event", [5], prefs, now)) == [(DUE, 0, True)]


@pytest.mark.parametrize("precision", ["date", "fallback"])
def test_automatic_precisions_keep_whole_day_leads(precision: str, now: datetime, prefs: Prefs):
    plans = plan_reminders(DUE, precision, "event", [1440, 60], prefs, now)
    assert fires(plans) == [(L(2026, 10, 8, 15, 0), 1440, True), (DUE, 0, True)]


def test_default_precision_ignores_leads(now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, "default", "event", [1440], prefs, now)) == [(DUE, 0, True)]


def test_none_precision(now: datetime, prefs: Prefs):
    assert plan_reminders(DUE, "none", "event", None, prefs, now) == ()


# --- DST: whole days in wall clock, minutes in UTC -------------------------------------------------------------


def test_day_lead_across_autumn_switch(now: datetime, prefs: Prefs):
    due = L(2026, 10, 25, 19, 0)  # CET, 18:00 UTC
    plans = plan_reminders(due, "exact", "event", [2880, 1440, 60], prefs, now)
    assert fires(plans) == [
        (L(2026, 10, 23, 19, 0), 2880, False),
        (L(2026, 10, 24, 19, 0), 1440, False),  # 19:00 CEST = 17:00 UTC: 25 hours before
        (L(2026, 10, 25, 18, 0), 60, False),
    ]
    assert due - plans[1].fire_at == timedelta(hours=25)
    assert plans[1].fire_at == datetime(2026, 10, 24, 17, 0, tzinfo=UTC)
    assert plans[2].fire_at == datetime(2026, 10, 25, 17, 0, tzinfo=UTC)


def test_day_lead_across_spring_switch(prefs: Prefs):
    now = L(2027, 3, 20, 12, 0)
    due = L(2027, 3, 28, 19, 0)  # CEST
    plans = plan_reminders(due, "exact", "event", [1440], prefs, now)
    assert fires(plans) == [(L(2027, 3, 27, 19, 0), 1440, False)]
    assert due - plans[0].fire_at == timedelta(hours=23)


def test_minute_lead_across_autumn_switch_is_utc(prefs: Prefs):
    now = L(2026, 10, 24, 12, 0)
    due = datetime(2026, 10, 25, 2, 0, tzinfo=UTC)  # 03:00 CET
    plans = plan_reminders(due, "exact", "event", [120], prefs, now)
    assert plans[0].fire_at == datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # 02:00 CEST (first 02:00)


def test_results_are_utc(now: datetime, prefs: Prefs):
    plans = plan_reminders(DUE, "exact", "event", [1440, 5], prefs, now)
    assert all(p.fire_at.tzinfo is UTC for p in plans)


# --- past / clamping ----------------------------------------------------------------------------------------


def test_fire_in_past_is_clamped_to_now(now: datetime, prefs: Prefs):
    due = now + timedelta(minutes=3)
    assert fires(plan_reminders(due, "exact", "event", None, prefs, now)) == [(now, 5, False)]


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(minutes=-1), timedelta(days=-3)])
def test_due_not_in_future_has_no_reminders(delta: timedelta, now: datetime, prefs: Prefs):
    assert plan_reminders(now + delta, "exact", "event", None, prefs, now) == ()
    assert plan_reminders(now + delta, "default", "event", None, prefs, now) == ()


def test_duplicates_collapse_keeping_smallest_lead(now: datetime, prefs: Prefs):
    due = now + timedelta(minutes=2)
    assert fires(plan_reminders(due, "exact", "event", [60, 10, 30], prefs, now)) == [(now, 10, False)]


def test_duplicate_leads_collapse(now: datetime, prefs: Prefs):
    assert len(plan_reminders(DUE, "exact", "event", [5, 5], prefs, now)) == 1


# --- quiet hours -------------------------------------------------------------------------------------------


def test_auto_in_quiet_hours_moves_to_quiet_end(now: datetime, prefs: Prefs):
    due = L(2026, 10, 6, 23, 30)
    assert fires(plan_reminders(due, "default", "event", None, prefs, now)) == [(L(2026, 10, 7, 8, 0), 0, True)]


def test_auto_early_morning_moves_same_day(now: datetime, prefs: Prefs):
    due = L(2026, 10, 7, 7, 0)
    assert fires(plan_reminders(due, "date", "event", None, prefs, now)) == [(L(2026, 10, 7, 8, 0), 0, True)]


def test_explicit_in_quiet_hours_is_kept(now: datetime, prefs: Prefs):
    due = L(2026, 10, 7, 0, 30)
    assert fires(plan_reminders(due, "exact", "event", None, prefs, now)) == [(L(2026, 10, 7, 0, 25), 5, False)]


def test_auto_clamped_to_now_in_quiet_hours_is_shifted(prefs: Prefs):
    now = L(2026, 10, 6, 23, 50)
    due = L(2026, 10, 7, 0, 30)
    plans = plan_reminders(due, "date", "event", [1440], prefs, now)
    assert fires(plans) == [(L(2026, 10, 7, 8, 0), 1440, True)]


def test_auto_quiet_shift_merges_duplicates(prefs: Prefs):
    now = L(2026, 10, 6, 12, 0)
    due = L(2026, 10, 8, 2, 0)
    plans = plan_reminders(due, "date", "event", [1440, 0], prefs, now)
    # 1440 -> 7 Oct 02:00 -> 7 Oct 08:00; 0 -> 8 Oct 02:00 -> 8 Oct 08:00
    assert fires(plans) == [(L(2026, 10, 7, 8, 0), 1440, True), (L(2026, 10, 8, 8, 0), 0, True)]


def test_quiet_disabled_keeps_auto_time(now: datetime):
    prefs = Prefs(quiet_enabled=False)
    due = L(2026, 10, 6, 23, 30)
    assert fires(plan_reminders(due, "default", "event", None, prefs, now)) == [(due, 0, True)]


def test_quiet_hours_custom_window(now: datetime):
    prefs = Prefs(quiet_start=time(22, 0), quiet_end=time(7, 30))
    due = L(2026, 10, 6, 22, 15)
    assert fires(plan_reminders(due, "date", "event", None, prefs, now)) == [(L(2026, 10, 7, 7, 30), 0, True)]
