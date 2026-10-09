"""Rendering of bot messages: HTML escaping, length limits, Russian dates and keyboards."""

from __future__ import annotations

import re
from datetime import date, time, timedelta

import pytest

from dumpbot.ai.schemas import Ask
from dumpbot.bot import views
from dumpbot.bot.cbdata import AskCb, DigestCb, ItemCb, KindCb, ListCb, OccCb, PickCb, SetCb
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
    text, kb = check_view(views.item_card(item, occ, [make_reminder(occ)], prefs, now))
    # The item's time is its notification time (2026-10-07): one line, no "напомню в 14:55".
    assert lines(text) == [
        "🛒 <b>Купить наушники</b>",
        "🔔 сегодня 15:00",
        "🏷 Покупка · #электроника #sony",
    ]
    # no [✅ Готово] on the "saved" card: it is on reminders and in the edit menu
    assert buttons(kb) == [
        ("✏️ Изменить", "i:edit:1"),
        ("🔕 Без напоминания", "i:norem:1"),
        ("🗑 Удалить", "i:del:1"),
    ]
    assert kb.inline_keyboard[0][-1].style == "danger"


def test_item_card_lunch_notifies_at_13(prefs, now, at, make_item, make_occ, make_reminder):
    occ = make_occ(at(2026, 10, 6, 13, 0), precision="slot")
    text, _ = views.item_card(make_item(), occ, [make_reminder(occ)], prefs, at(2026, 10, 6, 10, 0))
    assert "🔔 сегодня 13:00" in lines(text)
    assert "12:55" not in text and "напомню" not in text


def test_item_card_several_notification_times(prefs, now, at, make_item, make_occ, make_reminder, check_view):
    # "за день и за час до встречи в пн 10:00": the day before (Sun 10:00), then the item's time Mon 09:00.
    occ = make_occ(at(2026, 10, 12, 9, 0), precision="exact")
    rems = [make_reminder(occ, 23 * 60), make_reminder(occ, 0, id=2)]
    text, _ = check_view(views.item_card(make_item(kind="event"), occ, rems, prefs, now))
    assert lines(text)[:2] == ["📅 <b>Купить наушники</b>", "🔔 вс 11 окт 10:00, пн 12 окт 09:00"]


@pytest.mark.parametrize(
    ("fires", "expected"),
    [
        # "за час и за 15 минут до 10:30 завтра": the day once, its times joined
        ([(7, 9, 30), (7, 10, 15)], "🔔 завтра 09:30 и 10:15"),
        ([(6, 13, 0), (6, 14, 0), (6, 14, 45)], "🔔 сегодня 13:00, 14:00 и 14:45"),
        ([(8, 10, 30), (9, 9, 30), (9, 10, 15)], "🔔 чт 8 окт 10:30, пт 9 окт 09:30 и 10:15"),
    ],
)
def test_item_card_groups_notification_times_by_day(
    prefs, now, at, make_item, make_occ, make_reminder, fires, expected
):
    occ = make_occ(at(2026, 10, *fires[-1]), precision="exact")
    rems = [make_reminder(occ, 0, id=i, fire_at=at(2026, 10, *f)) for i, f in enumerate(fires, start=1)]
    text, _ = views.item_card(make_item(kind="event"), occ, rems, prefs, now)
    assert lines(text)[1] == expected
    assert views.time_text(make_item(kind="event"), occ, rems, prefs, now) == expected


def test_deadline_notifications_on_one_day_are_grouped_too(prefs, now, at, make_item, make_occ, make_reminder):
    # "до завтра 19:00, напомни за час": 18:00 and the evening before at 19:00 on different days, two on one day here
    item = make_item(when_spec={"role": "deadline"})
    occ = make_occ(at(2026, 10, 7, 19, 0), precision="exact")
    rems = [make_reminder(occ, 60, id=1), make_reminder(occ, 0, id=2, fire_at=at(2026, 10, 7, 19, 0))]
    assert views.time_text(item, occ, rems, prefs, now) == "⏳ до ср 7 окт 19:00 · 🔔 завтра 18:00 и 19:00"


def test_item_card_shows_the_reminder_times_themselves(prefs, now, at, make_item, make_occ, make_reminder):
    occ = make_occ(now + timedelta(minutes=3))
    text, _ = views.item_card(make_item(), occ, [make_reminder(occ, 3, fire_at=now)], prefs, now)
    assert "🔔 сегодня 14:05" in lines(text)


