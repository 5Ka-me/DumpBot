"""Rendering of bot messages: HTML escaping, length limits, Russian dates and keyboards."""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from dumpbot.ai.schemas import Ask
from dumpbot.bot import views
from dumpbot.bot.cbdata import AskCb, DigestCb, ItemCb, KindCb, LeadCb, OccCb, PickCb, SetCb
from dumpbot.domain.kinds import KINDS
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import Agenda, DigestData, UsageSummary

BIG = 10**12
NASTY = '<script>alert("x")</script> & <b>bold</b> &amp;'
NASTY_ESCAPED = '&lt;script&gt;alert("x")&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt; &amp;amp;'


def lines(text: str) -> list[str]:
    return text.split("\n")


# --- helpers -----------------------------------------------------------------------------------------------------


def test_esc_and_clip():
    assert views.esc(NASTY) == NASTY_ESCAPED
    assert views.clip("  a\n b  ", 10) == "a b"
    assert views.clip("abcdef", 4) == "abc…"
    assert views.q("<" * 10, 4) == "&lt;&lt;&lt;…"  # clipped before escaping


def test_tg_len_counts_utf16_units():
    assert views.tg_len("abc") == 3
    assert views.tg_len("🔔") == 2
    assert views.tg_len("сделано") == 7


def test_fit_drops_whole_lines(check_view):
    text = "\n".join(f"<b>строка {i}</b> &amp; ещё" for i in range(1000))
    out = views.fit(text)
    check_view(out)
    assert out.endswith("\n…")
    assert views.fit("short") == "short"


def test_fit_single_huge_line_strips_markup_and_entities(check_view):
    out = views.fit("<b>" + "&amp;" * 2000 + "</b>")
    check_view(out)
    assert out.endswith("…") and "<b>" not in out


def test_fit_lines_reports_the_rest():
    out = views.fit_lines([f"line {i}" for i in range(100)], 100)
    assert out[-1].startswith("… и ещё ")
    assert int(out[-1].split()[-1]) == 100 - (len(out) - 1)
    assert views.fit_lines(["a", "b"], 100) == ["a", "b"]


def test_short_url():
    assert views.short_url("https://www.habr.com/ru/articles/123/") == "habr.com/ru/articles/123"
    assert views.short_url("http://example.com") == "example.com"
    assert len(views.short_url("https://example.com/" + "x" * 100)) == views.URL_LABEL_MAX


# --- item card ---------------------------------------------------------------------------------------------------


def test_item_card_purchase_with_reminder(prefs, now, at, make_item, make_occ, make_reminder, check_view, buttons):
    item = make_item(kind="purchase", tags=["электроника", "sony"])
    occ = make_occ(at(2026, 10, 6, 15, 0))
    text, kb = check_view(views.item_card(item, occ, [make_reminder(occ, 5)], prefs, now))
    assert lines(text) == [
        "🛒 <b>Купить наушники</b>",
        "⏰ сегодня 15:00 · напомню в 14:55",
        "🏷 Покупка · #электроника #sony",
    ]
    assert buttons(kb) == [
        ("✅ Готово", "o:done:10"),
        ("✏️ Изменить", "i:edit:1"),
        ("🔕 Без напоминания", "i:norem:1"),
        ("🗑 Удалить", "i:del:1"),
    ]
    assert kb.inline_keyboard[0][0].style == "success"
    assert kb.inline_keyboard[1][-1].style == "danger"


def test_item_card_several_reminders(prefs, now, at, make_item, make_occ, make_reminder, check_view):
    occ = make_occ(at(2026, 10, 9, 15, 0), precision="slot")
    rems = [make_reminder(occ, 60), make_reminder(occ, 1440)]
    text, _ = check_view(views.item_card(make_item(kind="event"), occ, rems, prefs, now))
    assert lines(text)[:2] == ["📅 <b>Купить наушники</b>", "⏰ пт 9 окт 15:00 · напомню за 1 дн и за 1 ч"]


def test_item_card_lead_zero_among_several(prefs, now, at, make_item, make_occ, make_reminder):
    occ = make_occ(at(2026, 10, 7, 9, 30))
    text, _ = views.item_card(make_item(), occ, [make_reminder(occ, 0), make_reminder(occ, 30)], prefs, now)
    assert "⏰ завтра 09:30 · напомню за 30 мин и в 09:30" in text


