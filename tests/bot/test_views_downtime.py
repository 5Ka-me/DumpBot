"""Iteration 3, §5 and §6 (docs/features/iteration-3.md): the notice after a downtime, yesterday's summary in the
morning digest, a reminder held over quiet hours; what the bot heard in a voice note ("🎤 «…»")."""

from __future__ import annotations

from datetime import date, time

from dumpbot.bot import views
from dumpbot.bot.cbdata import ItemCb, ListCb
from dumpbot.dto import Agenda, CardExtras, DigestData


def lines(text: str) -> list[str]:
    return text.split("\n")


def _agenda(day: date, **kw) -> Agenda:
    values = {"day": day, "open": [], "overdue": [], "done": []}
    values.update(kw)
    return Agenda(**values)


def _cb(n: int, item: int, occ: int, *, s: str, a: str = "done") -> str:
    return ListCb(a=a, k="m", d="20261007", s=s, n=n, o=occ, i=item).pack()


# --- the notice after a downtime (§5) ----------------------------------------------------------------------------


def test_downtime_notice_as_in_the_spec(prefs, at, check_view):
    text, _ = check_view(
        views.downtime_notice(
            at(2026, 10, 6, 19, 0),
            at(2026, 10, 6, 23, 19),
            prefs,
            late=1,
            folded=date(2026, 10, 6),
            morning=True,
        )
    )
    assert text == (
        "😴 Меня не было с 19:00 до 23:19 (компьютер спал?). Опоздало напоминаний: 1. Итог дня — в утреннем дайджесте."
    )


def test_downtime_notice_says_when_held_reminders_come(prefs, at):
    start, end, morning = at(2026, 10, 6, 19, 0), at(2026, 10, 6, 23, 19), at(2026, 10, 7, 8, 0)
    only_held = views.downtime_notice(start, end, prefs, late=2, held=2, held_until=morning)
    assert only_held.endswith("Опоздало напоминаний: 2 — пришлю в 08:00.")
    mixed = views.downtime_notice(start, end, prefs, late=3, held=1, held_until=morning)
    assert mixed.endswith("Опоздало напоминаний: 3 (1 пришлю в 08:00).")


def test_downtime_notice_across_midnight_names_the_day(prefs, at):
    text = views.downtime_notice(
        at(2026, 10, 6, 19, 0),
        at(2026, 10, 7, 8, 30),
        prefs,
        missed=2,
        folded=date(2026, 10, 6),
        morning=True,
    )
    assert text == (
        "😴 Меня не было с вчера 19:00 до 08:30 (компьютер спал?). "
        "Не прислал устаревших напоминаний: 2 — эти дела в /today. Вчерашний итог — в утреннем дайджесте."
    )


def test_downtime_notice_without_a_morning_digest_points_to_today(prefs, at):
    text = views.downtime_notice(
        at(2026, 10, 6, 19, 0), at(2026, 10, 6, 23, 30), prefs, folded=date(2026, 10, 6), morning=False
    )
    assert text.endswith("(компьютер спал?). Итог дня не прислал — несделанное в /today.")
    assert "Опоздало" not in text


# --- a reminder held over quiet hours (§5) -----------------------------------------------------------------------


def test_held_reminder_says_how_late_it_is_from_its_own_time(prefs, at, make_item, make_occ, make_reminder, check_view):
    item = make_item(title="Позвонить маме")
    occ = make_occ(at(2026, 10, 6, 20, 0))
    reminder = make_reminder(occ, fire_at=at(2026, 10, 7, 8, 0), held_from=at(2026, 10, 6, 20, 0))
    text, _ = check_view(views.reminder_message(item, occ, reminder, prefs, at(2026, 10, 7, 8, 0)))
    assert lines(text) == ["🔔 <b>Позвонить маме</b>", "⏰ вчера 20:00 · опоздал на 12 ч"]


# --- yesterday's summary in the morning digest (§5) --------------------------------------------------------------


