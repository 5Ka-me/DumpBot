"""Iteration 4 through the dispatcher: the answers to the stale question (§4: [✅ Сделано] [💤 Когда-нибудь]
[🗑 Удалить] [Оставить], journaled as actions), [✅ N] of the weekly review (§6), the weekly review's settings, and
the moves the stale-items check counts (a move button, the evening digest's bulk move). The clock starts at Tue
2026-10-06 10:00 Europe/Warsaw."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from dumpbot import clock
from dumpbot.ai.schemas import When
from dumpbot.bot import lists
from dumpbot.bot.cbdata import DigestCb, ListCb, OccCb, SetCb, StaleCb
from dumpbot.db.models import Event, Item, Occurrence, UserSettings
from dumpbot.domain.prefs import Prefs
from dumpbot.services import stale

WARSAW = ZoneInfo("Europe/Warsaw")
OWNER = 4242


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=WARSAW).astimezone(UTC)


async def _item(seed, item_id: int) -> Item:
    async with seed.sessionmaker() as s:
        return await s.get(Item, item_id)


async def _events(seed) -> list[Event]:
    async with seed.sessionmaker() as s:
        return list((await s.scalars(select(Event).order_by(Event.id))).all())


async def _question(seed, created, message_id: int = 960) -> int:
    await seed.bot_message(message_id, purpose="stale", item_id=created.item.id, occurrence_id=created.occurrence.id)
    return message_id


async def test_done_closes_the_item(feed, updates, seed, tg, ctx, get_buttons) -> None:
    created = await seed.item("Сделать карту")
    message_id = await _question(seed, created)

    await feed(updates.callback(StaleCb(a="done", id=created.item.id), message_id=message_id))

    assert (await _item(seed, created.item.id)).status == "done"
    assert tg.toasts == ["✅ Готово"]
    [edit] = tg.edits
    assert edit.text.startswith("✅ <s>Сделать карту</s>")
    assert get_buttons(edit.reply_markup) == [("↩️ Не сделано", f"o:undone:{created.occurrence.id}")]
    assert ctx.scheduler_wakeup.is_set()
    [row] = await _events(seed)
    assert (row.type, row.source, row.item_id, row.data["action"]) == ("button", "stale", created.item.id, "st:done")


async def test_someday_takes_the_date_away(feed, updates, seed, tg) -> None:
    created = await seed.item("Сделать карту")
    await feed(updates.callback(StaleCb(a="smd", id=created.item.id), message_id=await _question(seed, created)))
    item = await _item(seed, created.item.id)
    assert (item.someday, item.status) == (True, "open")
    assert (
        tg.toasts == ["💤 Когда-нибудь"] and tg.edits[0].text == "💤 «Сделать карту» — когда-нибудь, запись в /someday"
    )


async def test_delete_can_be_taken_back(feed, updates, seed, tg, get_buttons) -> None:
    created = await seed.item("Сделать карту")
    await feed(updates.callback(StaleCb(a="del", id=created.item.id), message_id=await _question(seed, created)))
    assert (await _item(seed, created.item.id)).status == "deleted"
    assert tg.toasts == ["🗑 Удалено"]
    assert get_buttons(tg.edits[0].reply_markup) == [("↩️ Вернуть", f"i:undel:{created.item.id}")]


async def test_keep_changes_nothing_and_starts_the_count_afresh(feed, updates, seed, tg, frozen) -> None:
    created = await seed.item("Сделать карту")
    item_id = created.item.id
    async with seed.sessionmaker() as s:
        for day in (6, 7, 8):
            s.add(Event(type="digest_item", item_id=item_id, created_at=local(2026, 10, day, 9)))
        s.add(Event(type="stale_ask", item_id=item_id, created_at=local(2026, 10, 9, 9)))
        await s.commit()
    frozen.now = local(2026, 10, 9, 10)

    await feed(updates.callback(StaleCb(a="keep", id=item_id), message_id=await _question(seed, created)))

    assert (await _item(seed, item_id)).status == "open"
    assert tg.toasts == ["👌 Оставил"] and tg.edits[0].reply_markup is None
    async with seed.sessionmaker() as s:
        assert await stale.candidates(s) == []
        for day in (10, 11, 12):
            s.add(Event(type="digest_item", item_id=item_id, created_at=local(2026, 10, day, 9)))
        await s.commit()
        assert [c.item_id for c in await stale.candidates(s)] == [item_id]  # three more: asked again


async def test_an_answer_to_a_closed_or_deleted_item(feed, updates, seed, tg, ctx) -> None:
    done = await seed.item("Сделано")
    gone = await seed.item("Удалено")
    async with seed.sessionmaker() as s:
        (await s.get(Item, done.item.id)).status = "done"
        (await s.get(Item, gone.item.id)).status = "deleted"
        await s.commit()

    await feed(updates.callback(StaleCb(a="keep", id=done.item.id), message_id=await _question(seed, done)))
    await feed(updates.callback(StaleCb(a="done", id=gone.item.id), message_id=await _question(seed, gone, 961)))
    await feed(updates.callback(StaleCb(a="done", id=10**9)))
    await feed(updates.callback("st:nope:1"))

    assert tg.toasts == ["Уже сделано", "🗑 Запись удалена", "Запись не найдена", "Кнопка устарела"]
    assert tg.edits[1].text.startswith("🗑 Удалено")


# --- moves for the stale-items check -------------------------------------------------------------------------------


async def test_move_buttons_are_journaled_as_moves(feed, updates, seed, frozen) -> None:
    created = await seed.item("Отчёт", when=When(text="сегодня в 12", time="12:00"))
    frozen.now = local(2026, 10, 6, 12)
    await seed.bot_message(950, purpose="reminder", item_id=created.item.id, occurrence_id=created.occurrence.id)

    await feed(updates.callback(OccCb(a="s60", id=created.occurrence.id), message_id=950))
    await feed(updates.callback(OccCb(a="tmr", id=created.occurrence.id), message_id=950))

    rows = await _events(seed)
    assert [(r.data["action"], r.data.get("move")) for r in rows] == [("o:s60", None), ("o:tmr", True)]


async def test_the_evening_digests_bulk_move_journals_each_item(feed, updates, seed, frozen) -> None:
    first = await seed.item("Отчёт", when=When(text="сегодня в 12", time="12:00"))
    second = await seed.item("Звонок", when=When(text="сегодня в 15", time="15:00"))
    frozen.now = local(2026, 10, 6, 21)
    await seed.bot_message(970, purpose="digest")

    await feed(updates.callback(DigestCb(a="tmr", d="20261006"), message_id=970))

    edits = [r for r in await _events(seed) if r.type == "edit"]
    assert sorted((r.item_id, r.source, r.data) for r in edits) == sorted(
        [(first.item.id, "digest", {"move": True}), (second.item.id, "digest", {"move": True})]
    )
    assert {r.occurrence_id for r in edits} == {first.occurrence.id, second.occurrence.id}


# --- the weekly review's [✅ N] and settings -------------------------------------------------------------------------


async def test_a_weekly_review_line_closes_and_reopens(feed, updates, seed, tg, frozen) -> None:
    idea = await seed.item("Испанский", kind="idea", when=When(text="когда-нибудь", period="someday"))
    week = await seed.item("Шины", when=When(text="на следующей неделе", period="next_week"))
    frozen.now = local(2026, 10, 11, 19)
    async with seed.sessionmaker() as s:
        text, kb = await lists.render(s, "w", date(2026, 10, 11), Prefs(), clock.utcnow())
    assert "1. 📌 Шины" in text and "2. 💡 Испанский" in text

    await feed(
        updates.callback(
            ListCb.unpack(kb.inline_keyboard[0][1].callback_data), text=text, reply_markup=kb, message_id=980
        )
    )

    assert (await _item(seed, idea.item.id)).status == "done"
    assert tg.toasts == ["✅ Закрыл «Испанский»"]
    edited = tg.edits[0]
    assert "2. ✅ 💡 <s>Испанский</s>" in edited.text and "1. 📌 Шины" in edited.text
    labels = [b.text for row in edited.reply_markup.inline_keyboard for b in row]
    assert labels == ["✅ 1", "↩️ 2"]

    await feed(
        updates.callback(
            ListCb.unpack(edited.reply_markup.inline_keyboard[0][1].callback_data),
            text=edited.text,
            reply_markup=edited.reply_markup,
            message_id=980,
        )
    )
    assert (await _item(seed, idea.item.id)).status == "open"
    assert "2. 💡 Испанский" in tg.edits[1].text
    del week


async def test_the_weekly_review_toggle_and_time(feed, updates, seed, tg) -> None:
    await feed(updates.callback(SetCb(key="digest_toggle", v="weekly"), message_id=931))
    async with seed.sessionmaker() as s:
        prefs = Prefs.from_stored((await s.get(UserSettings, OWNER)).prefs)
    assert prefs.digest_weekly_enabled is False and prefs.digest_morning_enabled
    assert "🗓 Обзор недели (воскресенье): 19:00, выкл" in tg.edits[0].text

    await feed(updates.callback(SetCb(key="ask", v="digest_weekly_time"), message_id=932))
    assert "обзор недели (воскресенье)" in tg.edits[1].text
    await feed(updates.message("18:30"))
    async with seed.sessionmaker() as s:
        prefs = Prefs.from_stored((await s.get(UserSettings, OWNER)).prefs)
    assert prefs.digest_weekly_time.isoformat() == "18:30:00"


async def _occurrence(seed, occ_id: int) -> Occurrence:
    async with seed.sessionmaker() as s:
        return await s.get(Occurrence, occ_id)