def test_item_card_reminder_on_another_day(prefs, now, at, make_item, make_occ, make_reminder):
    occ = make_occ(at(2026, 10, 12, 10, 0))
    text, _ = views.item_card(make_item(), occ, [make_reminder(occ, 1440)], prefs, now)
    assert "⏰ пн 12 окт 10:00 · напомню вс 11 окт в 10:00" in text


def test_item_card_reminder_due_now(prefs, now, make_item, make_occ, make_reminder):
    occ = make_occ(now + timedelta(minutes=3))
    text, _ = views.item_card(make_item(), occ, [make_reminder(occ, 5, fire_at=now)], prefs, now)
    assert "· напомню сейчас" in text


def test_item_card_without_pending_reminders(prefs, now, at, make_item, make_occ, make_reminder, buttons):
    occ = make_occ(at(2026, 10, 7, 9, 30))
    sent = make_reminder(occ, 5, status="sent")
    cancelled = make_reminder(occ, 10, status="cancelled")
    text, kb = views.item_card(make_item(), occ, [sent, cancelled], prefs, now)
    assert "⏰ завтра 09:30 · без напоминания" in text
    assert ("🔕 Без напоминания", "i:norem:1") not in buttons(kb)


def test_item_card_overdue(prefs, now, at, make_item, make_occ):
    occ = make_occ(at(2026, 10, 5, 19, 0), precision="default")
    text, _ = views.item_card(make_item(), occ, [], prefs, now)
    assert "⏰ без срока · просрочено" in text
    occ = make_occ(at(2026, 10, 5, 18, 0))
    assert "⏰ вчера 18:00 · просрочено" in views.item_card(make_item(), occ, [], prefs, now)[0]


def test_item_card_default_time(prefs, now, at, make_item, make_occ, make_reminder):
    occ = make_occ(at(2026, 10, 6, 19, 0), precision="default")
    text, _ = views.item_card(make_item(), occ, [make_reminder(occ, 0, auto=True)], prefs, now)
    assert "⏰ без срока · напомню сегодня в 19:00" in text


def test_item_card_date_precision(prefs, now, at, make_item, make_occ, make_reminder):
    occ = make_occ(at(2026, 10, 12, 10, 0), precision="date")
    text, _ = views.item_card(make_item(), occ, [make_reminder(occ, 0, auto=True)], prefs, now)
    assert "⏰ пн 12 окт · напомню в 10:00" in text


def test_item_card_recurring(prefs, now, at, make_item, make_occ, make_reminder, buttons):
    item = make_item(rrule="FREQ=WEEKLY;BYDAY=MO", rec_active=True)
    occ = make_occ(at(2026, 10, 12, 9, 0))
    text, kb = views.item_card(item, occ, [make_reminder(occ, 5)], prefs, now)
    assert "🔁 каждый понедельник" in lines(text)
    assert ("⏹ Стоп повтор", "i:stoprec:1") in buttons(kb)

    item.rec_active = False
    text, kb = views.item_card(item, occ, [], prefs, now)
    assert "🔁 повтор остановлен" in lines(text)
    assert all(cb != "i:stoprec:1" for _, cb in buttons(kb))


def test_item_card_done(prefs, now, at, make_item, make_occ, check_view, buttons):
    occ = make_occ(at(2026, 10, 6, 13, 0), status="done")
    text, kb = check_view(views.item_card(make_item(kind="purchase"), occ, [], prefs, now))
    assert lines(text)[:3] == ["🛒 <b>Купить наушники</b>", "⏰ сегодня 13:00", "✅ сделано"]
    flat = buttons(kb)
    assert flat[0] == ("↩️ Не сделано", "o:undone:10")
    assert all(text != "✅ Готово" for text, _ in flat)


def test_item_card_done_item_without_occurrence(prefs, now, make_item, buttons):
    text, kb = views.item_card(make_item(status="done"), None, [], prefs, now)
    assert "✅ сделано" in text
    assert all(label not in ("✅ Готово", "↩️ Не сделано") for label, _ in buttons(kb))


def test_item_card_actionable_without_occurrence_uses_item_done(prefs, now, make_item, buttons):
    _, kb = views.item_card(make_item(id=BIG), None, [], prefs, now)
    assert buttons(kb)[0] == ("✅ Готово", f"i:done:{BIG}")


def test_item_card_note_has_no_done_button(prefs, now, make_item, check_view, buttons):
    text, kb = check_view(views.item_card(make_item(kind="note", title="Мысль"), None, [], prefs, now))
    assert lines(text) == ["📝 <b>Мысль</b>", "🏷 Заметка"]
    assert buttons(kb) == [("✏️ Изменить", "i:edit:1"), ("🗑 Удалить", "i:del:1")]


