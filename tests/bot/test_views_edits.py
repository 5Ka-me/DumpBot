"""views.edit_applied: the short reply to an edit applied by description (docs/features/iteration-3.md §3)."""

from __future__ import annotations

from datetime import date, timedelta

from dumpbot.ai.schemas import EditPatch
from dumpbot.bot import views
from dumpbot.bot.cbdata import UndoCb

BIG = 10**12


def patch(**kw) -> EditPatch:
    return EditPatch.model_validate(kw)


def render(item, occ, reminders, p, prefs, now, **kw):
    kw.setdefault("title_before", item.title)
    kw.setdefault("moved", p.when is not None or bool(p.rrule) or bool(p.lead))
    return views.edit_applied(item, occ, reminders, p, prefs, now, **kw)


def test_done_with_the_undo_button(prefs, now, at, make_item, make_occ, check_view, buttons):
    item = make_item(status="done")
    occ = make_occ(at(2026, 10, 6, 17, 0), status="done")
    text, kb = check_view(render(item, occ, [], patch(action="done"), prefs, now, undo=(BIG, BIG + 1)))
    assert text == "✅ Закрыл «Купить наушники»"
    assert buttons(kb) == [("↩️ Отменить", UndoCb(raw=BIG, item=BIG + 1).pack())]


def test_done_of_a_parent_and_of_a_series(prefs, now, at, make_item, make_occ, make_reminder, check_view):
    parent = make_item(title="Переезд", status="done")
    text, kb = check_view(render(parent, None, [], patch(action="done"), prefs, now, steps_closed=3))
    assert (text, kb) == ("✅ Закрыл «Переезд» и 3 шага", None)

    series = make_item(title="Зарядка", rrule="FREQ=DAILY", rec_active=True)
    nxt = make_occ(at(2026, 10, 7, 9, 0))
    text, _ = check_view(render(series, nxt, [make_reminder(nxt)], patch(action="done"), prefs, now))
    assert text == "✅ Закрыл «Зарядка» · следующий раз завтра 09:00"


def test_already_done_and_already_open(prefs, now, make_item, check_view):
    item = make_item(status="done")
    text, _ = check_view(render(item, None, [], patch(action="done"), prefs, now, changed=False))
    assert text == "✅ «Купить наушники» — уже сделано"
    text, _ = check_view(render(make_item(), None, [], patch(action="undone"), prefs, now, changed=False))
    assert text == "↩️ «Купить наушники» — и так не сделано"
    text, _ = check_view(render(make_item(), None, [], patch(action="undone"), prefs, now))
    assert text == "↩️ Снова открыл «Купить наушники»"


def test_delete_shows_only_the_delete(prefs, now, make_item, check_view):
    item = make_item(status="deleted", title="Старая <заметка>")
    text, _ = check_view(render(item, None, [], patch(action="delete", title="x"), prefs, now))
    assert text == "🗑 Удалил «Старая &lt;заметка&gt;»"


def test_new_time(prefs, now, at, make_item, make_occ, make_reminder, check_view):
    item = make_item(title="Виза")
    occ = make_occ(at(2026, 10, 7, 10, 0))
    p = patch(target="визу", when={"text": "на завтра", "day": "+1"})
    text, _ = check_view(render(item, occ, [make_reminder(occ)], p, prefs, now))
    assert text == "🕒 Перенёс «Виза» на завтра 10:00"

    # several notifications ("за день и за час"): the card's time line under it
    occ = make_occ(at(2026, 10, 8, 14, 0))
    reminders = [make_reminder(occ), make_reminder(occ, lead=1440)]
    p = patch(when={"text": "в чт в 15", "day": "TH", "time": "15:00"}, lead=[1440, 60])
    text, _ = check_view(render(make_item(title="Стоматолог (чт 15:00)"), occ, reminders, p, prefs, now))
    assert text.split("\n") == [
        "🕒 Перенёс «Стоматолог (чт 15:00)» на чт 8 окт 14:00",
        "🔔 завтра 14:00, чт 8 окт 14:00",
    ]


def test_new_time_with_reminders_off_or_in_the_past(prefs, now, at, make_item, make_occ, check_view):
    occ = make_occ(at(2026, 10, 7, 10, 0))
    text, _ = check_view(render(make_item(lead_minutes=[]), occ, [], patch(when={"day": "+1"}), prefs, now))
    assert text.split("\n") == ["🕒 Перенёс «Купить наушники» на завтра 10:00", "🔕 завтра 10:00"]

    past = make_occ(at(2026, 10, 1, 10, 0))
    item = make_item(needs_confirm=True)
    p = patch(when={"day": "2026-10-01"})
    text, _ = check_view(render(item, past, [], p, prefs, now, warnings=["дата в прошлом"]))
    assert text.split("\n") == [
        "🕒 Перенёс «Купить наушники» на чт 1 окт 10:00",
        "⏰ чт 1 окт 10:00 · просрочено",
        "⚠️ дата в прошлом",
    ]


