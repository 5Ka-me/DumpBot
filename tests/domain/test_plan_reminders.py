"""plan_reminders / notify_due: an item's time is its notification time (2026-10-07) — no default lead, explicit
leads move the notification, several leads notify several times; auto/quiet hours, wall-clock days across DST,
de-duplication."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from dumpbot.domain.prefs import Prefs
from dumpbot.domain.timeresolve import (
    ReminderPlan,
    effective_leads,
    lead_shift,
    notify_due,
    plan_reminders,
    shift_back,
    shift_forward,
)

TZ = ZoneInfo("Europe/Warsaw")


def L(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


def fires(plans: tuple[ReminderPlan, ...]) -> list[tuple[datetime, int, bool]]:
    return [(p.fire_at, p.lead_minutes, p.auto) for p in plans]


DUE = L(2026, 10, 9, 15, 0)


# --- one notification, at the time itself -------------------------------------------------------------------------


@pytest.mark.parametrize("precision", ["exact", "slot", "relative"])
@pytest.mark.parametrize("role", ["event", "remind"])
def test_a_time_the_user_gave_notifies_exactly_then(precision: str, role: str, now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, precision, role, None, prefs, now)) == [(DUE, 0, False)]


def test_no_default_lead_even_with_old_stored_prefs(now: datetime):
    prefs = Prefs.from_stored({"lead_minutes": 15})  # the retired preference is ignored
    assert not hasattr(prefs, "lead_minutes")
    assert fires(plan_reminders(DUE, "exact", "event", None, prefs, now)) == [(DUE, 0, False)]


def test_empty_and_zero_leads_are_no_lead(now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, "exact", "event", [], prefs, now)) == [(DUE, 0, False)]
    assert fires(plan_reminders(DUE, "exact", "event", [0], prefs, now)) == [(DUE, 0, False)]


@pytest.mark.parametrize("precision", ["date", "default", "fallback", "soon"])
@pytest.mark.parametrize("role", ["event", "remind"])
def test_automatic_precisions_fire_at_due_auto(precision: str, role: str, now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, precision, role, None, prefs, now)) == [(DUE, 0, True)]


def test_default_deadline_has_no_evening_before(now: datetime, prefs: Prefs):
    # A deadline without any date (a task "до …" the model could not place) keeps the single default reminder.
    assert fires(plan_reminders(DUE, "default", "deadline", None, prefs, now)) == [(DUE, 0, True)]


def test_none_precision(now: datetime, prefs: Prefs):
    assert plan_reminders(DUE, "none", "event", None, prefs, now) == ()


# --- explicit leads: several notifications --------------------------------------------------------------------------


def test_one_explicit_lead_is_already_in_the_due(now: datetime, prefs: Prefs):
    # "напомни за 30 минут до встречи в 15": due 14:30 (notify_due), one notification there.
    due = L(2026, 10, 9, 14, 30)
    assert fires(plan_reminders(due, "exact", "event", [30], prefs, now)) == [(due, 0, False)]


def test_several_leads_notify_at_each_moment(now: datetime, prefs: Prefs):
    # "за день и за час до встречи в пт 15:00": due Fri 14:00 (the last), the day before (Thu 15:00) comes earlier.
    due = L(2026, 10, 9, 14, 0)
    assert fires(plan_reminders(due, "exact", "event", [1440, 60], prefs, now)) == [
        (L(2026, 10, 8, 15, 0), 23 * 60, False),
        (due, 0, False),
    ]


def test_three_leads(now: datetime, prefs: Prefs):
    due = L(2026, 10, 9, 14, 30)  # stated Fri 15:00 minus the smallest lead
    plans = plan_reminders(due, "exact", "event", [30, 1440, 60], prefs, now)
    assert [p.fire_at for p in plans] == [L(2026, 10, 8, 15, 0), L(2026, 10, 9, 14, 0), due]


def test_earlier_lead_moments_already_past_are_dropped(prefs: Prefs):
    # Thu 15:05: "за день" has notified, "за час" (the due) is still ahead; at 14:05 nothing is left.
    due = L(2026, 10, 9, 14, 0)
    assert fires(plan_reminders(due, "exact", "event", [1440, 60], prefs, L(2026, 10, 8, 15, 5))) == [(due, 0, False)]
    assert plan_reminders(due, "exact", "event", [1440, 60], prefs, L(2026, 10, 9, 14, 5)) == ()


def test_catch_up_notifies_a_series_instance_at_its_stated_time(prefs: Prefs):
    # "каждый день в 14:30, напомни за час" sent at 14:05: today's instance is due 13:30, its lead moment is past.
    due, now = L(2026, 10, 6, 13, 30), L(2026, 10, 6, 14, 5)
    assert plan_reminders(due, "exact", "event", [60], prefs, now) == ()
    assert fires(plan_reminders(due, "exact", "event", [60], prefs, now, catch_up=True)) == [
        (L(2026, 10, 6, 14, 30), 0, False)
    ]
    # a lead moment still ahead is planned as usual; past the stated time there is nothing to catch up
    assert fires(plan_reminders(due, "exact", "event", [60], prefs, L(2026, 10, 6, 13, 0), catch_up=True)) == [
        (due, 0, False)
    ]
    assert plan_reminders(due, "exact", "event", [60], prefs, L(2026, 10, 6, 14, 31), catch_up=True) == ()


@pytest.mark.parametrize("precision", ["date", "fallback"])
def test_automatic_dates_take_whole_day_leads_only(precision: str, now: datetime, prefs: Prefs):
    assert effective_leads(precision, [1440, 60, 30]) == [1440]
    due = L(2026, 10, 9, 10, 0)  # "за 2 дня и за день до 10 окт": stated Sat 10:00, due the day before
    plans = plan_reminders(due, precision, "event", [2880, 1440, 60], prefs, now)
    assert fires(plans) == [(L(2026, 10, 8, 10, 0), 1440, True), (due, 0, True)]


@pytest.mark.parametrize("precision", ["default", "period", "soon", "none", "someday"])
def test_a_time_the_bot_chose_takes_no_leads(precision: str):
    assert effective_leads(precision, [1440, 60]) == []


def test_effective_leads_are_unique_positive_and_largest_first():
    assert effective_leads("exact", [30, 0, 1440, 30, 60]) == [1440, 60, 30]
    assert effective_leads("relative", [30]) == [30]
    assert effective_leads("exact", None) == []


def test_lead_shift_is_the_smallest_lead_but_not_for_a_deadline():
    assert lead_shift("exact", "event", [60, 1440]) == 60
    assert lead_shift("exact", "event", [1440]) == 1440
    assert lead_shift("slot", "remind", [30]) == 30
    assert lead_shift("exact", "deadline", [60]) == 0
    assert lead_shift("default", "event", [60]) == 0
    assert lead_shift("date", "event", [60]) == 0


# --- notify_due: the item's time from the stated one --------------------------------------------------------------


def test_notify_due_without_leads_is_the_stated_time(now: datetime, prefs: Prefs):
    assert notify_due(DUE, "exact", "event", None, prefs, now) == (DUE.astimezone(UTC), None)


def test_notify_due_moves_the_time_by_the_lead(now: datetime, prefs: Prefs):
    assert notify_due(DUE, "exact", "event", [30], prefs, now) == (L(2026, 10, 9, 14, 30), (30,))


def test_notify_due_with_several_leads_is_the_last(now: datetime, prefs: Prefs):
    # "за день и за час": the item sits on the day it happens, an hour before; the day before notifies earlier.
    assert notify_due(DUE, "exact", "event", [60, 1440], prefs, now) == (L(2026, 10, 9, 14, 0), (1440, 60))


def test_notify_due_drops_lead_moments_already_past(prefs: Prefs):
    # Said on Thu at 16:00: "за день" (Thu 15:00) has passed, "за час" (Fri 14:00) is the due.
    now = L(2026, 10, 8, 16, 0)
    assert notify_due(DUE, "exact", "event", [1440, 60], prefs, now) == (L(2026, 10, 9, 14, 0), (60,))


def test_notify_due_with_every_lead_moment_past_is_the_stated_time(prefs: Prefs):
    now = L(2026, 10, 9, 14, 45)
    assert notify_due(DUE, "exact", "event", [30], prefs, now) == (DUE.astimezone(UTC), None)


def test_notify_due_of_a_deadline_keeps_its_time(now: datetime, prefs: Prefs):
    assert notify_due(DUE, "exact", "deadline", [60], prefs, now) == (DUE.astimezone(UTC), (60,))


def test_notify_due_ignores_leads_the_precision_does_not_take(now: datetime, prefs: Prefs):
    assert notify_due(DUE, "default", "event", [60], prefs, now) == (DUE.astimezone(UTC), None)
    assert notify_due(DUE, "date", "event", [60], prefs, now) == (DUE.astimezone(UTC), None)
    assert notify_due(DUE, "date", "event", [1440], prefs, now) == (L(2026, 10, 8, 15, 0), (1440,))


# --- deadlines -----------------------------------------------------------------------------------------------------


def test_deadline_with_a_time_notifies_at_it_and_the_evening_before(now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, "exact", "deadline", None, prefs, now)) == [
        (L(2026, 10, 8, 19, 0), 20 * 60, True),
        (DUE, 0, False),
    ]


def test_deadline_lead_replaces_its_own_moment(now: datetime, prefs: Prefs):
    assert fires(plan_reminders(DUE, "exact", "deadline", [60], prefs, now)) == [
        (L(2026, 10, 8, 19, 0), 20 * 60, True),
        (L(2026, 10, 9, 14, 0), 60, False),
    ]


def test_date_deadline_keeps_its_morning_with_a_whole_day_lead(now: datetime, prefs: Prefs):
    due = L(2026, 10, 9, 10, 0)
    assert [p.fire_at for p in plan_reminders(due, "date", "deadline", [1440], prefs, now)] == [
        L(2026, 10, 8, 10, 0),
        L(2026, 10, 8, 19, 0),
        due,
    ]


# --- DST: whole days in wall clock, minutes in UTC -------------------------------------------------------------


def test_day_lead_across_autumn_switch(now: datetime, prefs: Prefs):
    stated = L(2026, 10, 25, 19, 0)  # CET, 18:00 UTC
    due, leads = notify_due(stated, "exact", "event", [2880, 1440, 60], prefs, now)
    assert (due, leads) == (L(2026, 10, 25, 18, 0), (2880, 1440, 60))
    plans = plan_reminders(due, "exact", "event", list(leads), prefs, now)
    assert [p.fire_at for p in plans] == [L(2026, 10, 23, 19, 0), L(2026, 10, 24, 19, 0), L(2026, 10, 25, 18, 0)]
    assert stated - plans[1].fire_at == timedelta(hours=25)  # 19:00 CEST = 17:00 UTC
    assert plans[1].fire_at == datetime(2026, 10, 24, 17, 0, tzinfo=UTC)
    assert plans[2].fire_at == datetime(2026, 10, 25, 17, 0, tzinfo=UTC)


def test_day_lead_across_spring_switch(prefs: Prefs):
    now = L(2027, 3, 20, 12, 0)
    stated = L(2027, 3, 28, 19, 0)  # CEST
    due, _ = notify_due(stated, "exact", "event", [1440], prefs, now)
    assert due == L(2027, 3, 27, 19, 0)
    assert stated - due == timedelta(hours=23)


def test_minute_lead_across_autumn_switch_is_utc(prefs: Prefs):
    now = L(2026, 10, 24, 12, 0)
    stated = datetime(2026, 10, 25, 2, 0, tzinfo=UTC)  # 03:00 CET
    due, _ = notify_due(stated, "exact", "event", [120], prefs, now)
    assert due == datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # 02:00 CEST (first 02:00)


def test_shift_forward_undoes_shift_back():
    for minutes in (0, 30, 1440, 2880):
        t = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)
        assert shift_forward(shift_back(t, minutes, TZ), minutes, TZ) == t


def test_results_are_utc(now: datetime, prefs: Prefs):
    plans = plan_reminders(L(2026, 10, 8, 15, 0), "exact", "event", [1440, 5], prefs, now)
    assert len(plans) == 2
    assert all(p.fire_at.tzinfo is UTC for p in plans)


# --- past / de-duplication ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(minutes=-1), timedelta(days=-3)])
def test_due_not_in_future_has_no_reminders(delta: timedelta, now: datetime, prefs: Prefs):
    assert plan_reminders(now + delta, "exact", "event", None, prefs, now) == ()
    assert plan_reminders(now + delta, "default", "event", None, prefs, now) == ()


def test_a_time_minutes_ahead_notifies_then(now: datetime, prefs: Prefs):
    due = now + timedelta(minutes=3)
    assert fires(plan_reminders(due, "exact", "event", None, prefs, now)) == [(due, 0, False)]


def test_duplicate_leads_collapse(now: datetime, prefs: Prefs):
    assert len(plan_reminders(L(2026, 10, 9, 14, 0), "exact", "event", [60, 60], prefs, now)) == 1


# --- quiet hours -------------------------------------------------------------------------------------------


def test_auto_in_quiet_hours_moves_to_quiet_end(now: datetime, prefs: Prefs):
    due = L(2026, 10, 6, 23, 30)
    assert fires(plan_reminders(due, "default", "event", None, prefs, now)) == [(L(2026, 10, 7, 8, 0), 0, True)]


def test_auto_early_morning_moves_same_day(now: datetime, prefs: Prefs):
    due = L(2026, 10, 7, 7, 0)
    assert fires(plan_reminders(due, "date", "event", None, prefs, now)) == [(L(2026, 10, 7, 8, 0), 0, True)]


def test_explicit_in_quiet_hours_is_kept(now: datetime, prefs: Prefs):
    due = L(2026, 10, 7, 0, 30)
    assert fires(plan_reminders(due, "exact", "event", None, prefs, now)) == [(due, 0, False)]


def test_auto_quiet_shift_merges_duplicates(prefs: Prefs):
    now = L(2026, 10, 6, 12, 0)
    due = L(2026, 10, 7, 2, 0)  # "за день" before a date at 02:00 (custom bare_date_time): stated 8 Oct 02:00
    plans = plan_reminders(due, "date", "event", [1440], prefs, now)
    # 7 Oct 02:00 -> 7 Oct 08:00 (the due), 8 Oct is not a lead moment of its own
    assert fires(plans) == [(L(2026, 10, 7, 8, 0), 0, True)]


def test_quiet_disabled_keeps_auto_time(now: datetime):
    prefs = Prefs(quiet_enabled=False)
    due = L(2026, 10, 6, 23, 30)
    assert fires(plan_reminders(due, "default", "event", None, prefs, now)) == [(due, 0, True)]


def test_quiet_hours_custom_window(now: datetime):
    prefs = Prefs(quiet_start=time(22, 0), quiet_end=time(7, 30))
    due = L(2026, 10, 6, 22, 15)
    assert fires(plan_reminders(due, "date", "event", None, prefs, now)) == [(L(2026, 10, 7, 7, 30), 0, True)]