def test_item_card_deleted_item_renders_deleted_card(prefs, now, make_item):
    assert views.item_card(make_item(status="deleted"), None, [], prefs, now) == views.deleted_card(
        make_item(status="deleted")
    )


def test_item_card_forward_source_and_link(prefs, now, make_item, check_view):
    item = make_item(
        kind="link",
        title="Статья про asyncio",
        source={"type": "channel", "title": "Хабр", "username": "habr", "url": "https://t.me/habr/42"},
        urls=["https://www.habr.com/ru/articles/123456/", "https://example.com"],
    )
    text, _ = check_view(views.item_card(item, None, [], prefs, now))
    assert '↪️ из <a href="https://t.me/habr/42">«Хабр»</a>' in lines(text)
    assert '🔗 <a href="https://www.habr.com/ru/articles/123456/">habr.com/ru/articles/123456</a> (+1)' in lines(text)


def test_item_card_forward_variants(prefs, now, make_item, check_view):
    nested = make_item(source={"forward_origin": {"type": "user", "sender_name": "Иван <Петров>"}})
    assert "↪️ из «Иван &lt;Петров&gt;»" in check_view(views.item_card(nested, None, [], prefs, now))[0]
    by_username = make_item(source={"forward": {"type": "chat", "username": "chatname"}})
    assert "↪️ из «@chatname»" in views.item_card(by_username, None, [], prefs, now)[0]
    plain = make_item(source={"forward_from": "канал «Хабр»"})
    assert "↪️ из «канал «Хабр»»" in views.item_card(plain, None, [], prefs, now)[0]
    unrelated = make_item(source={"page": "x"})
    assert "↪️" not in views.item_card(unrelated, None, [], prefs, now)[0]


def test_item_card_link_title_and_unsafe_urls(prefs, now, make_item, check_view):
    url = "https://example.com/a?b=1&c=2"
    item = make_item(kind="link", urls=[url], source={"link_titles": {url: "Пример & <тест>"}})
    text, _ = check_view(views.item_card(item, None, [], prefs, now))
    assert '🔗 <a href="https://example.com/a?b=1&amp;c=2">Пример &amp; &lt;тест&gt;</a> · example.com' in text

    js = make_item(kind="link", urls=['javascript:alert("1")'])
    text, _ = check_view(views.item_card(js, None, [], prefs, now))
    assert "<a " not in text and "🔗 javascript:alert(&quot;1&quot;)" not in text
    assert '🔗 javascript:alert("1")' in text

    quoted = make_item(kind="link", urls=['https://e.com/"><b>x'])
    text, _ = check_view(views.item_card(quoted, None, [], prefs, now))
    assert 'href="https://e.com/&quot;&gt;&lt;b&gt;x"' in text


def test_item_card_warnings_and_details(prefs, now, make_item, check_view):
    item = make_item(details="Нужны с шумодавом", needs_confirm=True)
    text, _ = check_view(
        views.item_card(item, None, [], prefs, now, warnings=["дата в прошлом", "дата в прошлом", "a < b"])
    )
    assert lines(text)[-3:] == ["<i>Нужны с шумодавом</i>", "⚠️ дата в прошлом", "⚠️ a &lt; b"]
    text, _ = views.item_card(item, None, [], prefs, now)
    assert lines(text)[-1] == f"⚠️ {views.NEEDS_CONFIRM_HINT}"


def test_item_card_escapes_everything(prefs, now, at, make_item, make_occ, check_view):
    item = make_item(title=NASTY, details=NASTY, tags=["<tag>&"], source={"type": "channel", "title": NASTY})
    text, _ = check_view(views.item_card(item, make_occ(at(2026, 10, 7, 9, 0)), [], prefs, now, warnings=[NASTY]))
    assert "<script>" not in text and "<b>bold" not in text
    assert f"<b>{NASTY_ESCAPED}</b>" in text
    assert "#&lt;tag&gt;&amp;" in text


def test_item_card_huge_content_stays_within_limits(prefs, now, make_item, check_view):
    item = make_item(title="Т" * 10_000, details="<д>" * 5_000, tags=[f"t{i}" * 20 for i in range(50)])
    text, _ = check_view(views.item_card(item, None, [], prefs, now, warnings=["⚠" * 3000] * 20))
    assert views.tg_len(text) < 4096
    assert "…</b>" in text


# --- item menus --------------------------------------------------------------------------------------------------