def test_span_someday_and_lead(prefs, now, at, make_item, make_occ, make_reminder, check_view):
    item = make_item(title="Шины", period_kind="week", period_start=date(2026, 10, 12), period_end=date(2026, 10, 18))
    occ = make_occ(at(2026, 10, 16, 19, 0), precision="period")
    p = patch(when={"text": "на следующей неделе", "period": "next_week"})
    text, _ = check_view(render(item, occ, [make_reminder(occ)], p, prefs, now))
    assert text.split("\n") == ["🕒 Перенёс «Шины»", "🗓 на следующей неделе (до вс 18 окт) · 🔔 пт 16 окт 19:00"]

    p = patch(when={"text": "когда-нибудь", "period": "someday"})
    text, _ = check_view(render(make_item(someday=True), None, [], p, prefs, now))
    assert text == "🌙 Отложил «Купить наушники» на когда-нибудь"

    occ = make_occ(at(2026, 10, 6, 14, 0))
    text, _ = check_view(
        render(make_item(title="Встреча (15:00)"), occ, [make_reminder(occ)], patch(lead=[60]), prefs, now)
    )
    assert text == "🔔 Напомню о «Встреча (15:00)» сегодня 14:00"


def test_time_not_understood(prefs, now, at, make_item, make_occ, make_reminder, check_view):
    occ = make_occ(at(2026, 10, 6, 15, 0))
    p = patch(when={"text": "потом"})
    text, _ = check_view(render(make_item(), occ, [make_reminder(occ)], p, prefs, now, moved=False, changed=False))
    assert text == "🤷 Время «Купить наушники» не изменилось — не понял, на когда"


def test_series_rename_kind_tags_and_more(prefs, now, at, make_item, make_occ, make_reminder, check_view):
    item = make_item(title="Вынести мусор", rrule="FREQ=WEEKLY;BYDAY=MO", rec_active=True)
    occ = make_occ(at(2026, 10, 12, 9, 0))
    p = patch(rrule="FREQ=WEEKLY;BYDAY=MO", when={"time": "09:00"})
    text, _ = check_view(render(item, occ, [make_reminder(occ)], p, prefs, now))
    assert text.split("\n") == ["🔁 «Вынести мусор» — каждый понедельник", "🔔 пн 12 окт 09:00"]

    item = make_item(title="Дописать README", kind="idea", tags=["docs", "работа"])
    p = patch(title="Дописать README", kind="idea", tags_add=["docs"])
    text, _ = check_view(render(item, None, [], p, prefs, now, title_before="Написать <документацию>"))
    assert text.split("\n") == [
        "✏️ Переименовал «Написать &lt;документацию&gt;» → «Дописать README»",
        "🏷 «Дописать README» — теперь идея",
        "#️⃣ Теги «Дописать README»: #docs #работа",
    ]

    text, _ = check_view(render(make_item(), None, [], patch(action="no_reminder"), prefs, now))
    assert text == "🔕 Выключил напоминания «Купить наушники»"
    # a stop of a one-off is "done" (iteration 4, §2); of a series it says whether this time was closed with it
    text, _ = check_view(render(make_item(), None, [], patch(action="stop_recurrence"), prefs, now))
    assert text == "✅ Закрыл «Купить наушники»"
    series = make_item(rrule="FREQ=DAILY", rec_active=False, status="done", done_at=now)
    text, _ = check_view(
        render(series, make_occ(now, status="done", done_at=now), [], patch(action="stop_recurrence"), prefs, now)
    )
    assert text == "⏹ Остановил повтор «Купить наушники» и закрыл этот раз"
    earlier = make_occ(now - timedelta(hours=5), status="done", done_at=now - timedelta(hours=5))
    text, _ = check_view(render(series, earlier, [], patch(action="stop_recurrence"), prefs, now))
    assert text == "⏹ Остановил повтор «Купить наушники»"
    text, _ = check_view(render(make_item(), None, [], patch(), prefs, now, changed=False))
    assert text == "🤷 В «Купить наушники» ничего не изменилось"
    text, _ = check_view(render(make_item(), None, [], patch(), prefs, now))
    assert text == "✏️ Изменил «Купить наушники»"


def test_long_titles_are_clipped(prefs, now, make_item, check_view):
    item = make_item(title="Очень длинное название " * 20, status="done")
    text, _ = check_view(render(item, None, [], patch(action="done"), prefs, now))
    assert len(text) < 200 and text.endswith("…»")


def test_help_mentions_edits_by_description():
    assert "«закрой задачу про документацию»" in views.help_text() and "↩️ Отменить" in views.help_text()
