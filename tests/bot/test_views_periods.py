"""Views for fuzzy time (docs/features/iteration-2.md §A): card time lines for spans, deadlines, "на днях" and
"когда-нибудь", reminder texts, the morning digest's period section, list rows, labels, /help.
Reference time: Tue 2026-10-06 14:05 Europe/Warsaw."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from dumpbot.bot import views
from dumpbot.bot.routers.commands import BOT_COMMANDS
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import Agenda, DigestData

PREFS = Prefs(tz="Europe/Warsaw")
TODAY = date(2026, 10, 6)


def week_item(make_item, **kw):
    return make_item(
        title="Посмотреть расбери пай",
        period_kind="week",
        period_start=date(2026, 10, 5),
        period_end=date(2026, 10, 11),
        when_spec={"text": "на этой неделе", "period": "this_week", "role": "event"},
        **kw,
    )


def auto_reminder(make_reminder, occ, fire, rid=1):
    return make_reminder(occ, lead=0, id=rid, fire_at=fire, auto=True)


# --- period_label ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "start", "end", "label"),
    [
        ("week", date(2026, 10, 5), date(2026, 10, 11), "на этой неделе"),
        ("week", date(2026, 10, 12), date(2026, 10, 18), "на следующей неделе"),
        ("week", date(2026, 10, 19), date(2026, 10, 25), "на неделе с 19 окт"),
        ("week", date(2026, 9, 28), date(2026, 10, 4), "на неделе с 28 сен"),
        ("month", date(2026, 10, 1), date(2026, 10, 31), "в этом месяце"),
        ("month", date(2026, 11, 1), date(2026, 11, 30), "в следующем месяце"),
        ("month", date(2026, 12, 1), date(2026, 12, 31), "в декабре"),
        ("month", date(2027, 9, 1), date(2027, 9, 30), "в сентябре 2027"),
        ("month", date(2027, 1, 1), date(2027, 1, 31), "в январе 2027"),
        ("season", date(2026, 9, 1), date(2026, 11, 30), "этой осенью"),
        ("season", date(2026, 12, 1), date(2027, 2, 28), "зимой"),
        ("season", date(2027, 6, 1), date(2027, 8, 31), "летом 2027"),
        ("season", date(2027, 3, 1), date(2027, 5, 31), "весной 2027"),
        ("soon", date(2026, 10, 6), date(2026, 10, 8), "на днях"),
    ],
)
def test_period_label(kind, start, end, label):
    assert views.period_label(kind, start, end, TODAY) == label


def test_period_label_next_month_across_the_year():
    assert views.period_label("month", date(2027, 1, 1), date(2027, 1, 31), date(2026, 12, 20)) == "в следующем месяце"
    assert views.period_label("season", date(2026, 12, 1), date(2027, 2, 28), date(2027, 1, 10)) == "этой зимой"
    assert views.period_label("season", date(2027, 6, 1), date(2027, 8, 31), date(2027, 7, 1)) == "этим летом"
    assert views.period_label("season", date(2027, 3, 1), date(2027, 5, 31), date(2027, 3, 1)) == "этой весной"


# --- cards ------------------------------------------------------------------------------------------------------


def test_card_of_the_live_example(make_item, make_occ, make_reminder, at, now, check_view):
    item = week_item(make_item)
    occ = make_occ(at(2026, 10, 9, 19, 0), precision="period")
    text, _ = check_view(views.item_card(item, occ, [auto_reminder(make_reminder, occ, occ.due_at)], PREFS, now))
    assert "🗓 на этой неделе (до вс 11 окт) · 🔔 пт 19:00" in text.splitlines()


def test_card_of_next_week_lists_both_reminders(make_item, make_occ, make_reminder, at, now, check_view):
    item = make_item(period_kind="week", period_start=date(2026, 10, 12), period_end=date(2026, 10, 18))
    occ = make_occ(at(2026, 10, 16, 19, 0), precision="period")
    rems = [
        auto_reminder(make_reminder, occ, at(2026, 10, 12, 10, 0), 1),
        auto_reminder(make_reminder, occ, occ.due_at, 2),
    ]
    text, _ = check_view(views.item_card(item, occ, rems, PREFS, now))
    assert "🗓 на следующей неделе (до вс 18 окт) · 🔔 пн 10:00, пт 16 окт 19:00" in text


def test_card_of_a_season_in_another_year(make_item, make_occ, make_reminder, at, now, check_view):
    item = make_item(period_kind="season", period_start=date(2027, 6, 1), period_end=date(2027, 8, 31))
    occ = make_occ(at(2027, 8, 31, 19, 0), precision="period")
    rems = [
        auto_reminder(make_reminder, occ, at(2027, 6, 1, 10, 0), 1),
        auto_reminder(make_reminder, occ, occ.due_at, 2),
    ]
    text, _ = check_view(views.item_card(item, occ, rems, PREFS, now))
    assert "🗓 летом 2027 (до 31 авг 2027) · 🔔 1 июн 2027 10:00, 31 авг 2027 19:00" in text


def test_card_of_a_done_period_item(make_item, make_occ, at, now, check_view, buttons):
    item = week_item(make_item, status="done")
    occ = make_occ(at(2026, 10, 9, 19, 0), precision="period", status="done", done_at=now)
    text, kb = check_view(views.item_card(item, occ, [], PREFS, now))
    lines = text.splitlines()
    assert "🗓 на этой неделе (до вс 11 окт)" in lines and "✅ сделано" in lines
    assert ("↩️ Не сделано", "o:undone:10") in buttons(kb)


def test_card_of_a_period_item_without_reminders(make_item, make_occ, at, check_view):
    item = week_item(make_item)
    occ = make_occ(at(2026, 10, 11, 19, 0), precision="period")
    sunday_night = at(2026, 10, 11, 20, 0)
    text, _ = check_view(views.item_card(item, occ, [], PREFS, sunday_night))
    assert "🗓 на этой неделе (до вс 11 окт) · 🔕 без напоминания" in text  # the week is not over yet
    text, _ = check_view(views.item_card(item, occ, [], PREFS, at(2026, 10, 12, 9, 0)))
    assert "🗓 на неделе с 5 окт (до вс 11 окт) · просрочено" in text


def test_card_of_a_date_deadline(make_item, make_occ, make_reminder, at, now, check_view):
    item = make_item(title="Сдать отчёт", when_spec={"text": "до пятницы", "day": "FR", "role": "deadline"})
    occ = make_occ(at(2026, 10, 9, 10, 0), precision="date")
    rems = [
        auto_reminder(make_reminder, occ, at(2026, 10, 8, 19, 0), 1),
        auto_reminder(make_reminder, occ, occ.due_at, 2),
    ]
    text, _ = check_view(views.item_card(item, occ, rems, PREFS, now))
    assert "⏳ до пт 9 окт · 🔔 чт 19:00, пт 10:00" in text


def test_card_of_a_deadline_with_a_time(make_item, make_occ, make_reminder, at, now, check_view):
    item = make_item(when_spec={"text": "до пятницы 15:00", "day": "FR", "time": "15:00", "role": "deadline"})
    occ = make_occ(at(2026, 10, 9, 15, 0), precision="exact")
    rems = [auto_reminder(make_reminder, occ, at(2026, 10, 8, 19, 0), 1), make_reminder(occ)]
    text, _ = check_view(views.item_card(item, occ, rems, PREFS, now))
    assert "⏳ до пт 9 окт 15:00 · 🔔 чт 19:00, пт 15:00" in text


def test_card_of_a_deadline_tomorrow_and_a_missed_one(make_item, make_occ, make_reminder, at, now, check_view):
    item = make_item(when_spec={"text": "до завтра", "day": "+1", "role": "deadline"})
    occ = make_occ(at(2026, 10, 7, 10, 0), precision="date")
    rems = [
        auto_reminder(make_reminder, occ, at(2026, 10, 6, 19, 0), 1),
        auto_reminder(make_reminder, occ, occ.due_at, 2),
    ]
    text, _ = check_view(views.item_card(item, occ, rems, PREFS, now))
    assert "⏳ до ср 7 окт · 🔔 сегодня 19:00, завтра 10:00" in text
    missed = make_occ(at(2026, 10, 5, 10, 0), precision="date", status="missed")
    text, _ = check_view(views.item_card(item, missed, [], PREFS, now))
    assert "⏳ до пн 5 окт" in text.splitlines()


def test_card_of_a_soon_item(make_item, make_occ, make_reminder, at, now, check_view):
    item = make_item(period_kind="soon", period_start=date(2026, 10, 6), period_end=date(2026, 10, 8))
    occ = make_occ(at(2026, 10, 8, 19, 0), precision="soon")
    text, _ = check_view(views.item_card(item, occ, [auto_reminder(make_reminder, occ, occ.due_at)], PREFS, now))
    assert "🗓 на днях · 🔔 чт 19:00" in text


def test_card_of_a_someday_item(make_item, make_occ, at, now, check_view, buttons):
    item = make_item(title="Выучить испанский", period_kind="someday", someday=True)
    text, kb = check_view(views.item_card(item, None, [], PREFS, now))
    assert text.splitlines()[1] == "🌙 когда-нибудь"
    assert [t for t, _ in buttons(kb)] == ["✏️ Изменить", "🗑 Удалить"]
    # an old cancelled / done occurrence (the item was dated before) is not shown
    old = make_occ(at(2026, 10, 7, 10, 0), precision="date", status="done")
    text, kb = check_view(views.item_card(item, old, [], PREFS, now))
    assert "🌙 когда-нибудь" in text and "✅ сделано" not in text and "⏰" not in text
    assert [t for t, _ in buttons(kb)] == ["✏️ Изменить", "🗑 Удалить"]


def test_plain_event_line_is_its_notification_time(make_item, make_occ, make_reminder, at, now, check_view):
    item = make_item(when_spec={"text": "в пятницу в 15", "day": "FR", "time": "15:00", "role": "event"})
    occ = make_occ(at(2026, 10, 9, 15, 0))
    text, _ = check_view(views.item_card(item, occ, [make_reminder(occ)], PREFS, now))
    assert "🔔 пт 9 окт 15:00" in text.splitlines()


# --- reminder messages -----------------------------------------------------------------------------------------


def test_reminder_of_a_period_item_names_the_span(make_item, make_occ, make_reminder, at, check_view, buttons):
    item = week_item(make_item)
    occ = make_occ(at(2026, 10, 9, 19, 0), precision="period")
    friday = at(2026, 10, 9, 19, 0)
    text, kb = check_view(views.reminder_message(item, occ, auto_reminder(make_reminder, occ, friday), PREFS, friday))
    assert text == "🔔 <b>Посмотреть расбери пай</b>\n🗓 на этой неделе (до вс 11 окт)"
    assert [t for t, _ in buttons(kb)] == ["✅ Готово", "⏰ 15 мин", "⏰ 1 час", "📅 Завтра"]
    done, _ = check_view(views.reminder_done(item, occ, PREFS, friday + timedelta(minutes=3)))
    assert done.splitlines()[1] == "<i>на этой неделе (до вс 11 окт) · сделано</i>"


def test_reminder_of_a_deadline_says_when_it_is_due(make_item, make_occ, make_reminder, at, check_view):
    item = make_item(title="Сдать отчёт", when_spec={"text": "до пятницы", "day": "FR", "role": "deadline"})
    occ = make_occ(at(2026, 10, 9, 10, 0), precision="date")
    thursday = at(2026, 10, 8, 19, 0)
    text, _ = check_view(views.reminder_message(item, occ, None, PREFS, thursday))
    assert text.splitlines()[1] == "⏳ срок: завтра"
    text, _ = check_view(views.reminder_message(item, occ, None, PREFS, at(2026, 10, 9, 10, 0)))
    assert text.splitlines()[1] == "⏳ срок: сегодня"
    timed = make_occ(at(2026, 10, 9, 15, 0), precision="exact")
    text, _ = check_view(views.reminder_message(item, timed, None, PREFS, at(2026, 10, 9, 14, 55)))
    assert text.splitlines()[1] == "⏳ срок: сегодня в 15:00"


# --- digests, lists, /today ---------------------------------------------------------------------------------------


def period_rows(make_row):
    return [
        make_row(
            item_id=1,
            title="Посмотреть расбери пай",
            period_kind="week",
            period_start=date(2026, 10, 5),
            period_end=date(2026, 10, 11),
        ),
        make_row(
            item_id=2,
            kind="purchase",
            title="Шины <зимние>",
            period_kind="month",
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
        ),
        make_row(
            item_id=3,
            title="Убрать дачу",
            period_kind="season",
            period_start=date(2026, 9, 1),
            period_end=date(2026, 11, 30),
        ),
    ]


def test_morning_digest_has_a_section_per_running_span(make_row, now, check_view):
    agenda = Agenda(day=TODAY, open=[], overdue=[], done=[], period_items=period_rows(make_row))
    text, kb = check_view(views.morning_digest(DigestData(kind="morning", agenda=agenda), PREFS, now))
    assert [b.text for row in kb.inline_keyboard for b in row] == ["✅ 1", "✅ 2", "✅ 3"]
    assert text.split("\n") == [
        "☀️ <b>Доброе утро!</b> План на сегодня, вт 6 окт",
        "",
        "На сегодня ничего не запланировано.",
        "",
        "🗓 <b>На этой неделе</b> (до вс 11 окт)",
        "1. 📌 Посмотреть расбери пай",
        "",
        "🗓 <b>В этом месяце</b> (до сб 31 окт)",
        "2. 🛒 Шины &lt;зимние&gt;",
        "",
        "🗓 <b>Этой осенью</b> (до пн 30 ноя)",
        "3. 📌 Убрать дачу",
    ]


def test_morning_digest_period_section_after_today_and_overdue(make_row, at, now, check_view):
    today = make_row(item_id=7, title="Созвон", due_at=at(2026, 10, 6, 15, 0), occurrence_id=70, occ_status="open")
    late = make_row(item_id=8, title="Старое", due_at=at(2026, 10, 5, 15, 0), occurrence_id=80, occ_status="open")
    agenda = Agenda(day=TODAY, open=[today], overdue=[late], done=[], period_items=period_rows(make_row)[:1])
    text, _ = check_view(views.morning_digest(DigestData(kind="morning", agenda=agenda), PREFS, now))
    assert text.index("1. 15:00 · 📌 Созвон") < text.index("Просрочено") < text.index("🗓 <b>На этой неделе</b>")


def test_rows_show_spans_and_someday_as_words(make_row, at, now, check_view):
    rows = [
        *period_rows(make_row)[:2],
        make_row(item_id=4, title="Испанский", period_kind="someday", someday=True),
        make_row(item_id=5, title="Обычное", due_at=at(2026, 10, 7, 10, 0), occurrence_id=50, occ_status="open"),
    ]
    text, _ = check_view(views.rows_list("📋 Открытые задачи", rows, PREFS, now))
    assert text.split("\n")[1:] == [
        "• на этой неделе · 📌 Посмотреть расбери пай",
        "• в этом месяце · 🛒 Шины &lt;зимние&gt;",
        "• когда-нибудь · 📌 Испанский",
        "• завтра 10:00 · 📌 Обычное",
    ]


def test_today_view_shows_the_running_spans(make_row, now, check_view):
    agenda = Agenda(day=TODAY, open=[], overdue=[], done=[], period_items=period_rows(make_row)[:1])
    text, _ = check_view(views.today_list(agenda, PREFS, now))
    assert "На сегодня ничего не запланировано." in text
    assert "🗓 <b>На этой неделе</b> (до вс 11 окт)\n1. 📌 Посмотреть расбери пай" in text


def test_help_and_command_menu_mention_someday(check_view):
    text = check_view(views.help_text())[0]
    assert "/someday" in text and "на этой неделе" in text and "когда-нибудь" in text and "до пятницы" in text
    commands = [c for c, _ in BOT_COMMANDS]
    assert commands.index("someday") == commands.index("tasks") + 1
