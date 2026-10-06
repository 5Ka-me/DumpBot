"""Commands router: lists, search, /ask, /recurring, /settings, /usage, /help, /cancel, unknown commands."""

from __future__ import annotations

from datetime import timedelta

from aiogram.methods import SendMessage
from sqlalchemy import select

from dumpbot.ai.schemas import When
from dumpbot.bot.cbdata import ItemCb
from dumpbot.bot.sender import REACT_SEEN
from dumpbot.db.models import AiUsage, BotMessage, Occurrence


async def _purposes(seed) -> list[str]:
    async with seed.sessionmaker() as s:
        return list((await s.scalars(select(BotMessage.purpose).order_by(BotMessage.message_id))).all())


async def test_start_and_help_show_the_help(feed, updates, tg) -> None:
    await feed(updates.message("/start"))
    await feed(updates.message("/help"))

    start, help_ = tg.texts
    assert "Привет" in start
    assert "/today" in start and "/today" in help_
    assert all(m.chat_id == 4242 for m in tg.sent)


async def test_today_lists_open_overdue_and_done(feed, updates, seed, tg, frozen) -> None:
    today = await seed.item("Позвонить маме", when=When(text="сегодня в 15", time="15:00"))
    await seed.item("Купить хлеб", when=When(text="сегодня в 12", time="12:00"))
    await seed.item("Завтрашнее дело")  # tomorrow: not in /today
    async with seed.sessionmaker() as s:
        occ = await s.get(Occurrence, today.occurrence.id)
        occ.status, occ.done_at = "done", frozen.now
        overdue = await seed.item("Старое дело", when=When(text="вчера", time="09:00"))
        old = await s.get(Occurrence, overdue.occurrence.id)
        old.due_at = frozen.now - timedelta(days=1)
        await s.commit()

    await feed(updates.message("/today"))

    [text] = tg.texts
    assert "Сегодня" in text
    assert "Купить хлеб" in text
    assert "Просрочено" in text and "Старое дело" in text
    assert "Сделано" in text and "Позвонить маме" in text
    assert "Завтрашнее дело" not in text


async def test_today_empty(feed, updates, tg) -> None:
    await feed(updates.message("/today"))
    assert "ничего не запланировано" in tg.texts[0]


async def test_tasks_lists_only_actionable_open_items(feed, updates, seed, tg) -> None:
    await seed.item("Купить наушники", kind="purchase")
    await seed.item("Идея про отпуск", kind="idea", timed=False)

    await feed(updates.message("/tasks"))

    [text] = tg.texts
    assert "Открытые задачи" in text
    assert "Купить наушники" in text
    assert "Идея про отпуск" not in text


async def test_recent_lists_saved_items(feed, updates, seed, tg) -> None:
    await seed.item("Первая", kind="note", timed=False)
    await seed.item("Вторая", kind="idea", timed=False)

    await feed(updates.message("/recent"))

    text = tg.texts[0]
    assert "Последние записи" in text
    assert text.index("Вторая") < text.index("Первая")


async def test_search_without_text_shows_a_hint(feed, updates, tg) -> None:
    await feed(updates.message("/search"))
    assert "/search" in tg.texts[0]


async def test_search_finds_items_and_escapes_the_query(feed, updates, seed, tg) -> None:
    await seed.item("Купить наушники Sony", kind="purchase")
    await seed.item("Позвонить маме")

    await feed(updates.message("/search наушники"))
    await feed(updates.message("/search <b>нет</b>"))

    found, nothing = tg.texts
    assert "наушники Sony" in found and "маме" not in found
    assert "&lt;b&gt;" in nothing and "Ничего не нашёл" in nothing


async def test_ask_saves_a_forced_query_and_queues_it(feed, updates, seed, tg, ctx) -> None:
    update = updates.message("/ask что у меня завтра?")
    await feed(update)

    [raw] = await seed.raws()
    assert raw.force_query is True
    assert raw.text == "что у меня завтра?"
    assert raw.raw_json["text"] == "/ask что у меня завтра?"
    [job] = await seed.jobs()
    assert job.kind == "message" and job.payload == {"raw_id": raw.id}
    assert tg.reactions == [(update.message.message_id, REACT_SEEN)]
    assert ctx.jobs_wakeup.is_set()


async def test_ask_without_question_shows_a_hint(feed, updates, seed, tg) -> None:
    await feed(updates.message("/ask"))
    assert await seed.raws() == []
    assert "/ask" in tg.texts[0]


async def test_recurring_lists_series_with_stop_buttons(feed, updates, seed, tg, get_buttons) -> None:
    series = await seed.item("Планёрка", when=When(text="каждый пн в 9", time="09:00"), rrule="FREQ=WEEKLY;BYDAY=MO")
    await seed.item("Разовая")

    await feed(updates.message("/recurring"))

    [msg] = tg.of(SendMessage)
    assert "Планёрка" in msg.text and "Разовая" not in msg.text
    assert "понедельник" in msg.text
    assert get_buttons(msg.reply_markup) == [("⏹ 1. Планёрка", ItemCb(a="stoprec", id=series.item.id).pack())]
    assert await _purposes(seed) == ["recurring"]


async def test_recurring_empty(feed, updates, tg) -> None:
    await feed(updates.message("/recurring"))
    [msg] = tg.of(SendMessage)
    assert "Повторяющихся задач нет" in msg.text
    assert msg.reply_markup is None


async def test_settings_shows_the_menu(feed, updates, seed, tg, get_buttons) -> None:
    await feed(updates.message("/settings"))

    [msg] = tg.of(SendMessage)
    assert "Настройки" in msg.text and "Europe/Warsaw" in msg.text
    assert ("✖️ Закрыть", "s:close:") in get_buttons(msg.reply_markup)
    assert await _purposes(seed) == ["settings"]


async def test_usage_shows_this_months_spend(feed, updates, seed, tg, frozen) -> None:
    async with seed.sessionmaker() as s:
        s.add(AiUsage(created_at=frozen.now, purpose="extract", model="m", cost_usd=0.25))
        await s.commit()

    await feed(updates.message("/usage"))

    text = tg.texts[0]
    assert "Расходы на ИИ" in text
    assert "$0.25" in text and "$5.00" in text


async def test_cancel_without_dialog(feed, updates, tg) -> None:
    await feed(updates.message("/cancel"))
    assert tg.texts == ["Нечего отменять."]


async def test_unknown_command_hint(feed, updates, tg) -> None:
    await feed(updates.message("/nonsense"))
    assert "Не знаю такой команды" in tg.texts[0]


async def test_command_with_bot_mention(feed, updates, tg) -> None:
    await feed(updates.message("/help@dump_test_bot"))
    assert "/today" in tg.texts[0]