def test_deleted_card(make_item, check_view, buttons):
    text, kb = check_view(views.deleted_card(make_item(title="A & B", kind="idea")))
    assert text == "🗑 Удалено: 💡 <s>A &amp; B</s>"
    assert buttons(kb) == [("↩️ Вернуть", "i:undel:1")]


def test_edit_menu(make_item, check_view, buttons):
    _, kb = check_view(views.edit_menu(make_item(id=BIG)))
    assert [cb for _, cb in buttons(kb)] == [
        ItemCb(a=a, id=BIG).pack() for a in ("time", "kind", "tags", "title", "lead", "back")
    ]
    assert [label for label, _ in buttons(kb)] == [
        "🕒 Время",
        "🏷 Категория",
        "#️⃣ Теги",
        "✏️ Название",
        "⏱ Напомнить за…",
        "← Назад",
    ]


def test_kind_menu(make_item, check_view, buttons):
    _, kb = check_view(views.kind_menu(make_item(id=BIG, kind="idea")))
    flat = buttons(kb)
    assert [cb for _, cb in flat[:-1]] == [KindCb(id=BIG, k=k).pack() for k in KINDS]
    assert flat[2] == ("✓ 💡 Идея", f"k:{BIG}:idea")
    assert flat[-1] == ("← Назад", f"i:back:{BIG}")


def test_lead_menu(make_item, check_view, buttons):
    text, kb = check_view(views.lead_menu(make_item(id=BIG, lead_minutes=[1440, 60])))
    assert "Сейчас: за 1 дн и за 1 ч" in text
    flat = buttons(kb)
    assert [cb for _, cb in flat[:-1]] == [LeadCb(id=BIG, m=m).pack() for m in (0, 5, 15, 30, 60, 1440)]
    assert ("✓ 1 ч", f"l:{BIG}:60") in flat and ("✓ 1 дн", f"l:{BIG}:1440") in flat
    assert "как в настройках" in views.lead_menu(make_item())[0]


# --- reminders ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "relative"),
    [
        (timedelta(minutes=5), "через 5 мин"),
        (timedelta(seconds=20), "сейчас"),
        (timedelta(0), "сейчас"),
        (timedelta(hours=-2), "просрочено на 2 ч"),
        (timedelta(hours=1, minutes=30), "через 1 ч 30 мин"),
        (timedelta(days=1, hours=2, minutes=5), "через 1 дн 2 ч"),
    ],
)
def test_reminder_message(prefs, at, make_item, make_occ, make_reminder, check_view, buttons, delta, relative):
    due = at(2026, 10, 6, 13, 0)
    occ = make_occ(due, id=BIG)
    text, kb = check_view(views.reminder_message(make_item(), occ, make_reminder(occ), prefs, due - delta))
    first, second = lines(text)[:2]
    assert first == "🔔 <b>Купить наушники</b>"
    assert second.startswith(f"{relative} · ")
    assert buttons(kb) == [
        ("✅ Готово", f"o:done:{BIG}"),
        ("⏰ 15 мин", f"o:s15:{BIG}"),
        ("⏰ 1 час", f"o:s60:{BIG}"),
        ("📅 Завтра", f"o:tmr:{BIG}"),
    ]


def test_reminder_message_absolute_time_and_extras(prefs, now, at, make_item, make_occ, check_view):
    item = make_item(rrule="FREQ=DAILY", rec_active=True, details="<взять паспорт>")
    occ = make_occ(at(2026, 10, 6, 14, 10))
    text, _ = check_view(views.reminder_message(item, occ, None, prefs, now))
    assert lines(text) == [
        "🔔 <b>Купить наушники</b>",
        "через 5 мин · сегодня 14:10",
        "🔁 каждый день",
        "<i>&lt;взять паспорт&gt;</i>",
    ]


def test_reminder_done_and_undone(prefs, now, at, make_item, make_occ, check_view, buttons):
    occ = make_occ(at(2026, 10, 6, 13, 0), status="done")
    text, kb = check_view(views.reminder_done(make_item(title="A<B"), occ, prefs, now))
    assert lines(text) == ["✅ <s>A&lt;B</s>", "<i>сегодня 13:00 · сделано</i>"]
    assert buttons(kb) == [("↩️ Не сделано", "o:undone:10")]
    assert views.done_text is views.reminder_done


