"""Settings menu: navigation, one-tap changes, typed values with validation, budget raise -> deferred jobs run."""

from __future__ import annotations

from datetime import time, timedelta

import pytest
from aiogram.types import Chat, MessageOriginChannel

from dumpbot.bot.cbdata import ItemCb, SetCb
from dumpbot.bot.routers.settings import parse_money, parse_time, parse_tz, parse_value
from dumpbot.bot.sender import REACT_SAVED
from dumpbot.bot.states import SettingsStates
from dumpbot.db.models import Job
from dumpbot.domain.prefs import Prefs
from dumpbot.services.settings_repo import load_prefs, save_prefs
from dumpbot.workers import jobs

OWNER = 4242


async def _prefs(seed) -> Prefs:
    async with seed.sessionmaker() as s:
        return await load_prefs(s, OWNER)


async def _state(dp, bot) -> str | None:
    return await dp.fsm.get_context(bot, chat_id=OWNER, user_id=OWNER).get_state()


async def test_menu_navigation_and_close(feed, updates, tg, get_buttons) -> None:
    await feed(updates.callback(SetCb(key="menu", v="quiet"), message_id=930))
    await feed(updates.callback(SetCb(key="menu", v="main"), message_id=930))
    await feed(updates.callback(SetCb(key="menu", v="nonsense"), message_id=930))
    await feed(updates.callback(SetCb(key="close"), message_id=930))

    quiet, main, fallback, closed = tg.edits
    assert "Тихие часы" in quiet.text
    assert "Настройки" in main.text and "Настройки" in fallback.text
    assert "Настройки" in closed.text and closed.reply_markup is None
    assert tg.toasts == [None] * 4


@pytest.mark.parametrize(
    ("key", "value", "check"),
    [
        ("tz", "Europe/Kyiv", lambda p: p.tz == "Europe/Kyiv"),
        ("lead", "15", lambda p: p.lead_minutes == 15),
        ("quiet_toggle", "", lambda p: p.quiet_enabled is False),
        ("digest_toggle", "evening", lambda p: p.digest_evening_enabled is False and p.digest_morning_enabled),
        ("stt", "ru", lambda p: p.stt_language == "ru"),
        ("stt", "auto", lambda p: p.stt_language is None),
        ("budget", "3", lambda p: p.budget_usd == 3.0),
    ],
)
async def test_one_tap_changes(feed, updates, seed, tg, ctx, key, value, check) -> None:
    await feed(updates.callback(SetCb(key=key, v=value), message_id=931))

    assert check(await _prefs(seed))
    assert tg.toasts == ["✅ Сохранено"]
    [edit] = tg.edits
    assert edit.message_id == 931
    assert ctx.scheduler_wakeup.is_set()


@pytest.mark.parametrize(
    ("key", "value"),
    [("tz", "Mars/Olympus"), ("lead", "abc"), ("lead", "99999"), ("digest_toggle", "noon"), ("stt", "de")],
)
async def test_invalid_one_tap_values(feed, updates, seed, tg, ctx, key, value) -> None:
    await feed(updates.callback(SetCb(key=key, v=value)))

    assert await _prefs(seed) == Prefs()
    assert tg.toasts == ["Неверное значение"]
    assert tg.edits == []
    assert not ctx.scheduler_wakeup.is_set()


async def test_unknown_key_is_stale(feed, updates, tg) -> None:
    await feed(updates.callback(SetCb(key="volume", v="11")))
    assert tg.toasts == ["Кнопка устарела"]


async def test_ask_flow_saves_a_typed_time(feed, updates, seed, tg, dp, bot, ctx) -> None:
    await feed(updates.callback(SetCb(key="ask", v="slot_morning"), message_id=932))

    assert await _state(dp, bot) == SettingsStates.value.state
    [prompt] = tg.edits
    assert "утро" in prompt.text and "ЧЧ:ММ" in prompt.text

    answer = updates.message("8.45")
    await feed(answer)

    assert (await _prefs(seed)).slot_morning == time(8, 45)
    assert await _state(dp, bot) is None
    menu = tg.edits[-1]
    assert menu.message_id == 932 and "Время суток" in menu.text
    assert tg.reactions == [(answer.message.message_id, REACT_SAVED)]
    assert ctx.scheduler_wakeup.is_set()
    assert await seed.raws() == []


