"""Commands router and fuzzy time (docs/features/iteration-2.md §A): /someday, /tasks without "когда-нибудь",
/today with the period section. Reference time: Tue 2026-10-06 10:00 Europe/Warsaw (frozen)."""

from __future__ import annotations

from dumpbot.ai.schemas import When


async def test_someday_lists_only_someday_items(feed, updates, seed, tg) -> None:
    await seed.item("Выучить испанский", when=When(text="когда-нибудь", period="someday"))
    await seed.item("Написать <книгу>", kind="idea", when=When(text="когда-нибудь", period="someday"))
    await seed.item("Позвонить маме")

    await feed(updates.message("/someday"))

    [text] = tg.texts
    assert text.startswith("<b>🌙 Когда-нибудь</b>")
    assert "• когда-нибудь · 📌 Выучить испанский" in text
    assert "• когда-нибудь · 💡 Написать &lt;книгу&gt;" in text
    assert "Позвонить маме" not in text


async def test_someday_empty(feed, updates, tg) -> None:
    await feed(updates.message("/someday"))
    assert "когда-нибудь выучить испанский" in tg.texts[0]


async def test_tasks_leave_someday_items_out(feed, updates, seed, tg) -> None:
    await seed.item("Выучить испанский", when=When(text="когда-нибудь", period="someday"))
    await seed.item("Посмотреть Raspberry Pi", when=When(text="на этой неделе", period="this_week"))

    await feed(updates.message("/tasks"))

    [text] = tg.texts
    assert "• на этой неделе · 📌 Посмотреть Raspberry Pi" in text
    assert "испанский" not in text


async def test_today_shows_the_running_week(feed, updates, seed, tg) -> None:
    await seed.item("Посмотреть Raspberry Pi", when=When(text="на этой неделе", period="this_week"))
    await seed.item("Шины", when=When(text="на следующей неделе", period="next_week"))

    await feed(updates.message("/today"))

    [text] = tg.texts
    assert "На сегодня ничего не запланировано." in text
    assert "🗓 <b>На этой неделе</b> (до вс 11 окт)\n• 📌 Посмотреть Raspberry Pi" in text
    assert "Шины" not in text