def test_reminder_snoozed(prefs, now, make_item, make_occ, make_reminder, check_view, buttons):
    occ = make_occ(now)
    snoozed = make_reminder(occ, 0, fire_at=now + timedelta(minutes=15))
    text, kb = check_view(views.reminder_snoozed(make_item(), occ, snoozed, prefs, now))
    assert lines(text)[1] == "💤 отложено · напомню сегодня в 14:20"
    assert buttons(kb) == [("✅ Готово", "o:done:10")]
    tomorrow = make_reminder(occ, 0, fire_at=now + timedelta(days=1))
    assert "напомню завтра в 14:05" in views.reminder_snoozed(make_item(), occ, tomorrow, prefs, now)[0]


# --- lists -------------------------------------------------------------------------------------------------------


def test_rows_list_russian_dates(prefs, now, at, make_row, check_view):
    rows = [
        make_row(kind="purchase", title="Купить наушники", due_at=at(2026, 10, 6, 13, 0), occ_status="open"),
        make_row(title="Сдать отчёт", due_at=at(2026, 10, 7, 9, 0), occ_status="done", status="done"),
        make_row(title="Планёрка", due_at=at(2026, 10, 9, 15, 0), rrule="FREQ=WEEKLY;BYDAY=FR"),
        make_row(title="Вчерашнее", due_at=at(2026, 10, 5, 18, 0)),
        make_row(title="Через год", due_at=at(2027, 10, 9, 15, 0)),
        make_row(kind="idea", title="Идея <тест>"),
    ]
    text, kb = check_view(views.rows_list("📋 Задачи <все>", rows, prefs, now))
    assert kb is None
    assert lines(text) == [
        "<b>📋 Задачи &lt;все&gt;</b>",
        "• сегодня 13:00 · 🛒 Купить наушники",
        "• завтра 09:00 · 📌 <s>Сдать отчёт</s>",
        "• пт 9 окт 15:00 · 📌 Планёрка 🔁",
        "• вчера 18:00 · 📌 Вчерашнее",
        "• 9 окт 2027 15:00 · 📌 Через год",
        "• 💡 Идея &lt;тест&gt;",
    ]


def test_rows_list_empty(prefs, now, check_view):
    assert check_view(views.rows_list("Сегодня", [], prefs, now)) == ("<b>Сегодня</b>\nПусто", None)
    assert views.rows_list("Поиск", [], prefs, now, empty="Ничего не нашёл <:(>")[0].endswith("&lt;:(&gt;")


def test_rows_list_truncates(prefs, now, make_row, check_view):
    rows = [make_row(title=f"{i} " + "очень длинный заголовок & " * 10, due_at=now) for i in range(500)]
    text, _ = check_view(views.rows_list("Все", rows, prefs, now))
    assert re.search(r"\n… и ещё \d+$", text)
    shown = len(lines(text)) - 2
    assert lines(text)[-1] == f"… и ещё {500 - shown}"


def test_edit_candidates(prefs, now, at, make_row, check_view, buttons):
    rows = [make_row(item_id=BIG + i, title=f"Встреча {i}", due_at=at(2026, 10, 7, 10 + i, 0)) for i in range(7)]
    text, kb = check_view(views.edit_candidates(rows, BIG, prefs, now))
    assert lines(text)[:3] == [
        "✏️ Какую запись изменить?",
        "1. завтра 10:00 · 📌 Встреча 0",
        "2. завтра 11:00 · 📌 Встреча 1",
    ]
    assert len(lines(text)) == 6
    assert buttons(kb) == [(str(i + 1), PickCb(raw=BIG, item=BIG + i).pack()) for i in range(5)]


def test_ask_message(check_view, buttons):
    ask = Ask(q="В 8 утра или <вечера>?", options=["утра", "вечера " * 20])
    text, kb = check_view(views.ask_message(ask, BIG))
    assert text == "🤔 В 8 утра или &lt;вечера&gt;?"
    flat = buttons(kb)
    assert [cb for _, cb in flat] == [AskCb(raw=BIG, n=0).pack(), AskCb(raw=BIG, n=1).pack()]
    assert flat[0][0] == "утра" and len(flat[1][0]) <= 64
    assert all(len(row) == 1 for row in kb.inline_keyboard)


# --- digests -----------------------------------------------------------------------------------------------------


def _agenda(**kw) -> Agenda:
    values = {"day": date(2026, 10, 9), "open": [], "overdue": [], "done": []}
    values.update(kw)
    return Agenda(**values)