def _folded_morning(at, make_row, *, closed: bool = False) -> DigestData:
    bread_done = {"occ_status": "done"} if closed else {}
    yesterday = _agenda(
        date(2026, 10, 6),
        open=[
            make_row(item_id=5, occurrence_id=51, title="Купить хлеб", due_at=at(2026, 10, 6, 19, 0), **bread_done),
            make_row(item_id=6, occurrence_id=61, title="Позвонить маме", due_at=at(2026, 10, 6, 20, 0)),
        ],
        done=[make_row(item_id=7, occurrence_id=71, title="Отчёт", occ_status="done")],
    )
    today = _agenda(
        date(2026, 10, 7),
        open=[make_row(item_id=1, occurrence_id=11, title="Спорт", due_at=at(2026, 10, 7, 18, 0))],
        overdue=[
            make_row(item_id=3, occurrence_id=31, title="Старое", due_at=at(2026, 10, 5, 19, 0)),
            make_row(item_id=6, occurrence_id=61, title="Позвонить маме", due_at=at(2026, 10, 6, 20, 0)),
        ],
    )
    return DigestData(kind="morning", agenda=today, yesterday=yesterday)


def test_morning_digest_with_yesterdays_summary(prefs, at, make_row, check_view, buttons):
    data = _folded_morning(at, make_row)
    text, kb = check_view(views.morning_digest(data, prefs, at(2026, 10, 7, 9, 0)))
    assert lines(text) == [
        "☀️ <b>Доброе утро!</b> План на сегодня, ср 7 окт",
        "",
        "1. 18:00 · 📌 Спорт",
        "",
        "🌙 <b>Вчерашний итог</b> (не успел прислать)",
        "Сделано 1 из 3",
        "2. 19:00 · 📌 Купить хлеб",
        "3. 20:00 · 📌 Позвонить маме",
        "",
        "⚠️ <b>Просрочено</b>",
        "4. пн 5 окт 19:00 · 📌 Старое",  # yesterday's own lines are not overdue a second time
    ]
    assert buttons(kb) == [
        ("✅ 1", _cb(1, 1, 11, s="o")),
        ("✅ 2", _cb(2, 5, 51, s="y")),
        ("✅ 3", _cb(3, 6, 61, s="y")),
        ("✅ 4", _cb(4, 3, 31, s="v")),
    ]


def test_morning_digest_re_render_counts_a_closed_yesterday_line_as_done(prefs, at, make_row, check_view, buttons):
    data = _folded_morning(at, make_row, closed=True)
    sections = views.morning_sections(data)
    pinned = [("o", 1, sections["o"][0]), ("y", 2, sections["y"][0]), ("y", 3, sections["y"][1])]
    pinned.append(("v", 4, sections["v"][0]))
    text, kb = check_view(
        views.morning_digest(data, prefs, at(2026, 10, 7, 9, 5), lines=views.pin_lines("m", sections, pinned))
    )
    assert "Сделано 2 из 3" in text
    assert "2. ✅ 19:00 · 📌 <s>Купить хлеб</s>" in lines(text)
    assert ("↩️ 2", _cb(2, 5, 51, s="y", a="undo")) in buttons(kb)


def test_morning_digest_with_a_fully_done_yesterday(prefs, at, make_row, check_view):
    yesterday = _agenda(date(2026, 10, 6), done=[make_row(item_id=7, occurrence_id=71, title="Отчёт")])
    data = DigestData(kind="morning", agenda=_agenda(date(2026, 10, 7)), yesterday=yesterday)
    text, kb = check_view(views.morning_digest(data, prefs, at(2026, 10, 7, 9, 0)))
    assert lines(text)[2:] == [
        "На сегодня ничего не запланировано.",
        "",
        "🌙 <b>Вчерашний итог</b> (не успел прислать)",
        "Всё сделано 🎉",
    ]
    assert kb is None


