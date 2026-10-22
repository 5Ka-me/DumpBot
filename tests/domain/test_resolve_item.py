"""resolve_item: defaults, confirmation rules, recurrence (docs/architecture.md §4.1). An item's time is its
notification time (2026-10-07): no default lead, an explicit lead moves the due."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from dumpbot import dto
from dumpbot.ai.schemas import ExtractedItem, When
from dumpbot.domain import recurrence, timeresolve
from dumpbot.domain.prefs import Prefs
from dumpbot.domain.timeresolve import (
    WARN_BAD_RRULE,
    WARN_BAD_TIME,
    WARN_PAST_DATE,
    ReminderPlan,
    Resolution,
    resolve_item,
)

TZ = ZoneInfo("Europe/Warsaw")


def L(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


def item(kind: str = "task", *, when: dict | None = None, **kw: object) -> ExtractedItem:
    return ExtractedItem(kind=kind, title="Тест", when=When(**when) if when is not None else None, **kw)


def test_dto_reexports():
    assert timeresolve.Resolution is dto.Resolution
    assert timeresolve.ReminderPlan is dto.ReminderPlan


# --- no when ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["task", "event", "purchase"])
def test_actionable_without_time_gets_default(kind: str, now: datetime, prefs: Prefs):
    res = resolve_item(item(kind), now, prefs, source="text")
    assert res.due_at == L(2026, 10, 6, 19, 0)
    assert res.precision == "default"
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 6, 19, 0), lead_minutes=0, auto=True),)
    assert (res.rrule, res.needs_confirm, res.warnings) == (None, False, ())


def test_default_after_19_is_tomorrow(prefs: Prefs):
    res = resolve_item(item(), L(2026, 10, 6, 19, 30), prefs, source="text")
    assert res.due_at == L(2026, 10, 7, 19, 0)


def test_default_exactly_at_19_is_tomorrow(prefs: Prefs):
    res = resolve_item(item(), L(2026, 10, 6, 19, 0), prefs, source="text")
    assert res.due_at == L(2026, 10, 7, 19, 0)


def test_default_time_from_prefs(now: datetime):
    res = resolve_item(item(), now, Prefs(task_default_time=time(20, 15)), source="text")
    assert res.due_at == L(2026, 10, 6, 20, 15)


def test_default_reminder_is_moved_out_of_quiet_hours(now: datetime):
    prefs = Prefs(task_default_time=time(23, 30))
    res = resolve_item(item(), now, prefs, source="text")
    assert res.due_at == L(2026, 10, 6, 23, 30)
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 7, 8, 0), lead_minutes=0, auto=True),)


def test_default_ignores_lead(now: datetime, prefs: Prefs):
    res = resolve_item(item(lead=[60]), now, prefs, source="text")
    assert [r.lead_minutes for r in res.reminders] == [0]


@pytest.mark.parametrize("kind", ["idea", "note", "link", "meme", "quote", "other"])
def test_not_actionable_without_time_has_no_due(kind: str, now: datetime, prefs: Prefs):
    res = resolve_item(item(kind), now, prefs, source="text")
    assert res == Resolution(due_at=None, precision="none")


def test_when_with_only_text_counts_as_no_time(now: datetime, prefs: Prefs):
    res = resolve_item(item(when={"text": "когда-нибудь"}), now, prefs, source="text")
    assert res.precision == "default"
    res = resolve_item(item("idea", when={"text": "когда-нибудь"}), now, prefs, source="text")
    assert res.precision == "none"


# --- with when ----------------------------------------------------------------------------------------------


def test_exact_event_notifies_at_its_time(now: datetime, prefs: Prefs):
    res = resolve_item(item("event", when={"day": "FR", "time": "15:00"}), now, prefs, source="text")
    assert res.due_at == L(2026, 10, 9, 15, 0)
    assert res.precision == "exact"
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 9, 15, 0), lead_minutes=0, auto=False),)
    assert (res.leads, res.stated_at) == (None, None)
    assert res.due_at.tzinfo is UTC
    assert all(r.fire_at.tzinfo is UTC for r in res.reminders)


def test_no_default_lead_anywhere(now: datetime):
    # A stored "lead_minutes" from before 2026-10-07 is ignored: "в обед" notifies at 13:00, not 12:55.
    prefs = Prefs.from_stored({"tz": "Europe/Warsaw", "lead_minutes": 5})
    for when in ({"time": "lunch"}, {"time": "15:00"}, {"day": "FR", "time": "09:30"}, {"at": "2026-10-23T23:30"}):
        res = resolve_item(item("event", when=when), now, prefs, source="text")
        assert [r.fire_at for r in res.reminders] == [res.due_at], when


def test_slot_notifies_at_the_slot(now: datetime, prefs: Prefs):
    res = resolve_item(item("purchase", when={"text": "в обед", "time": "lunch"}), now, prefs, source="text")
    assert res.due_at == L(2026, 10, 7, 13, 0)
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 7, 13, 0), lead_minutes=0, auto=False),)


def test_explicit_lead_moves_the_due(now: datetime, prefs: Prefs):
    # "напомни за 30 минут до встречи в 15": the item's time is 14:30, 15:00 is the stated time (for the title).
    res = resolve_item(item("event", when={"time": "15:00"}, lead=[30]), now, prefs, source="text")
    assert (res.due_at, res.precision, res.leads, res.stated_at) == (
        L(2026, 10, 6, 14, 30),
        "exact",
        (30,),
        L(2026, 10, 6, 15, 0),
    )
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 6, 14, 30), lead_minutes=0, auto=False),)


def test_several_leads_give_several_notifications(now: datetime, prefs: Prefs):
    # "за день и за час до встречи в пт 15": the item's time is the last notification (Fri 14:00), on the day of
    # the meeting; the day before notifies earlier, like a deadline's evening before.
    res = resolve_item(item("event", when={"day": "FR", "time": "15:00"}, lead=[1440, 60]), now, prefs, source="text")
    assert (res.due_at, res.leads, res.stated_at) == (L(2026, 10, 9, 14, 0), (1440, 60), L(2026, 10, 9, 15, 0))
    assert [(r.fire_at, r.lead_minutes) for r in res.reminders] == [
        (L(2026, 10, 8, 15, 0), 23 * 60),
        (L(2026, 10, 9, 14, 0), 0),
    ]


def test_a_lead_moment_already_past_is_dropped(prefs: Prefs):
    # Said on Thu at 16:00: "за день и за час до встречи завтра в 15" — only "за час" is left.
    now = L(2026, 10, 8, 16, 0)
    res = resolve_item(item("event", when={"day": "+1", "time": "15:00"}, lead=[1440, 60]), now, prefs, source="text")
    assert (res.due_at, res.leads) == (L(2026, 10, 9, 14, 0), (60,))
    assert [r.fire_at for r in res.reminders] == [L(2026, 10, 9, 14, 0)]


def test_every_lead_moment_past_notifies_at_the_stated_time(prefs: Prefs):
    now = L(2026, 10, 6, 14, 45)
    res = resolve_item(item("event", when={"time": "15:00"}, lead=[30]), now, prefs, source="text")
    assert (res.due_at, res.leads, res.stated_at) == (L(2026, 10, 6, 15, 0), None, None)


@pytest.mark.parametrize("role", ["remind", "event"])
def test_remind_and_event_roles_both_fire_at_the_time(role: str, now: datetime, prefs: Prefs):
    res = resolve_item(item(when={"time": "15:00", "role": role}), now, prefs, source="text")
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 6, 15, 0), lead_minutes=0, auto=False),)


def test_relative(now: datetime, prefs: Prefs):
    res = resolve_item(item(when={"text": "через 2 часа", "in_min": 120}), now, prefs, source="text")
    assert (res.due_at, res.precision) == (L(2026, 10, 6, 16, 5), "relative")
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 6, 16, 5), lead_minutes=0, auto=False),)


def test_relative_with_stated_lead(now: datetime, prefs: Prefs):
    it = item("event", when={"text": "через 2 часа", "in_min": 120}, lead=[30])  # "…созвон, напомни за 30 минут"
    res = resolve_item(it, now, prefs, source="text")
    assert (res.due_at, res.leads) == (L(2026, 10, 6, 15, 35), (30,))
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 6, 15, 35), lead_minutes=0, auto=False),)


def test_date_precision_is_auto(now: datetime, prefs: Prefs):
    res = resolve_item(item(when={"day": "+1"}), now, prefs, source="text")
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 7, 10, 0), lead_minutes=0, auto=True),)


@pytest.mark.parametrize("day", ["+0", "10-06", "d6", "2026-10-06"])
@pytest.mark.parametrize("source", ["text", "forward"])
def test_today_without_time_in_the_afternoon_stays_today(day: str, source: str, now: datetime, prefs: Prefs):
    # 14:05: "сегодня купить молоко" / "концерт 6 октября" / "оплатить 6-го" — today, reminded today at 19:00.
    res = resolve_item(item(when={"text": "сегодня", "day": day}), now, prefs, source=source)
    assert (res.due_at, res.precision, res.warnings) == (L(2026, 10, 6, 19, 0), "date", ())
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 6, 19, 0), lead_minutes=0, auto=True),)
    assert res.needs_confirm is (source == "forward")


def test_today_without_time_late_at_night_is_reminded_after_quiet_hours(prefs: Prefs):
    res = resolve_item(item(when={"text": "сегодня", "day": "+0"}), L(2026, 10, 6, 23, 10), prefs, source="text")
    assert (res.due_at, res.warnings, res.needs_confirm) == (L(2026, 10, 6, 23, 59), (), False)
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 7, 8, 0), lead_minutes=0, auto=True),)


def test_date_precision_honours_whole_day_lead(now: datetime, prefs: Prefs):
    res = resolve_item(item("event", when={"day": "10-12"}, lead=[1440]), now, prefs, source="text")
    assert (res.due_at, res.stated_at) == (L(2026, 10, 11, 10, 0), L(2026, 10, 12, 10, 0))
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 11, 10, 0), lead_minutes=0, auto=True),)


def test_date_precision_ignores_minute_leads(now: datetime, prefs: Prefs):
    res = resolve_item(item("event", when={"day": "10-12"}, lead=[30]), now, prefs, source="text")
    assert (res.due_at, res.leads) == (L(2026, 10, 12, 10, 0), None)


def test_non_actionable_with_time_gets_reminder(now: datetime, prefs: Prefs):
    res = resolve_item(item("idea", when={"day": "+1", "time": "evening"}), now, prefs, source="text")
    assert res.due_at == L(2026, 10, 7, 19, 0)
    assert len(res.reminders) == 1


def test_explicit_time_in_quiet_hours_is_not_shifted(now: datetime, prefs: Prefs):
    res = resolve_item(item("event", when={"time": "23:30"}), now, prefs, source="text")
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 6, 23, 30), lead_minutes=0, auto=False),)


def test_bare_date_in_quiet_hours_is_shifted(now: datetime):
    prefs = Prefs(bare_date_time=time(7, 0))
    res = resolve_item(item(when={"day": "+1"}), now, prefs, source="text")
    assert res.due_at == L(2026, 10, 7, 7, 0)
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 7, 8, 0), lead_minutes=0, auto=True),)


def test_past_date_has_no_reminders(now: datetime, prefs: Prefs):
    res = resolve_item(item(when={"day": "2026-10-01", "time": "12:00"}), now, prefs, source="text")
    assert res.due_at == L(2026, 10, 1, 12, 0)
    assert res.reminders == ()
    assert res.needs_confirm is True
    assert res.warnings == (WARN_PAST_DATE,)


def test_fallback_at(now: datetime, prefs: Prefs):
    res = resolve_item(item("event", when={"at": "2026-11-27"}), now, prefs, source="text")
    assert (res.due_at, res.precision, res.needs_confirm) == (L(2026, 11, 27, 10, 0), "fallback", True)
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 11, 27, 10, 0), lead_minutes=0, auto=True),)


def test_at_with_time_ignores_quiet_hours(now: datetime, prefs: Prefs):
    # "через две недели в пятницу в 23:30 поезд": an explicit time, sent as usual even in quiet hours.
    res = resolve_item(item("event", when={"at": "2026-10-23T23:30"}), now, prefs, source="text")
    assert (res.due_at, res.precision, res.needs_confirm) == (L(2026, 10, 23, 23, 30), "exact", True)
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 23, 23, 30), lead_minutes=0, auto=False),)


def test_bad_date_for_actionable_falls_back_to_default(now: datetime, prefs: Prefs):
    res = resolve_item(item(when={"day": "02-30"}), now, prefs, source="text")
    assert (res.due_at, res.precision) == (L(2026, 10, 6, 19, 0), "default")
    assert res.warnings == (WARN_BAD_TIME,)
    assert res.needs_confirm is True


def test_bad_date_for_note(now: datetime, prefs: Prefs):
    res = resolve_item(item("note", when={"day": "02-30"}), now, prefs, source="text")
    assert (res.due_at, res.precision, res.reminders) == (None, "none", ())
    assert (res.warnings, res.needs_confirm) == ((WARN_BAD_TIME,), True)


# --- needs_confirm ------------------------------------------------------------------------------------------


def test_low_confidence_needs_confirm(now: datetime, prefs: Prefs):
    assert resolve_item(item(when={"time": "15:00"}, conf="low"), now, prefs, source="text").needs_confirm
    assert resolve_item(item("note", conf="low"), now, prefs, source="text").needs_confirm
    assert not resolve_item(item(when={"time": "15:00"}, conf="med"), now, prefs, source="text").needs_confirm


@pytest.mark.parametrize(("source", "confirm"), [("photo", True), ("forward", True), ("text", False), ("voice", False)])
def test_photo_and_forward_with_time_need_confirm(source: str, confirm: bool, now: datetime, prefs: Prefs):
    res = resolve_item(item("event", when={"day": "FR", "time": "15:00"}), now, prefs, source=source)
    assert res.needs_confirm is confirm
    assert len(res.reminders) == 1  # the reminder is still planned


@pytest.mark.parametrize("source", ["photo", "forward"])
def test_photo_without_time_does_not_need_confirm(source: str, now: datetime, prefs: Prefs):
    assert resolve_item(item(), now, prefs, source=source).needs_confirm is False


def test_early_hour_needs_confirm(now: datetime, prefs: Prefs):
    res = resolve_item(item(when={"text": "завтра в 5", "day": "+1", "time": "05:00"}), now, prefs, source="text")
    assert res.needs_confirm is True
    assert res.due_at == L(2026, 10, 7, 5, 0)  # the time itself is not changed


# --- recurrence ---------------------------------------------------------------------------------------------


def test_weekly_with_time(now: datetime, prefs: Prefs):
    it = item(when={"text": "каждый понедельник в 9", "time": "09:00"}, rrule="FREQ=WEEKLY;BYDAY=MO")
    res = resolve_item(it, now, prefs, source="text")
    assert res.rrule == "FREQ=WEEKLY;BYDAY=MO"
    assert res.rec_time_local == time(9, 0)
    assert res.rec_dtstart_local == datetime(2026, 10, 6, 9, 0)
    assert res.rec_dtstart_local.tzinfo is None
    assert (res.due_at, res.precision) == (L(2026, 10, 12, 9, 0), "exact")
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 12, 9, 0), lead_minutes=0, auto=False),)
    assert (res.needs_confirm, res.warnings) == (False, ())


def test_weekly_with_a_lead_is_due_its_lead_earlier(now: datetime, prefs: Prefs):
    # "каждый понедельник в 10 планёрка, напомни за 15 минут": the rule keeps 10:00, each instance is due 09:45.
    it = item(when={"text": "каждый понедельник в 10", "time": "10:00"}, rrule="FREQ=WEEKLY;BYDAY=MO", lead=[15])
    res = resolve_item(it, now, prefs, source="text")
    assert (res.rec_time_local, res.leads) == (time(10, 0), (15,))
    assert (res.due_at, res.stated_at) == (L(2026, 10, 12, 9, 45), L(2026, 10, 12, 10, 0))
    assert [r.fire_at for r in res.reminders] == [L(2026, 10, 12, 9, 45)]


def test_daily_series_keeps_today_when_only_its_lead_moment_has_passed(now: datetime, prefs: Prefs):
    # 14:05: "каждый день в 14:30, напомни за час" — the 13:30 lead moment is gone, but today's 14:30 is not: it
    # notifies at 14:30, as "в 14:30, напомни за час" without "каждый день" does. Its due stays 13:30 (stated minus
    # the lead), so the next instances follow on from it.
    it = item(when={"text": "каждый день в 14:30", "time": "14:30"}, rrule="FREQ=DAILY", lead=[60])
    res = resolve_item(it, now, prefs, source="text")
    assert (res.due_at, res.stated_at) == (L(2026, 10, 6, 13, 30), L(2026, 10, 6, 14, 30))
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 6, 14, 30), lead_minutes=0, auto=False),)
    one_off = resolve_item(item(when={"time": "14:30"}, lead=[60]), now, prefs, source="text")
    assert [r.fire_at for r in one_off.reminders] == [L(2026, 10, 6, 14, 30)]


def test_weekly_with_a_lead_sent_on_the_day_keeps_that_days_instance(prefs: Prefs):
    # Fri 10:00: "каждую пятницу в 15, напомни за день" — today's 15:00 is still ahead: notified then, not skipped
    # to Thursday next week.
    now = L(2026, 10, 9, 10, 0)
    it = item(when={"text": "каждую пятницу в 15", "time": "15:00"}, rrule="FREQ=WEEKLY;BYDAY=FR", lead=[1440])
    res = resolve_item(it, now, prefs, source="text")
    assert (res.due_at, res.stated_at) == (L(2026, 10, 8, 15, 0), L(2026, 10, 9, 15, 0))
    assert [r.fire_at for r in res.reminders] == [L(2026, 10, 9, 15, 0)]


def test_count_is_not_used_up_by_an_instance_skipped_for_its_lead(now: datetime, prefs: Prefs):
    # 14:05: "каждый день в 14:30, 3 раза, напомни за час" — today, tomorrow and the day after, as without the lead.
    it = item(when={"text": "каждый день в 14:30, 3 раза", "time": "14:30"}, rrule="FREQ=DAILY;COUNT=3", lead=[60])
    res = resolve_item(it, now, prefs, source="text")
    assert res.rec_dtstart_local == datetime(2026, 10, 6, 14, 30)
    tz = prefs.zone
    stated, instances = res.stated_at, []
    while stated is not None:
        instances.append(stated)
        stated = recurrence.next_occurrence(res.rrule, res.rec_dtstart_local, tz, stated)
    assert instances == [L(2026, 10, 6, 14, 30), L(2026, 10, 7, 14, 30), L(2026, 10, 8, 14, 30)]


def test_count_is_not_used_up_by_a_time_already_past_today(now: datetime, prefs: Prefs):
    # 14:05: "каждый день в 9, 2 раза" means tomorrow and the day after, not "today 09:00 (past) + tomorrow".
    it = item(when={"text": "каждый день в 9, 2 раза", "time": "09:00"}, rrule="FREQ=DAILY;COUNT=2")
    res = resolve_item(it, now, prefs, source="text")
    assert res.rec_dtstart_local == datetime(2026, 10, 7, 9, 0)
    assert res.due_at == L(2026, 10, 7, 9, 0)
    tz = prefs.zone
    second = recurrence.next_occurrence(res.rrule, res.rec_dtstart_local, tz, res.due_at)
    assert second == L(2026, 10, 8, 9, 0)
    assert recurrence.next_occurrence(res.rrule, res.rec_dtstart_local, tz, second) is None
    # a time still ahead today stays the first instance
    later = item(when={"text": "каждый день в 18, 2 раза", "time": "18:00"}, rrule="FREQ=DAILY;COUNT=2")
    res = resolve_item(later, now, prefs, source="text")
    assert (res.rec_dtstart_local, res.due_at) == (datetime(2026, 10, 6, 18, 0), L(2026, 10, 6, 18, 0))


def test_recurring_without_time_uses_task_default(now: datetime, prefs: Prefs):
    res = resolve_item(item(when={"text": "каждый день"}, rrule="FREQ=DAILY"), now, prefs, source="text")
    assert (res.due_at, res.precision, res.rec_time_local) == (L(2026, 10, 6, 19, 0), "default", time(19, 0))
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 6, 19, 0), lead_minutes=0, auto=True),)


def test_recurring_without_when(now: datetime, prefs: Prefs):
    res = resolve_item(item(rrule="FREQ=WEEKLY;BYDAY=FR"), now, prefs, source="text")
    assert (res.due_at, res.precision) == (L(2026, 10, 9, 19, 0), "default")


def test_recurring_with_slot(now: datetime, prefs: Prefs):
    it = item(when={"text": "каждое утро", "time": "morning"}, rrule="FREQ=DAILY")
    res = resolve_item(it, now, prefs, source="text")
    assert (res.due_at, res.precision, res.rec_time_local) == (L(2026, 10, 7, 9, 0), "slot", time(9, 0))


def test_recurring_today_still_ahead_is_included(now: datetime, prefs: Prefs):
    it = item(when={"time": "15:00"}, rrule="FREQ=WEEKLY;BYDAY=TU")
    assert resolve_item(it, now, prefs, source="text").due_at == L(2026, 10, 6, 15, 0)


def test_recurring_day_only_sets_the_start(now: datetime, prefs: Prefs):
    it = item(when={"day": "+3", "time": "10:00"}, rrule="FREQ=MONTHLY;BYMONTHDAY=5")
    res = resolve_item(it, now, prefs, source="text")
    assert res.due_at == L(2026, 11, 5, 10, 0)
    assert res.rec_dtstart_local == datetime(2026, 10, 9, 10, 0)


@pytest.mark.parametrize(
    ("when", "rule", "start", "due", "second"),
    [
        # "с понедельника каждый день в 9 пить таблетки" (Tue): starts Monday, not tomorrow
        ({"day": "MO", "time": "09:00"}, "FREQ=DAILY", datetime(2026, 10, 12, 9, 0), L(2026, 10, 12, 9, 0), 13),
        # "каждые две недели по пятницам, начиная со следующей": 16 Oct, 30 Oct… (not 9 Oct, 23 Oct…)
        (
            {"day": "next FR", "time": "18:00"},
            "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR",
            datetime(2026, 10, 16, 18, 0),
            L(2026, 10, 16, 18, 0),
            30,
        ),
        # "каждый год 15 марта": a bare FREQ=YEARLY follows the start date, not the day it was said
        ({"day": "03-15"}, "FREQ=YEARLY", datetime(2027, 3, 15, 19, 0), L(2027, 3, 15, 19, 0), None),
    ],
)
def test_recurring_starts_on_the_stated_day(
    when: dict, rule: str, start: datetime, due: datetime, second: int | None, now: datetime, prefs: Prefs
):
    res = resolve_item(item(when=when, rrule=rule), now, prefs, source="text")
    assert (res.rec_dtstart_local, res.due_at) == (start, due)
    nxt = recurrence.next_occurrence(res.rrule, res.rec_dtstart_local, prefs.zone, res.due_at)
    assert nxt is not None
    if second is not None:
        assert nxt.astimezone(TZ).day == second
    else:
        assert nxt == L(2028, 3, 15, 19, 0)


def test_recurring_day_in_the_past_starts_today(now: datetime, prefs: Prefs):
    it = item(when={"day": "2026-09-01", "time": "18:00"}, rrule="FREQ=DAILY")
    res = resolve_item(it, now, prefs, source="text")
    assert (res.rec_dtstart_local, res.due_at) == (datetime(2026, 10, 6, 18, 0), L(2026, 10, 6, 18, 0))


def test_recurring_from_photo_needs_confirm(now: datetime, prefs: Prefs):
    res = resolve_item(item(rrule="FREQ=DAILY"), now, prefs, source="photo")
    assert res.needs_confirm is True


def test_recurring_low_confidence(now: datetime, prefs: Prefs):
    res = resolve_item(item(rrule="FREQ=DAILY", conf="low"), now, prefs, source="text")
    assert res.needs_confirm is True
    assert res.rrule == "FREQ=DAILY"


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=WEEKLY;BYSECOND=9",  # key outside the allowlist
        "FREQ=WEEKLY;BYDAY=XX",
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30",  # never happens
        "FREQ=DAILY;UNTIL=20260101",  # already over
    ],
)
def test_invalid_rrule_degrades_to_one_off(rule: str, now: datetime, prefs: Prefs):
    res = resolve_item(item(when={"day": "+1", "time": "09:00"}, rrule=rule), now, prefs, source="text")
    assert res.rrule is None
    assert (res.rec_time_local, res.rec_dtstart_local) == (None, None)
    assert res.warnings == (WARN_BAD_RRULE,)
    assert res.needs_confirm is True
    assert res.due_at == L(2026, 10, 7, 9, 0)


def test_invalid_rrule_note_without_time(now: datetime, prefs: Prefs):
    res = resolve_item(item("note", rrule="FREQ=WEEKLY;BYSECOND=9"), now, prefs, source="text")
    assert (res.due_at, res.rrule, res.warnings) == (None, None, (WARN_BAD_RRULE,))


def test_recurring_due_across_dst(prefs: Prefs):
    now = L(2026, 10, 20, 9, 30)  # Tuesday after the Monday 09:00 occurrence
    it = item(when={"time": "09:00"}, rrule="FREQ=WEEKLY;BYDAY=MO")
    res = resolve_item(it, now, prefs, source="text")
    assert res.due_at == datetime(2026, 10, 26, 8, 0, tzinfo=UTC)  # 09:00 CET
    assert res.reminders[0].fire_at == res.due_at


# --- several times a day (docs/features/iteration-4.md §2) ----------------------------------------------------------


def test_two_times_a_day_are_one_series(now: datetime, prefs: Prefs):
    """ "каждый день в 9:00 и 18:00" at 14:05: one series, its rule names both times; the next one is today's 18:00."""
    it = item(when={"text": "каждый день в 9:00 и 18:00", "time": "09:00"}, rrule="FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0")
    res = resolve_item(it, now, prefs, source="text")
    assert res.rrule == "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0"
    assert res.due_at == L(2026, 10, 6, 18, 0)
    assert res.precision == "exact"
    assert res.rec_dtstart_local == datetime(2026, 10, 6, 9, 0)  # the day starts at its first time
    assert res.reminders == (ReminderPlan(fire_at=L(2026, 10, 6, 18, 0), lead_minutes=0, auto=False),)
    assert recurrence.rule_times(res.rrule) == (time(9, 0), time(18, 0))