def test_item_card_without_pending_reminders(prefs, now, at, make_item, make_occ, make_reminder, buttons):
    occ = make_occ(at(2026, 10, 7, 9, 30))
    sent = make_reminder(occ, 5, status="sent")
    cancelled = make_reminder(occ, 10, status="cancelled")
    text, kb = views.item_card(make_item(), occ, [sent, cancelled], prefs, now)
    assert "🔕 завтра 09:30" in lines(text)
    assert ("🔕 Без напоминания", "i:norem:1") not in buttons(kb)


def test_item_card_default_time_without_reminder(prefs, now, at, make_item, make_occ):
    occ = make_occ(at(2026, 10, 6, 19, 0), precision="default")
    assert "🔕 без напоминания" in lines(views.item_card(make_item(), occ, [], prefs, now)[0])


def test_item_card_overdue(prefs, now, at, make_item, make_occ):
    occ = make_occ(at(2026, 10, 5, 19, 0), precision="default")
    text, _ = views.item_card(make_item(), occ, [], prefs, now)
    assert "⏰ вчера 19:00 · просрочено" in lines(text)
    occ = make_occ(at(2026, 10, 5, 18, 0))
    assert "⏰ вчера 18:00 · просрочено" in lines(views.item_card(make_item(), occ, [], prefs, now)[0])


def test_item_card_default_time(prefs, now, at, make_item, make_occ, make_reminder):
    occ = make_occ(at(2026, 10, 6, 19, 0), precision="default")
    text, _ = views.item_card(make_item(), occ, [make_reminder(occ, 0, auto=True)], prefs, now)
    assert "🔔 сегодня 19:00" in lines(text)


def test_item_card_cancelled_default_time_shows_no_time(prefs, now, at, make_item, make_occ):
    # A task without a time turned into an idea: its 19:00 default occurrence is cancelled.
    occ = make_occ(at(2026, 10, 6, 19, 0), precision="default", status="cancelled")
    text, _ = views.item_card(make_item(kind="idea"), occ, [], prefs, now)
    assert "⏰" not in text and "🔔" not in text and "🔕" not in text


def test_item_card_date_precision(prefs, now, at, make_item, make_occ, make_reminder):
    occ = make_occ(at(2026, 10, 12, 10, 0), precision="date")
    text, _ = views.item_card(make_item(), occ, [make_reminder(occ, 0, auto=True)], prefs, now)
    assert "🔔 пн 12 окт 10:00" in lines(text)
    done = make_occ(at(2026, 10, 12, 10, 0), precision="date", status="done")
    assert "⏰ пн 12 окт" in lines(views.item_card(make_item(), done, [], prefs, now)[0])


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


def test_item_card_has_no_done_button(prefs, now, at, make_item, make_occ, buttons):
    occ = make_occ(at(2026, 10, 6, 15, 0))
    for o in (occ, None):
        _, kb = views.item_card(make_item(id=BIG), o, [], prefs, now)
        assert all(label != "✅ Готово" for label, _ in buttons(kb))


def test_edit_menu_has_done_for_open_actionable(make_item, buttons):
    _, kb = views.edit_menu(make_item(id=BIG))
    assert buttons(kb)[0] == ("✅ Готово", f"i:done:{BIG}")
    assert kb.inline_keyboard[0][0].style == "success"
    for item in (make_item(status="done"), make_item(kind="note", title="Мысль")):
        _, kb = views.edit_menu(item)
        assert all(label != "✅ Готово" for label, _ in buttons(kb))


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
        ItemCb(a=a, id=BIG).pack() for a in ("done", "time", "kind", "tags", "title", "back")
    ]
    assert [label for label, _ in buttons(kb)] == [
        "✅ Готово",
        "🕒 Время",
        "🏷 Категория",
        "#️⃣ Теги",
        "✏️ Название",
        "← Назад",
    ]


def test_kind_menu(make_item, check_view, buttons):
    _, kb = check_view(views.kind_menu(make_item(id=BIG, kind="idea")))
    flat = buttons(kb)
    assert [cb for _, cb in flat[:-1]] == [KindCb(id=BIG, k=k).pack() for k in KINDS]
    assert flat[2] == ("✓ 💡 Идея", f"k:{BIG}:idea")
    assert flat[-1] == ("← Назад", f"i:back:{BIG}")