def test_morning_digest(prefs, at, make_row, check_view):
    now = at(2026, 10, 9, 9, 0)
    agenda = _agenda(
        open=[
            make_row(title="Позвонить маме", due_at=at(2026, 10, 9, 9, 30)),
            make_row(kind="purchase", title="Купить <хлеб>", due_at=at(2026, 10, 9, 19, 0), rrule="FREQ=DAILY"),
        ],
        overdue=[make_row(title="Сдать отчёт", due_at=at(2026, 10, 8, 19, 0))],
    )
    data = DigestData(kind="morning", agenda=agenda, recurring_today=1)
    text, kb = check_view(views.morning_digest(data, prefs, now))
    assert kb is None
    assert lines(text) == [
        "☀️ <b>Доброе утро!</b> План на сегодня, пт 9 окт",
        "",
        "1. 09:30 · 📌 Позвонить маме",
        "2. 19:00 · 🛒 Купить &lt;хлеб&gt; 🔁",
        "",
        "⚠️ <b>Просрочено</b>",
        "• вчера 19:00 · 📌 Сдать отчёт",
        "",
        "🔁 Повторяющихся сегодня: 1",
    ]


def test_morning_digest_only_overdue(prefs, at, make_row, check_view):
    now = at(2026, 10, 9, 9, 0)
    agenda = _agenda(overdue=[make_row(title="Старое", due_at=at(2026, 10, 1, 19, 0))])
    text, _ = check_view(views.morning_digest(DigestData(kind="morning", agenda=agenda), prefs, now))
    assert "На сегодня ничего не запланировано." in text
    assert "• чт 1 окт 19:00 · 📌 Старое" in text
    assert "Повторяющихся" not in text


def test_evening_digest(prefs, at, make_row, check_view, buttons):
    now = at(2026, 10, 9, 21, 0)
    agenda = _agenda(
        done=[make_row(title="Купить наушники", kind="purchase", occ_status="done", due_at=at(2026, 10, 9, 13, 0))],
        open=[make_row(title="Позвонить маме", due_at=at(2026, 10, 9, 19, 0))],
        overdue=[make_row(title="Сдать отчёт", due_at=at(2026, 10, 7, 19, 0))],
    )
    text, kb = check_view(views.evening_digest(DigestData(kind="evening", agenda=agenda), prefs, now))
    assert lines(text) == [
        "🌙 <b>Итоги дня</b>, пт 9 окт",
        "Сделано 1 из 2",
        "",
        "<b>Сделано</b>",
        "✅ 🛒 <s>Купить наушники</s>",
        "",
        "<b>Не сделано</b>",
        "⬜ 19:00 · 📌 Позвонить маме",
        "",
        "<b>Просрочено</b>",
        "⬜ ср 7 окт 19:00 · 📌 Сдать отчёт",
    ]
    assert buttons(kb) == [("📅 Перенести невыполненное на завтра", DigestCb(a="tmr").pack())]


def test_evening_digest_all_done_has_no_button(prefs, at, make_row, check_view):
    now = at(2026, 10, 9, 21, 0)
    agenda = _agenda(done=[make_row(title="Готово", occ_status="done", due_at=at(2026, 10, 9, 13, 0))])
    text, kb = check_view(views.evening_digest(DigestData(kind="evening", agenda=agenda), prefs, now))
    assert lines(text)[1] == "Всё сделано 🎉"
    assert kb is None


def test_evening_digest_empty(prefs, at, check_view):
    text, kb = check_view(views.evening_digest(DigestData(kind="evening", agenda=_agenda()), prefs, at(2026, 10, 9)))
    assert text == "🌙 <b>Итоги дня</b>, пт 9 окт\nСегодня задач не было."
    assert kb is None


def test_digests_truncate(prefs, at, make_row, check_view):
    now = at(2026, 10, 9, 9, 0)
    many = [make_row(title="задача & " * 12, due_at=at(2026, 10, 9, 12, 0)) for _ in range(300)]
    old = [make_row(title="старое & " * 12, due_at=at(2026, 10, 1, 12, 0)) for _ in range(300)]
    data = DigestData(kind="morning", agenda=_agenda(open=many, overdue=old, done=many), recurring_today=3)
    text, _ = check_view(views.morning_digest(data, prefs, now))
    assert "… и ещё" in text and text.endswith("🔁 Повторяющихся сегодня: 3")
    text, kb = check_view(views.evening_digest(data, prefs, now))
    assert "… и ещё" in text and kb is not None


# --- usage / help ------------------------------------------------------------------------------------------------