def test_bare_hours_take_the_minutes_of_the_time(now: datetime, prefs: Prefs):
    it = item(when={"text": "в 9:30 и 18:30", "time": "09:30"}, rrule="FREQ=DAILY;BYHOUR=9,18")
    assert resolve_item(it, now, prefs, source="text").rrule == "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=30"


def test_hours_without_a_time_are_whole_hours_and_exact(now: datetime, prefs: Prefs):
    """No when.time (the model put the times in the rule only): BYMINUTE=0, the instances are exact (not the
    automatic 19:00 of a task without a time)."""
    res = resolve_item(item(rrule="FREQ=DAILY;BYHOUR=9,21"), now, prefs, source="text")
    assert (res.rrule, res.precision) == ("FREQ=DAILY;BYHOUR=9,21;BYMINUTE=0", "exact")
    assert res.due_at == L(2026, 10, 6, 21, 0)
    assert res.reminders[0].auto is False


def test_different_minutes_are_paired_by_the_phrase(now: datetime, prefs: Prefs):
    """ "в 8:30 и 20:00" written as hours x minutes would be four times: the ones the phrase names stay."""
    it = item(
        when={"text": "каждый день в 8:30 и 20:00", "time": "08:30"}, rrule="FREQ=DAILY;BYHOUR=8,20;BYMINUTE=30,0"
    )
    res = resolve_item(it, now, prefs, source="text")
    assert res.rrule == "FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0,30;BYSETPOS=2,3"
    assert recurrence.rule_times(res.rrule) == (time(8, 30), time(20, 0))
    assert res.due_at == L(2026, 10, 6, 20, 0)