@pytest.mark.parametrize(
    ("field", "bad", "good", "expected"),
    [
        ("quiet_start", "25:00", "22:30", time(22, 30)),
        ("tz", "Europe/Atlantis", "Asia/Tbilisi", "Asia/Tbilisi"),
        ("lead_minutes", "-5", "45", 45),
        ("budget_usd", "дорого", "7,5", 7.5),
    ],
)
async def test_ask_flow_validation(feed, updates, seed, tg, dp, bot, field, bad, good, expected) -> None:
    await feed(updates.callback(SetCb(key="ask", v=field)))

    await feed(updates.message(bad))

    assert "Попробуй ещё раз" in tg.texts[-1]
    assert await _state(dp, bot) == SettingsStates.value.state
    assert getattr(await _prefs(seed), field) == getattr(Prefs(), field)

    await feed(updates.message(good))

    assert getattr(await _prefs(seed), field) == expected
    assert await _state(dp, bot) is None


async def test_a_second_bad_value_is_saved_as_a_note(feed, updates, seed, tg, dp, bot) -> None:
    # An abandoned "конец тихих часов" prompt: one error, then the owner's notes are saved again.
    await feed(updates.callback(SetCb(key="ask", v="quiet_end"), message_id=934))

    await feed(updates.message("купить хлеб"))
    assert "Попробуй ещё раз" in tg.texts[-1]
    assert await _state(dp, bot) == SettingsStates.value.state

    await feed(updates.message("позвонить маме"))

    [raw] = await seed.raws()
    assert raw.text == "позвонить маме"
    assert await _state(dp, bot) is None
    assert "Тихие часы" in tg.edits[-1].text and tg.edits[-1].message_id == 934
    assert (await _prefs(seed)).quiet_end == Prefs().quiet_end


@pytest.mark.parametrize("kind", ["forward", "reply", "question", "photo", "late"])
async def test_what_is_not_a_value_is_captured(feed, updates, seed, dp, bot, frozen, kind) -> None:
    await feed(updates.callback(SetCb(key="ask", v="lead_minutes"), message_id=935))
    if kind == "forward":
        origin = MessageOriginChannel(date=frozen.now, chat=Chat(id=-1001, type="channel", title="Хабр"), message_id=7)
        message = updates.message("15", forward_origin=origin)
    elif kind == "reply":
        created = await seed.item()
        await seed.bot_message(960, item_id=created.item.id)
        message = updates.message("15", reply_to=960)
    elif kind == "question":
        message = updates.message("? 15")
    elif kind == "photo":
        message = updates.message(photo=True, caption="15")
    else:
        frozen.advance(minutes=6)
        message = updates.message("15")

    await feed(message)

    assert (await _prefs(seed)).lead_minutes == Prefs().lead_minutes
    assert len(await seed.raws()) == 1
    assert await _state(dp, bot) is None


async def test_a_late_reply_to_the_prompt_is_still_the_value(feed, updates, seed, dp, bot, frozen) -> None:
    await feed(updates.callback(SetCb(key="ask", v="lead_minutes"), message_id=936))
    frozen.advance(minutes=10)

    await feed(updates.message("15", reply_to=936))

    assert (await _prefs(seed)).lead_minutes == 15
    assert await seed.raws() == []


async def test_ask_unknown_field_is_stale(feed, updates, tg, dp, bot) -> None:
    await feed(updates.callback(SetCb(key="ask", v="owner_id")))
    assert tg.toasts == ["Кнопка устарела"]
    assert await _state(dp, bot) is None


async def test_cancel_button_and_command(feed, updates, tg, dp, bot) -> None:
    await feed(updates.callback(SetCb(key="ask", v="quiet_end"), message_id=933))
    await feed(updates.callback(SetCb(key="menu", v="quiet"), message_id=933))  # [✖️ Отмена]
    assert await _state(dp, bot) is None

    await feed(updates.callback(SetCb(key="ask", v="quiet_end"), message_id=933))
    await feed(updates.message("/cancel"))
    assert await _state(dp, bot) is None
    assert tg.texts[-1] == "Отменено."
    assert "Тихие часы" in tg.edits[-1].text