def test_morning_digest_without_a_folded_evening_is_as_before(prefs, at, make_row):
    agenda = _agenda(date(2026, 10, 7), open=[make_row(item_id=1, occurrence_id=11, title="Спорт")])
    text, _ = views.morning_digest(DigestData(kind="morning", agenda=agenda), prefs, at(2026, 10, 7, 9, 0))
    assert "Вчерашний итог" not in text
    assert views.morning_sections(DigestData(kind="morning", agenda=agenda)).keys() == {"o", "v", "p", "d"}


# --- what the bot heard (§6) -------------------------------------------------------------------------------------


def test_heard_line_is_one_short_escaped_line():
    assert views.heard_line("Купить <наушники>\n в обед") == "🎤 «Купить &lt;наушники&gt; в обед»"
    long = views.heard_line("слово " * 100)
    assert long is not None and long.startswith("🎤 «слово") and long.endswith("…»")
    assert len(long) <= views.HEARD_MAX + 4
    assert views.heard_line(None) is None
    assert views.heard_line("  ") is None


def test_with_heard_puts_the_line_on_top(check_view):
    text, kb = check_view(views.with_heard(("🔎 <b>Вот что нашёл</b>", None), "что у меня завтра"))
    assert lines(text) == ["🎤 «что у меня завтра»", "🔎 <b>Вот что нашёл</b>"]
    assert kb is None
    assert views.with_heard(("x", None), None) == ("x", None)


def test_item_card_shows_what_was_heard(prefs, at, make_item, make_occ, make_reminder, check_view, buttons):
    item = make_item(kind="purchase", title="Купить наушники", tags=["электроника"])
    occ = make_occ(at(2026, 10, 6, 13, 0))
    extras = CardExtras(heard="Купить наушники в обед & <дёшево>")
    text, kb = check_view(
        views.item_card(item, occ, [make_reminder(occ)], prefs, at(2026, 10, 6, 10, 12), extras=extras)
    )
    assert lines(text) == [
        "🛒 <b>Купить наушники</b>",
        "🔔 сегодня 13:00",
        "🏷 Покупка · #электроника",
        "🎤 «Купить наушники в обед &amp; &lt;дёшево&gt;»",
    ]
    assert [t for t, _ in buttons(kb)] == ["✏️ Изменить", "🔕 Без напоминания", "🗑 Удалить"]
    plain, _ = views.item_card(item, occ, [], prefs, at(2026, 10, 6, 10, 12), extras=CardExtras())
    assert "🎤" not in plain


def test_heard_item_for_one_idea(make_item, check_view, buttons):
    idea = make_item(id=42, kind="idea", title="Бот, который <сам> предлагает подзадачи")
    text, kb = check_view(views.heard_item("идея бот который сам предлагает подзадачи", idea))
    assert (
        text
        == "🎤 «идея бот который сам предлагает подзадачи» → 💡 Идея «Бот, который &lt;сам&gt; предлагает подзадачи»"
    )
    assert buttons(kb) == [
        ("✏️ Изменить", ItemCb(a="edit", id=42).pack()),
        ("🗑 Не то — удалить", ItemCb(a="del", id=42).pack()),
    ]


def test_heard_item_with_a_long_transcript_and_big_ids(make_item, check_view, buttons):
    note = make_item(id=10**12 + 1, kind="note", title="Запись 1")
    text, kb = check_view(views.heard_item("ммм " * 80, note))
    [line] = lines(text)
    assert line.startswith("🎤 «ммм ммм") and line.endswith("…» → 📝 Заметка «Запись 1»")
    assert buttons(kb) == [
        ("✏️ Изменить", ItemCb(a="edit", id=10**12 + 1).pack()),
        ("🗑 Не то — удалить", ItemCb(a="del", id=10**12 + 1).pack()),
    ]


def test_help_mentions_downtime_and_voice_and_fits():
    text = views.help_text()
    assert "🎤" in text and "компьютер спал" in text
    assert len(text.encode("utf-16-le")) // 2 <= 4096


def test_quiet_hours_end_is_the_morning(prefs):
    assert prefs.quiet_end == time(8, 0)  # the times the notice above names