def test_usage_text(check_view):
    summary = UsageSummary(
        month="октябрь 2026",
        spent_usd=0.42,
        limit_usd=5.0,
        calls=123,
        by_purpose={"stt": (23, 0.12), "extract": (100, 0.30), "<new>": (1, 0.001)},
    )
    text, _ = check_view(views.usage_text(summary))
    assert lines(text)[:5] == [
        "💰 <b>Расходы на ИИ</b> · октябрь 2026",
        "$0.42 из $5.00 (8%)",
        "▓░░░░░░░░░",
        "Вызовов: 123",
        "• разбор сообщений — 100 · $0.30",
    ]
    assert "• голосовые — 23 · $0.12" in text
    assert "• &lt;new&gt; — 1 · $0.0010" in text


@pytest.mark.parametrize(
    ("spent", "limit", "expected"),
    [(4.2, 5.0, "⚠️ Израсходовано больше 80%"), (5.0, 5.0, "⛔ Бюджет исчерпан"), (0.1, 0.0, "бюджет $0")],
)
def test_usage_text_budget_states(check_view, spent, limit, expected):
    summary = UsageSummary(month="октябрь 2026", spent_usd=spent, limit_usd=limit, calls=1, by_purpose={})
    assert expected in check_view(views.usage_text(summary))[0]


def test_help_text_lists_all_commands(check_view):
    text, _ = check_view(views.help_text())
    for command in ("/today", "/tasks", "/recent", "/search", "/ask", "/recurring", "/settings", "/usage", "/help"):
        assert f"\n{command} " in text
    assert "«?»" in text and "reply" in text


# --- settings ----------------------------------------------------------------------------------------------------


def test_settings_menu_shows_all_values(check_view, buttons):
    prefs = Prefs(
        tz="Europe/Berlin",
        task_default_time="18:00",
        lead_minutes=10,
        quiet_enabled=False,
        digest_evening_enabled=False,
        budget_usd=7.5,
        stt_language="en",
        slot_lunch="12:30",
    )
    text, kb = check_view(views.settings_menu(prefs))
    for expected in (
        "Часовой пояс: Europe/Berlin",
        "Задача без времени: 18:00",
        "Напоминать: за 10 мин",
        "обед 12:30",
        "Тихие часы: 23:00–08:00 (выкл)",
        "утро 09:00 (вкл), вечер 21:00 (выкл)",
        "Бюджет: $7.50 в месяц",
        "Язык голосовых: English",
        "новых напоминаний",
    ):
        assert expected in text
    assert [cb for _, cb in buttons(kb)] == [
        "s:menu:tz",
        "s:ask:task_default_time",
        "s:menu:lead",
        "s:menu:slots",
        "s:menu:quiet",
        "s:menu:digest",
        "s:menu:budget",
        "s:menu:stt",
        "s:close:",
    ]


@pytest.mark.parametrize("name", sorted(views.SETTINGS_MENUS))
def test_settings_submenus(name, prefs, check_view, buttons):
    _, kb = check_view(views.settings_view(name, prefs))
    flat = buttons(kb)
    assert flat
    if name != "main":
        assert flat[-1] == ("← Назад", "s:menu:main")


def test_settings_view_unknown_name_falls_back_to_main(prefs):
    assert views.settings_view("nope", prefs) == views.settings_menu(prefs)


def test_settings_marks_current_values(check_view, buttons):
    prefs = Prefs(tz="Europe/Kyiv", lead_minutes=15, stt_language="ru", budget_usd=10, quiet_enabled=False)
    assert ("✓ Europe/Kyiv", "s:tz:Europe/Kyiv") in buttons(views.settings_tz_menu(prefs)[1])
    assert ("✓ 15 мин", "s:lead:15") in buttons(views.settings_lead_menu(prefs)[1])
    assert ("✓ Русский", "s:stt:ru") in buttons(views.settings_stt_menu(prefs)[1])
    assert ("✓ $10", "s:budget:10") in buttons(views.settings_budget_menu(prefs)[1])
    assert ("🔕 Включить тихие часы", "s:quiet_toggle:") in buttons(views.settings_quiet_menu(prefs)[1])
    auto = Prefs()
    assert ("✓ Авто", "s:stt:auto") in buttons(views.settings_stt_menu(auto)[1])
    assert ("✓ 5 мин", "s:lead:5") not in buttons(views.settings_lead_menu(prefs)[1])


def test_settings_digest_toggles(check_view, buttons):
    prefs = Prefs(digest_morning_enabled=False)
    text, kb = check_view(views.settings_digest_menu(prefs))
    assert "Утренний (план на день): 09:00, выкл" in text
    flat = buttons(kb)
    assert ("☀️ Включить утренний", "s:digest_toggle:morning") in flat
    assert ("🌙 Выключить вечерний", "s:digest_toggle:evening") in flat