async def test_settings_prompt_replaces_an_edit_dialog(feed, updates, seed, tg, dp, bot) -> None:
    created = await seed.item()
    await feed(updates.callback(ItemCb(a="title", id=created.item.id), message_id=950))

    await feed(updates.callback(SetCb(key="ask", v="quiet_end"), message_id=951))

    restored, prompt = tg.edits[-2:]
    assert restored.message_id == 950 and restored.text.startswith("📌")
    assert prompt.message_id == 951
    assert await _state(dp, bot) == SettingsStates.value.state


async def test_navigating_another_menu_keeps_the_dialog(feed, updates, dp, bot) -> None:
    await feed(updates.callback(SetCb(key="ask", v="quiet_end"), message_id=952))
    await feed(updates.callback(SetCb(key="menu", v="digest"), message_id=953))
    assert await _state(dp, bot) == SettingsStates.value.state


async def test_expired_settings_prompt_lets_text_through(feed, updates, seed, dp, bot, frozen) -> None:
    await feed(updates.callback(SetCb(key="ask", v="quiet_end")))
    frozen.advance(minutes=30)

    await feed(updates.message("07:00"))

    assert (await _prefs(seed)).quiet_end == Prefs().quiet_end
    [raw] = await seed.raws()
    assert raw.text == "07:00"
    assert await _state(dp, bot) is None


# --- budget ------------------------------------------------------------------------------------------------------


async def _deferred_job(seed, frozen) -> Job:
    raw = await seed.raw(70, status="deferred")
    async with seed.sessionmaker() as s:
        await jobs.enqueue(
            s, "message", {"raw_id": raw.id}, dedupe_key=f"msg:{raw.id}", run_after=frozen.now + timedelta(days=20)
        )
        await s.commit()
    [job] = await seed.jobs()
    return job


async def test_budget_increase_requeues_deferred_jobs(feed, updates, seed, tg, ctx, frozen) -> None:
    job = await _deferred_job(seed, frozen)

    await feed(updates.callback(SetCb(key="budget", v="10")))

    [after] = await seed.jobs()
    assert after.id == job.id and after.run_after == frozen.now
    assert ctx.jobs_wakeup.is_set()
    assert (await _prefs(seed)).budget_usd == 10.0


async def test_budget_decrease_leaves_deferred_jobs(feed, updates, seed, ctx, frozen) -> None:
    job = await _deferred_job(seed, frozen)

    await feed(updates.callback(SetCb(key="budget", v="1")))

    [after] = await seed.jobs()
    assert after.run_after == job.run_after
    assert not ctx.jobs_wakeup.is_set()


async def test_typed_budget_increase_requeues(feed, updates, seed, ctx, frozen) -> None:
    async with seed.sessionmaker() as s:
        await save_prefs(s, OWNER, Prefs(budget_usd=0))
        await s.commit()
    await _deferred_job(seed, frozen)

    await feed(updates.callback(SetCb(key="ask", v="budget_usd")))
    await feed(updates.message("$2.5"))

    [after] = await seed.jobs()
    assert after.run_after == frozen.now
    assert (await _prefs(seed)).budget_usd == 2.5
    assert ctx.jobs_wakeup.is_set()


# --- parsers (no DB) ---------------------------------------------------------------------------------------------


def test_parsers() -> None:
    assert parse_time("9:05") == time(9, 5)
    assert parse_time(" 23.59 ") == time(23, 59)
    for bad in ("24:00", "9", "9:5", "ab:cd", ""):
        with pytest.raises(ValueError):
            parse_time(bad)
    assert parse_tz(" Europe/Warsaw ") == "Europe/Warsaw"
    for bad in ("", "Europe /Warsaw", "Nowhere/City", "../etc/passwd", "Europe"):
        with pytest.raises(ValueError):
            parse_tz(bad)
    assert parse_money("10") == 10.0 and parse_money("$ 7,25") == 7.25 and parse_money("0") == 0.0
    for bad in ("-1", "nan", "inf", "пять"):
        with pytest.raises(ValueError):
            parse_money(bad)
    assert parse_value("minutes", "30") == 30
    with pytest.raises(ValueError):
        parse_value("minutes", "10081")
    with pytest.raises(ValueError):
        parse_value("color", "red")