def test_no_lead_menu_any_more(make_item, buttons):
    # "⏱ Напомнить за…" is gone (2026-10-07): "напомни за час" is a reply to the card.
    assert not hasattr(views, "lead_menu")
    text, kb = views.edit_menu(make_item(id=BIG))
    assert "Напомнить за" not in text and all("⏱" not in label for label, _ in buttons(kb))
    assert "напомни за час" in text


# --- reminders ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(minutes=5), timedelta(minutes=-2), timedelta(days=1)])
def test_reminder_message(prefs, at, make_item, make_occ, make_reminder, check_view, buttons, delta):
    # The reminder comes at the item's time: just the title, no "через 5 мин".
    due = at(2026, 10, 6, 13, 0)
    occ = make_occ(due, id=BIG)
    text, kb = check_view(views.reminder_message(make_item(), occ, make_reminder(occ), prefs, due - delta))
    assert lines(text) == ["🔔 <b>Купить наушники</b>"]
    # 13:00: the evening (19:00) is more than an hour away -> [🌙 Вечером] instead of [⏰ 15 мин]
    assert buttons(kb) == [
        ("✅ Готово", f"o:done:{BIG}"),
        ("⏰ 1 час", f"o:s60:{BIG}"),
        ("🌙 Вечером", f"o:eve:{BIG}"),
        ("📅 Завтра", f"o:tmr:{BIG}"),
    ]
    assert [len(row) for row in kb.inline_keyboard] == [1, 3]


@pytest.mark.parametrize(
    ("hour", "minute", "evening"),
    [(0, 30, True), (10, 0, True), (17, 59, True), (18, 0, False), (18, 30, False), (19, 0, False), (23, 0, False)],
)
def test_reminder_offers_the_evening_until_an_hour_before_it(
    prefs, at, make_item, make_occ, make_reminder, buttons, hour, minute, evening
):
    now = at(2026, 10, 6, hour, minute)
    occ = make_occ(now, id=BIG)
    _, kb = views.reminder_message(make_item(), occ, make_reminder(occ), prefs, now)
    labels = [label for label, _ in buttons(kb)]
    if evening:
        assert labels == ["✅ Готово", "⏰ 1 час", "🌙 Вечером", "📅 Завтра"]
    else:
        assert labels == ["✅ Готово", "⏰ 15 мин", "⏰ 1 час", "📅 Завтра"]
    assert views.offers_evening(prefs, now) is evening


def test_reminder_evening_follows_the_evening_slot(prefs, at, make_item, make_occ, buttons):
    late_evening = Prefs(slot_evening=time(21, 0))
    now = at(2026, 10, 6, 19, 30)  # 19:30: an hour and a half before a 21:00 evening
    occ = make_occ(now)
    assert "🌙 Вечером" in [
        label for label, _ in buttons(views.reminder_message(make_item(), occ, None, late_evening, now)[1])
    ]
    assert "🌙 Вечером" not in [
        label for label, _ in buttons(views.reminder_message(make_item(), occ, None, prefs, now)[1])
    ]


@pytest.mark.parametrize(
    ("late", "expected"),
    [
        (timedelta(minutes=3), "⏰ сегодня 13:00 · опоздал на 3 мин"),
        (timedelta(hours=2), "⏰ сегодня 13:00 · опоздал на 2 ч"),
        (timedelta(hours=5, minutes=10), "⏰ сегодня 13:00 · опоздал на 5 ч 10 мин"),
        (timedelta(hours=21), "⏰ вчера 13:00 · опоздал на 21 ч"),
    ],
)
def test_a_late_reminder_says_when_it_was_for(
    prefs, at, make_item, make_occ, make_reminder, check_view, late, expected
):
    # The PC slept from 12:30: the 13:00 reminder of "Созвон" arrives later — it must not read as "now".
    occ = make_occ(at(2026, 10, 6, 13, 0))
    text, _ = check_view(views.reminder_message(make_item(), occ, make_reminder(occ), prefs, occ.due_at + late))
    assert lines(text) == ["🔔 <b>Купить наушники</b>", expected]
    # re-rendered without the reminder (after "↩️ Не сделано"): no such line
    text, _ = views.reminder_message(make_item(), occ, None, prefs, occ.due_at + late)
    assert lines(text) == ["🔔 <b>Купить наушники</b>"]


