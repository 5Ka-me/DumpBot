"""bot.views, iteration 4: the stale question (§4) and the weekly review (§6); the settings of the review."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from dumpbot.bot import views
from dumpbot.bot.cbdata import LIST_WEEKLY, ListCb, SetCb, StaleCb
from dumpbot.bot.routers.commands import START_TEXT
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import WeeklyData

PREFS = Prefs()
SUNDAY = date(2026, 10, 11)


def test_the_stale_question(check_view, buttons, make_item, make_occ, make_reminder, now, at) -> None:
    item = make_item(id=12, title="Сделать <карту>", created_at=now - timedelta(days=15))
    occ = make_occ(at(2026, 10, 5, 19, 0), id=30, precision="default")
    text, kb = check_view(views.stale_question(item, occ, [], PREFS, now, moves=0))
    assert text.split("\n") == [
        "🕸 📌 «Сделать &lt;карту&gt;» висит 15 дней. Что делаем?",
        "⏰ вчера 19:00 · просрочено",
    ]
    assert buttons(kb) == [
        ("✅ Сделано", "st:done:12"),
        ("💤 Когда-нибудь", "st:smd:12"),
        ("🗑 Удалить", "st:del:12"),
        ("Оставить", "st:keep:12"),
    ]
    assert [len(row) for row in kb.inline_keyboard] == [2, 2]


def test_the_stale_question_says_how_often_it_was_moved(
    check_view, make_item, make_occ, make_reminder, now, at
) -> None:
    item = make_item(created_at=now - timedelta(days=2))
    occ = make_occ(at(2026, 10, 9, 19, 0))
    text, _ = check_view(views.stale_question(item, occ, [make_reminder(occ)], PREFS, now, moves=3))
    assert text.split("\n") == [
        "🕸 📌 «Купить наушники» висит 2 дня. Что делаем?",
        "Переносили уже 3 раза.",
        "🔔 пт 9 окт 19:00",
    ]
    today = make_item(created_at=now - timedelta(hours=3))
    text, _ = check_view(views.stale_question(today, None, [], PREFS, now, moves=2))
    assert text == "🕸 📌 «Купить наушники» переносили уже 2 раза. Что делаем?"


def test_the_stale_answers(check_view, buttons, make_item, make_occ, now, at) -> None:
    item = make_item(id=12, status="done")
    occ = make_occ(at(2026, 10, 5, 19, 0), id=30, status="done")
    text, kb = check_view(views.stale_done(item, occ, PREFS, now))
    assert text.startswith("✅ <s>Купить наушники</s>") and buttons(kb) == [("↩️ Не сделано", "o:undone:30")]
    assert views.stale_done(item, None, PREFS, now) == ("✅ <s>Купить наушники</s>", None)
    assert check_view(views.stale_someday(item))[0] == "💤 «Купить наушники» — когда-нибудь, запись в /someday"
    assert check_view(views.stale_kept(item))[0].startswith("👌 Оставил «Купить наушники» как есть")


def test_stale_callback_data_fits() -> None:
    assert len(StaleCb(a="keep", id=10**18).pack().encode()) <= 64
    assert StaleCb.unpack("st:smd:7") == StaleCb(a="smd", id=7)


# --- the weekly review ---------------------------------------------------------------------------------------------


def test_the_weekly_review(check_view, buttons, make_row, now, at) -> None:
    sunday = at(2026, 10, 11, 19, 0)
    week = make_row(
        item_id=1, occurrence_id=11, title="Шины", period_kind="week",
        period_start=date(2026, 10, 12), period_end=date(2026, 10, 18), due_at=at(2026, 10, 16, 19, 0),
    )  # fmt: skip
    idea = make_row(item_id=2, kind="idea", title="Испанский", someday=True)
    call = make_row(item_id=3, occurrence_id=33, title="Позвонить <Пете>", due_at=at(2026, 10, 8, 12, 0))
    tap = make_row(item_id=4, occurrence_id=44, title="Починить кран", due_at=at(2026, 10, 1, 19, 0))
    data = WeeklyData(day=SUNDAY, period=[week], someday=[idea], someday_total=3, unanswered=[call], undated=[tap])

    text, kb = check_view(views.weekly_review(data, PREFS, sunday))

    assert text.split("\n") == [
        "🗓 <b>Обзор недели</b>, вс 11 окт",
        "",
        "🗓 <b>На следующей неделе</b> (до вс 18 окт)",
        "1. 📌 Шины",
        "",
        "🌙 <b>Когда-нибудь</b>",
        "2. 💡 Испанский",
        "<i>… и ещё 2 — в /someday</i>",
        "",
        "🔕 <b>Напоминания без ответа</b>",
        "3. чт 8 окт 12:00 · 📌 Позвонить &lt;Пете&gt;",
        "",
        "📋 <b>Задачи без даты</b>",
        "4. 📌 Починить кран",
    ]
    assert buttons(kb) == [
        ("✅ 1", "ld:done:w:20261011:p:1:11:1"),
        ("✅ 2", "ld:done:w:20261011:s:2:0:2"),
        ("✅ 3", "ld:done:w:20261011:r:3:33:3"),
        ("✅ 4", "ld:done:w:20261011:u:4:44:4"),
    ]


def test_a_weekly_line_closed_by_its_button(check_view, buttons, make_row, at) -> None:
    sunday = at(2026, 10, 11, 19, 30)
    idea = make_row(item_id=2, kind="idea", title="Испанский", someday=True, status="done")
    data = WeeklyData(day=SUNDAY, someday=[], someday_total=0)  # closed: no longer "когда-нибудь"
    lines = views.pin_lines(LIST_WEEKLY, views.weekly_sections(data), [(views.SEC_SOMEDAY, 1, idea)])
    text, kb = check_view(views.weekly_review(data, PREFS, sunday, lines=lines))
    assert "1. ✅ 💡 <s>Испанский</s>" in text and "и ещё" not in text
    assert buttons(kb) == [("↩️ 1", ListCb(a="undo", k="w", d="20261011", s="s", n=1, o=0, i=2).pack())]


def test_an_emptied_weekly_review(check_view) -> None:
    text, kb = check_view(views.weekly_review(WeeklyData(day=SUNDAY), PREFS, datetime(2026, 10, 11, 18)))
    assert text.endswith("Всё разобрано 🎉") and kb is None


def test_weekly_lines_are_matched_by_item(make_row) -> None:
    old = make_row(item_id=5, occurrence_id=50, title="Таблетки")
    fresh = make_row(item_id=5, occurrence_id=51, title="Таблетки")
    lines = views.pin_lines(LIST_WEEKLY, {views.SEC_UNANSWERED: [fresh]}, [(views.SEC_UNANSWERED, 1, old)])
    assert lines[views.SEC_UNANSWERED] == [(old, 1)]  # the same item: one line


# --- settings and help ---------------------------------------------------------------------------------------------


def test_the_digest_menu_has_the_weekly_review(check_view, buttons) -> None:
    text, kb = check_view(views.settings_digest_menu(Prefs()))
    assert "🗓 Обзор недели (воскресенье): 19:00, вкл" in text
    assert ("🗓 Выключить обзор недели", SetCb(key="digest_toggle", v="weekly").pack()) in buttons(kb)
    assert ("🗓 Время 19:00", SetCb(key="ask", v="digest_weekly_time").pack()) in buttons(kb)
    off = Prefs(digest_weekly_enabled=False)
    assert ("🗓 Включить обзор недели", "s:digest_toggle:weekly") in buttons(views.settings_digest_menu(off)[1])
    assert "обзор недели вс 19:00 (выкл)" in views.settings_menu(off)[0]
    assert views.ASK_FIELDS["digest_weekly_time"].menu == "digest"


def test_help_mentions_the_review_and_stale_items_and_start_fits() -> None:
    text = views.help_text()
    assert "в воскресенье 19:00 — обзор недели" in text and "раз в день спрошу" in text
    assert "в первый и последний рабочий день срока" in text
    assert views.tg_len(f"{START_TEXT}\n\n{text}") <= views.MAX_TEXT