def test_a_product_the_phrase_does_not_name_stays(now: datetime, prefs: Prefs):
    it = item(when={"text": "каждые полчаса", "time": "09:00"}, rrule="FREQ=DAILY;BYHOUR=9,10;BYMINUTE=0,30")
    res = resolve_item(it, now, prefs, source="text")
    assert recurrence.rule_times(res.rrule) == (time(9, 0), time(9, 30), time(10, 0), time(10, 30))


def test_a_monthly_rule_picking_a_day_keeps_one_time(now: datetime, prefs: Prefs):
    """BYSETPOS of "в первый будний день" would pick one of the times anyway: the rule says so (the card then warns
    «понял только 09:00»)."""
    rule = "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1;BYHOUR=9,18;BYMINUTE=0"
    res = resolve_item(item(when={"time": "09:00"}, rrule=rule), now, prefs, source="text")
    assert res.rrule == "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1;BYHOUR=9;BYMINUTE=0"


def test_times_with_a_start_day_count_today_while_one_is_ahead(now: datetime, prefs: Prefs):
    """ "по вторникам в 9 и 18" said on Tuesday 14:05: this evening's 18:00 is the first (not next Tuesday)."""
    it = item(when={"day": "TU", "time": "09:00"}, rrule="FREQ=WEEKLY;BYDAY=TU;BYHOUR=9,18;BYMINUTE=0")
    res = resolve_item(it, now, prefs, source="text")
    assert res.due_at == L(2026, 10, 6, 18, 0)