def test_reminder_message_absolute_time_and_extras(prefs, now, at, make_item, make_occ, check_view):
    item = make_item(rrule="FREQ=DAILY", rec_active=True, details="<взять паспорт>")
    occ = make_occ(at(2026, 10, 6, 14, 10))
    text, _ = check_view(views.reminder_message(item, occ, None, prefs, now))
    assert lines(text) == [
        "🔔 <b>Купить наушники</b>",
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
    text, kb = check_view(views.reminder_snoozed(make_item(), snoozed, prefs, now))
    assert lines(text) == ["🔔 <b>Купить наушники</b>", "💤 отложено · напомню сегодня в 14:20"]
    assert kb is None and buttons(kb) == []  # no [✅ Готово]: the snoozed reminder comes with its own buttons
    tomorrow = make_reminder(occ, 0, fire_at=now + timedelta(days=1))
    assert views.reminder_snoozed(make_item(), tomorrow, prefs, now) == (
        "🔔 <b>Купить наушники</b>\n💤 отложено · напомню завтра в 14:05",
        None,
    )


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


def _done_cb(n: int, item: int, occ: int | None = None, *, kind: str = "m", s: str = "o", d: str = "20261009") -> str:
    return ListCb(a="done", k=kind, d=d, s=s, n=n, o=occ or 0, i=item).pack()


def test_morning_digest(prefs, at, make_row, check_view, buttons):
    now = at(2026, 10, 9, 9, 0)
    agenda = _agenda(
        open=[
            make_row(item_id=1, occurrence_id=11, title="Позвонить маме", due_at=at(2026, 10, 9, 9, 30)),
            make_row(
                item_id=2,
                occurrence_id=12,
                kind="purchase",
                title="Купить <хлеб>",
                due_at=at(2026, 10, 9, 19, 0),
                rrule="FREQ=DAILY",
            ),
        ],
        overdue=[make_row(item_id=3, occurrence_id=13, title="Сдать отчёт", due_at=at(2026, 10, 8, 19, 0))],
    )
    data = DigestData(kind="morning", agenda=agenda, recurring_today=1)
    text, kb = check_view(views.morning_digest(data, prefs, now))
    assert lines(text) == [
        "☀️ <b>Доброе утро!</b> План на сегодня, пт 9 окт",
        "",
        "1. 09:30 · 📌 Позвонить маме",
        "2. 19:00 · 🛒 Купить &lt;хлеб&gt; 🔁",
        "",
        "⚠️ <b>Просрочено</b>",
        "3. вчера 19:00 · 📌 Сдать отчёт",
        "",
        "🔁 Повторяющихся сегодня: 1",
    ]
    # the overdue line is numbered too; the button knows its list, day, section, line and occurrence
    assert buttons(kb) == [
        ("✅ 1", _done_cb(1, 1, 11)),
        ("✅ 2", _done_cb(2, 2, 12)),
        ("✅ 3", _done_cb(3, 3, 13, s="v")),
    ]


def test_morning_digest_only_overdue(prefs, at, make_row, check_view):
    now = at(2026, 10, 9, 9, 0)
    agenda = _agenda(overdue=[make_row(title="Старое", due_at=at(2026, 10, 1, 19, 0))])
    text, _ = check_view(views.morning_digest(DigestData(kind="morning", agenda=agenda), prefs, now))
    assert "На сегодня ничего не запланировано." in text
    assert "1. чт 1 окт 19:00 · 📌 Старое" in text
    assert "Повторяющихся" not in text


def test_evening_digest(prefs, at, make_row, check_view, buttons):
    now = at(2026, 10, 9, 21, 0)
    agenda = _agenda(
        done=[make_row(title="Купить наушники", kind="purchase", occ_status="done", due_at=at(2026, 10, 9, 13, 0))],
        open=[make_row(item_id=2, occurrence_id=22, title="Позвонить маме", due_at=at(2026, 10, 9, 19, 0))],
        overdue=[make_row(item_id=3, occurrence_id=23, title="Сдать отчёт", due_at=at(2026, 10, 7, 19, 0))],
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
        "1. ⬜ 19:00 · 📌 Позвонить маме",
        "",
        "<b>Просрочено</b>",
        "2. ⬜ ср 7 окт 19:00 · 📌 Сдать отчёт",
    ]
    assert buttons(kb) == [
        ("✅ 1", _done_cb(1, 2, 22, kind="e", s="m")),
        ("✅ 2", _done_cb(2, 3, 23, kind="e", s="v")),
        ("📅 Перенести невыполненное на завтра", DigestCb(a="tmr", d="20261009").pack()),
    ]
    assert [len(row) for row in kb.inline_keyboard] == [2, 1]


def test_evening_digest_keeps_items_still_ahead_tonight_apart(prefs, at, make_row, check_view, buttons):
    now = at(2026, 10, 9, 21, 0)
    agenda = _agenda(
        done=[make_row(title="Отчёт", occ_status="done", due_at=at(2026, 10, 9, 13, 0))],
        open=[
            make_row(item_id=2, title="Позвонить маме", due_at=at(2026, 10, 9, 19, 0)),
            make_row(item_id=3, title="Таблетки", due_at=at(2026, 10, 9, 22, 0), rrule="FREQ=DAILY"),
        ],
    )
    text, kb = check_view(views.evening_digest(DigestData(kind="evening", agenda=agenda), prefs, now))
    assert lines(text) == [
        "🌙 <b>Итоги дня</b>, пт 9 окт",
        "Сделано 1 из 2",
        "",
        "<b>Сделано</b>",
        "✅ 📌 <s>Отчёт</s>",
        "",
        "<b>Не сделано</b>",
        "1. ⬜ 19:00 · 📌 Позвонить маме",
        "",
        "<b>Ещё сегодня</b>",
        "2. 🕒 22:00 · 📌 Таблетки 🔁",
    ]
    assert buttons(kb) == [
        ("✅ 1", _done_cb(1, 2, kind="e", s="m")),
        ("✅ 2", _done_cb(2, 3, kind="e", s="a")),
        ("📅 Перенести невыполненное на завтра", DigestCb(a="tmr", d="20261009").pack()),
    ]


def test_evening_digest_with_only_items_ahead_has_no_move_button(prefs, at, make_row, check_view, buttons):
    now = at(2026, 10, 9, 21, 0)
    agenda = _agenda(
        done=[make_row(title="Отчёт", occ_status="done", due_at=at(2026, 10, 9, 13, 0))],
        open=[make_row(item_id=2, title="Таблетки", due_at=at(2026, 10, 9, 22, 0))],
    )
    text, kb = check_view(views.evening_digest(DigestData(kind="evening", agenda=agenda), prefs, now))
    assert lines(text)[1] == "Сделано 1 из 1"
    assert "<b>Ещё сегодня</b>" in text and "Не сделано" not in text
    assert buttons(kb) == [("✅ 1", _done_cb(1, 2, kind="e", s="a"))]


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


def test_digests_truncate(prefs, at, make_row, check_view, buttons):
    now = at(2026, 10, 9, 9, 0)
    many = [make_row(title="задача & " * 12, due_at=at(2026, 10, 9, 12, 0)) for _ in range(300)]
    old = [make_row(title="старое & " * 12, due_at=at(2026, 10, 1, 12, 0)) for _ in range(300)]
    data = DigestData(kind="morning", agenda=_agenda(open=many, overdue=old, done=many), recurring_today=3)
    text, kb = check_view(views.morning_digest(data, prefs, now))
    assert "… и ещё" in text and text.endswith("🔁 Повторяющихся сегодня: 3")
    assert views.LIST_MORE_HINT in text
    assert [label for label, _ in buttons(kb)] == [f"✅ {n}" for n in range(1, 21)]
    text, kb = check_view(views.evening_digest(data, prefs, now))
    assert "… и ещё" in text and kb is not None


# --- numbered lists: [✅ N] / [↩️ N] (iteration 3, §2) ----------------------------------------------------------


def test_today_list_numbers_the_open_lines(prefs, at, make_row, check_view, buttons):
    now = at(2026, 10, 9, 12, 0)
    agenda = _agenda(
        open=[
            make_row(item_id=1, occurrence_id=11, title="Купить хлеб", due_at=at(2026, 10, 9, 13, 0)),
            make_row(item_id=2, occurrence_id=12, title="Позвонить", due_at=at(2026, 10, 9, 15, 0)),
        ],
        overdue=[make_row(item_id=3, occurrence_id=13, title="Отчёт", due_at=at(2026, 10, 8, 19, 0))],
        done=[make_row(item_id=4, occurrence_id=14, title="Зарядка", occ_status="done", due_at=at(2026, 10, 9, 9))],
    )
    text, kb = check_view(views.today_list(agenda, prefs, now))
    assert lines(text) == [
        "<b>📅 Сегодня, пт 9 окт</b>",
        "1. сегодня 13:00 · 📌 Купить хлеб",
        "2. сегодня 15:00 · 📌 Позвонить",
        "",
        "<b>⚠️ Просрочено</b>",
        "3. вчера 19:00 · 📌 Отчёт",
        "",
        "<b>✅ Сделано</b>",
        "• сегодня 09:00 · 📌 <s>Зарядка</s>",
    ]
    assert buttons(kb) == [
        ("✅ 1", _done_cb(1, 1, 11, kind="t")),
        ("✅ 2", _done_cb(2, 2, 12, kind="t")),
        ("✅ 3", _done_cb(3, 3, 13, kind="t", s="v")),
    ]


def test_today_list_without_open_lines_has_no_buttons(prefs, at, make_row, check_view):
    agenda = _agenda(done=[make_row(title="Зарядка", occ_status="done", due_at=at(2026, 10, 9, 9))])
    text, kb = check_view(views.today_list(agenda, prefs, at(2026, 10, 9, 12, 0)))
    assert "На сегодня ничего не запланировано." in text
    assert kb is None


def test_tasks_list_at_most_20_buttons_5_a_row(prefs, now, make_row, check_view, buttons):
    rows = [make_row(item_id=100 + i, occurrence_id=None, title=f"Задача {i}") for i in range(25)]
    text, kb = check_view(views.tasks_list(rows, prefs, now))
    assert lines(text)[:3] == ["<b>📋 Открытые задачи</b>", "1. 📌 Задача 0", "2. 📌 Задача 1"]
    assert "20. 📌 Задача 19" in lines(text) and "• 📌 Задача 20" in lines(text)
    assert lines(text)[-1] == views.TASKS_MORE_HINT
    assert [len(row) for row in kb.inline_keyboard] == [5, 5, 5, 5]
    assert buttons(kb)[0] == ("✅ 1", ListCb(a="done", k="k", d="", s="o", n=1, o=0, i=100).pack())
    assert buttons(kb)[-1][0] == "✅ 20"


def test_tasks_list_empty(prefs, now, check_view):
    assert check_view(views.tasks_list([], prefs, now)) == ("<b>📋 Открытые задачи</b>\nОткрытых задач нет 🎉", None)


def test_list_buttons_fit_64_bytes_with_huge_ids(prefs, at, make_row, check_view):
    rows = [make_row(item_id=BIG + i, occurrence_id=BIG + i, due_at=at(2026, 10, 9, 13)) for i in range(20)]
    for view in (
        views.tasks_list(rows, prefs, at(2026, 10, 9, 12)),
        views.today_list(_agenda(open=rows), prefs, at(2026, 10, 9, 12)),
        views.evening_digest(DigestData(kind="evening", agenda=_agenda(overdue=rows)), prefs, at(2026, 10, 9, 21)),
    ):
        _, kb = check_view(view)
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert len(datas) >= 20 and all(len(d.encode()) <= 64 for d in datas)


def test_a_done_numbered_line_is_struck_through_with_an_undo_button(prefs, at, make_row, check_view, buttons):
    now = at(2026, 10, 9, 21, 0)
    milk = make_row(item_id=1, occurrence_id=11, title="Молоко", due_at=at(2026, 10, 9, 13, 0))
    mom = make_row(item_id=2, occurrence_id=12, title="Маме", due_at=at(2026, 10, 9, 19, 0), occ_status="done")
    pills = make_row(item_id=3, occurrence_id=13, title="Таблетки", due_at=at(2026, 10, 9, 22, 0), occ_status="done")
    lines_ = {views.SEC_DONE: [], views.SEC_MISSED: [(milk, 1), (mom, 2)], views.SEC_AHEAD: [(pills, 3)]}
    data = DigestData(kind="evening", agenda=_agenda())
    text, kb = check_view(views.evening_digest(data, prefs, now, lines=lines_))
    assert lines(text) == [
        "🌙 <b>Итоги дня</b>, пт 9 окт",
        "Сделано 2 из 3",
        "",
        "<b>Не сделано</b>",
        "1. ⬜ 13:00 · 📌 Молоко",
        "2. ✅ 19:00 · 📌 <s>Маме</s>",
        "",
        "<b>Ещё сегодня</b>",
        "3. ✅ 22:00 · 📌 <s>Таблетки</s>",
    ]
    assert [label for label, _ in buttons(kb)] == ["✅ 1", "↩️ 2", "↩️ 3", "📅 Перенести невыполненное на завтра"]
    assert buttons(kb)[1][1] == ListCb(a="undo", k="e", d="20261009", s="m", n=2, o=12, i=2).pack()


def test_pin_lines_keeps_the_numbers_and_appends_new_rows(make_row):
    a = make_row(item_id=1, occurrence_id=11, title="A")
    b_done = make_row(item_id=2, occurrence_id=12, title="B", occ_status="done")
    c = make_row(item_id=3, occurrence_id=13, title="C")
    new = make_row(item_id=4, occurrence_id=14, title="New")
    fresh = {views.SEC_OPEN: [a, c, new], views.SEC_OVERDUE: [], views.SEC_DONE: [b_done]}
    pinned = [(views.SEC_OPEN, 1, a), (views.SEC_OPEN, 2, b_done), (views.SEC_OVERDUE, 3, c)]
    lines_ = views.pin_lines("t", fresh, pinned)
    # B keeps its place (not moved to "done"), C stays overdue, the new row has no number
    assert lines_[views.SEC_OPEN] == [(a, 1), (b_done, 2), (new, None)]
    assert lines_[views.SEC_OVERDUE] == [(c, 3)]
    assert lines_[views.SEC_DONE] == []
    # /tasks tells lines apart by item: a series' next instance is the same line
    nxt = make_row(item_id=1, occurrence_id=99, title="A")
    assert views.pin_lines("k", {views.SEC_OPEN: [nxt]}, [(views.SEC_OPEN, 1, a)])[views.SEC_OPEN] == [(a, 1)]


def test_number_lines_skips_done_rows_and_stops_at_20(make_row):
    rows = [make_row(item_id=i, title=str(i)) for i in range(22)]
    rows[0] = make_row(item_id=0, title="done", occ_status="done")
    numbered = views.number_lines("k", {views.SEC_OPEN: rows})[views.SEC_OPEN]
    assert [n for _, n in numbered] == [None, *range(1, 21), None]


def test_period_lines_are_numbered_after_today_and_overdue(prefs, at, make_row, check_view, buttons):
    now = at(2026, 10, 9, 12, 0)
    week = make_row(
        item_id=5,
        occurrence_id=15,
        title="Raspberry Pi",
        period_kind="week",
        period_start=date(2026, 10, 5),
        period_end=date(2026, 10, 11),
        due_at=at(2026, 10, 9, 19, 0),
    )
    agenda = _agenda(open=[make_row(item_id=1, due_at=at(2026, 10, 9, 13))], period_items=[week])
    text, kb = check_view(views.today_list(agenda, prefs, now))
    assert "🗓 <b>На этой неделе</b> (до вс 11 окт)\n2. 📌 Raspberry Pi" in text
    assert buttons(kb)[1] == ("✅ 2", _done_cb(2, 5, 15, kind="t", s="p"))


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
    assert "аудиофайлы (до 5 мин)" in text and "кружочки сохраняю без распознавания" in text
    assert "🌙 на вечер" in text and "✅ 1 ✅ 2" in text and "↩️" in text  # iteration 3: §4 and §2


# --- settings ----------------------------------------------------------------------------------------------------


def test_settings_menu_shows_all_values(check_view, buttons):
    prefs = Prefs(
        tz="Europe/Berlin",
        task_default_time="18:00",
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
        "обед 12:30",
        "Тихие часы: 23:00–08:00 (выкл)",
        "утро 09:00 (вкл), вечер 21:00 (выкл)",
        "Бюджет: $7.50 в месяц",
        "Голос: язык English, распознавание авто",
        "новых напоминаний",
    ):
        assert expected in text
    assert "Напоминать" not in text  # no default lead (2026-10-07)
    assert [cb for _, cb in buttons(kb)] == [
        "s:menu:tz",
        "s:ask:task_default_time",
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
    prefs = Prefs(tz="Europe/Kyiv", stt_language="ru", budget_usd=10, quiet_enabled=False)
    assert ("✓ Europe/Kyiv", "s:tz:Europe/Kyiv") in buttons(views.settings_tz_menu(prefs)[1])
    assert ("✓ Русский", "s:stt:ru") in buttons(views.settings_stt_menu(prefs)[1])
    assert ("✓ $10", "s:budget:10") in buttons(views.settings_budget_menu(prefs)[1])
    assert ("🔕 Включить тихие часы", "s:quiet_toggle:") in buttons(views.settings_quiet_menu(prefs)[1])
    auto = Prefs(stt_language=None)
    assert ("✓ Авто", "s:stt:auto") in buttons(views.settings_stt_menu(auto)[1])
    assert ("✓ 🔀 Авто: chutes, при сбое — локальный", "s:stt_mode:auto") in buttons(views.settings_stt_menu(auto)[1])


def test_old_lead_settings_menu_falls_back_to_main(prefs):
    assert "lead" not in views.SETTINGS_MENUS and "lead" not in views.SETTINGS_PARENT
    assert views.settings_view("lead", prefs) == views.settings_menu(prefs)


@pytest.mark.parametrize("local_stt", [True, False])
def test_settings_voice_menu_language_and_mode(local_stt, check_view, buttons):
    prefs = Prefs(stt_language="en", stt_mode="local")
    text, kb = check_view(views.settings_view("stt", prefs, local_stt=local_stt))
    assert text.startswith("🎙 <b>Голос</b>\nЯзык: English\nРаспознавание: локальный")
    assert "кружочки — нет" in text and "до 5 минут" in text
    assert ("LOCAL_STT_URL пуст" in text) is not local_stt
    note = "" if local_stt else " (не настроен)"
    assert buttons(kb) == [
        ("Авто", "s:stt:auto"),
        ("Русский", "s:stt:ru"),
        ("✓ English", "s:stt:en"),
        ("🔀 Авто: chutes, при сбое — локальный", "s:stt_mode:auto"),
        (f"✓ 💻 Локальный, при сбое — chutes{note}", "s:stt_mode:local"),
        ("☁️ Только chutes", "s:stt_mode:chutes"),
        ("← Назад", "s:menu:main"),
    ]
    # One mode per row: the long labels are not cut on a phone screen.
    assert [len(row) for row in kb.inline_keyboard] == [3, 1, 1, 1, 1]


def test_settings_voice_menu_marks_chutes_only(buttons):
    _, kb = views.settings_stt_menu(Prefs(stt_mode="chutes"), local_stt=True)
    assert ("✓ ☁️ Только chutes", "s:stt_mode:chutes") in buttons(kb)
    assert "распознавание только chutes" in views.settings_menu(Prefs(stt_mode="chutes"))[0]


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
        views_to_check.append(views.settings_view("stt", prefs, local_stt=True))
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
        "quiet_toggle",
        "digest_toggle",
        "stt",
        "stt_mode",
        "budget",
        "ask",
    }
    assert set(views.SETTINGS_PARENT) <= {k for k, _ in emitted}


# --- all builders ------------------------------------------------------------------------------------------------


def test_all_builders_with_huge_ids(prefs, now, make_item, make_occ, make_reminder, make_row, check_view):
    item = make_item(id=BIG, kind="event", rrule="FREQ=DAILY", rec_active=True, lead_minutes=[1440, 5])
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
        views.reminder_message(item, occ, rem, prefs, now),
        views.reminder_done(item, occ, prefs, now),
        views.reminder_snoozed(item, rem, prefs, now),
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
    occ_cbs = {OccCb(a=a, id=BIG).pack() for a in ("done", "s60", "eve", "tmr")}
    reminder_kb = built[5][1]
    assert {b.callback_data for row in reminder_kb.inline_keyboard for b in row} == occ_cbs