def test_settings_slots_menu_asks_every_time_field(prefs, buttons):
    asked = {SetCb.unpack(cb).v for _, cb in buttons(views.settings_slots_menu(prefs)[1]) if cb.startswith("s:ask:")}
    assert asked == {
        "slot_morning",
        "slot_lunch",
        "slot_afternoon",
        "slot_evening",
        "slot_after_work",
        "slot_night",
        "weekend_time",
        "bare_date_time",
        "task_default_time",
    }


@pytest.mark.parametrize("field", sorted(views.ASK_FIELDS))
def test_settings_ask_prompts(field, prefs, check_view, buttons):
    assert hasattr(prefs, field)
    text, kb = check_view(views.settings_ask(field, prefs))
    assert text.startswith("✍️ Введи значение")
    assert "Сейчас: " in text
    assert buttons(kb) == [("✖️ Отмена", SetCb(key="menu", v=views.ASK_FIELDS[field].menu).pack())]


def test_settings_ask_unknown_field():
    with pytest.raises(KeyError):
        views.settings_ask("owner_id", Prefs())


def test_every_emitted_set_callback_is_documented(buttons):
    """The settings router is written separately: every SetCb key/value must be in the module docstring."""
    variants = [
        Prefs(),
        Prefs(quiet_enabled=False, digest_morning_enabled=False, digest_evening_enabled=False, stt_language="en"),
    ]
    emitted: set[tuple[str, str]] = set()
    for prefs in variants:
        views_to_check = [fn(prefs) for fn in views.SETTINGS_MENUS.values()]
        views_to_check += [views.settings_ask(f, prefs) for f in views.ASK_FIELDS]
        for _, kb in views_to_check:
            emitted |= {(cb.key, cb.v) for cb in (SetCb.unpack(data) for _, data in buttons(kb))}
    doc = views.__doc__ or ""
    for key, value in emitted:
        assert f"``{key}``" in doc, key
        if key == "ask":
            assert value in views.ASK_FIELDS
        elif key == "menu":
            assert value in views.SETTINGS_MENUS and f"``{value}``" in doc
        elif key == "tz":
            assert value in views.TZ_CHOICES
        elif value:
            assert f"``{value}``" in doc, (key, value)
    assert {k for k, _ in emitted} == {
        "menu",
        "close",
        "tz",
        "lead",
        "quiet_toggle",
        "digest_toggle",
        "stt",
        "budget",
        "ask",
    }
    assert set(views.SETTINGS_PARENT) <= {k for k, _ in emitted}


# --- all builders ------------------------------------------------------------------------------------------------


def test_all_builders_with_huge_ids(prefs, now, make_item, make_occ, make_reminder, make_row, check_view):
    item = make_item(id=BIG, kind="event", rrule="FREQ=DAILY", rec_active=True, lead_minutes=[5])
    occ = make_occ(now + timedelta(hours=1), id=BIG, item_id=BIG)
    rem = make_reminder(occ, 5, id=BIG)
    row = make_row(item_id=BIG, occurrence_id=BIG, due_at=now)
    data = DigestData(kind="morning", agenda=_agenda(open=[row], overdue=[row], done=[row]), recurring_today=1)
    built = [
        views.item_card(item, occ, [rem], prefs, now),
        views.item_card(item, None, [], prefs, now),
        views.deleted_card(item),
        views.edit_menu(item),
        views.kind_menu(item),
        views.lead_menu(item),
        views.reminder_message(item, occ, rem, prefs, now),
        views.reminder_done(item, occ, prefs, now),
        views.reminder_snoozed(item, occ, rem, prefs, now),
        views.morning_digest(data, prefs, now),
        views.evening_digest(data, prefs, now),
        views.rows_list("x", [row], prefs, now),
        views.ask_message(Ask(q="?", options=["a", "b", "c", "d"]), BIG),
        views.edit_candidates([row] * 5, BIG, prefs, now),
        views.settings_menu(prefs),
        views.usage_text(UsageSummary(month="м", spent_usd=0, limit_usd=5, calls=0, by_purpose={})),
        views.help_text(),
    ]
    for view in built:
        check_view(view)
    occ_cbs = {OccCb(a=a, id=BIG).pack() for a in ("done", "s15", "s60", "tmr")}
    reminder_kb = built[6][1]
    assert {b.callback_data for row in reminder_kb.inline_keyboard for b in row} == occ_cbs