def test_times_and_count_start_at_the_next_time(now: datetime, prefs: Prefs):
    """COUNT counts from DTSTART: 09:00 has passed at 14:05, so the three are today's 18:00, then tomorrow's two."""
    it = item(when={"time": "09:00"}, rrule="FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0;COUNT=3")
    res = resolve_item(it, now, prefs, source="text")
    assert res.rec_dtstart_local == datetime(2026, 10, 6, 18, 0)
    assert res.due_at == L(2026, 10, 6, 18, 0)
    start = res.rec_dtstart_local
    assert recurrence.next_occurrence(res.rrule, start, TZ, L(2026, 10, 7, 8, 0)) == L(2026, 10, 7, 9, 0)
    assert recurrence.next_occurrence(res.rrule, start, TZ, L(2026, 10, 7, 9, 0)) == L(2026, 10, 7, 18, 0)
    assert recurrence.next_occurrence(res.rrule, start, TZ, L(2026, 10, 7, 18, 0)) is None


def test_times_with_a_lead_shift_every_instance(now: datetime, prefs: Prefs):
    it = item(when={"time": "09:00"}, rrule="FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0", lead=[15])
    res = resolve_item(it, now, prefs, source="text")
    assert res.due_at == L(2026, 10, 6, 17, 45)
    assert res.stated_at == L(2026, 10, 6, 18, 0)
