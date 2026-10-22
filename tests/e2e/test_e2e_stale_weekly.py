"""End-to-end, iteration 4 (docs/features/iteration-4.md): a task left hanging -> three digests without an action ->
"🕸 «Сделать карту» висит 1 день. Что делаем?" right after the digest -> [Оставить] (the count starts afresh) -> asked
again three digests later -> [💤 Когда-нибудь] -> the Sunday weekly review lists it under "🌙 Когда-нибудь" -> its
[✅ 1] closes it (§4, §6); a task moved twice by its reminder's [📅 Завтра] is asked about too.

Harness: conftest.py (``e2e``); the clock starts on Tue 2026-10-06 10:12 Europe/Warsaw.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from dumpbot.db.models import Event

WARSAW = ZoneInfo("Europe/Warsaw")


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=WARSAW).astimezone(UTC)


def task(title: str, when: dict[str, Any]) -> dict[str, Any]:
    return {"intent": "save", "items": [{"kind": "task", "title": title, "when": when}]}


async def save(e2e: Any, text: str, extraction: dict[str, Any]) -> None:
    e2e.ai.script(text, extraction)
    await e2e.card_for(e2e.tg.push_message(text))
    await e2e.idle()


async def tick(e2e: Any, now: datetime) -> list[Any]:
    """One scheduler tick; the messages it sent."""
    before = len(e2e.tg.sent)
    await e2e.tick(now)
    return e2e.tg.sent[before:]


async def press(e2e: Any, message: Any, data: str) -> Any:
    before = len(e2e.tg.edits_of(message.message_id))
    e2e.tg.push_callback(message, data)
    edits = await e2e.wait(lambda: e2e.tg.edits_of(message.message_id)[before:], f"edit after {data}")
    await e2e.wait_db(lambda: _journaled(e2e, data), f"{data} journaled")  # written right after the answer
    return edits[-1]


async def _journaled(e2e: Any, data: str) -> bool:
    rows = await e2e.all(Event)
    return any(e.type == "button" and (e.data or {}).get("cb") == data for e in rows)


def questions(messages: list[Any]) -> list[Any]:
    return [m for m in messages if m.text.startswith("🕸")]


async def test_a_hanging_task_is_asked_about_kept_put_off_and_reviewed(e2e: Any) -> None:
    await save(e2e, "сделать карту в 18", task("Сделать карту", {"text": "в 18", "time": "18:00"}))
    item = await e2e.item("Сделать карту")

    assert questions(await tick(e2e, local(2026, 10, 6, 18, 0))) == []  # its reminder, ignored
    assert questions(await tick(e2e, local(2026, 10, 6, 21, 0))) == []  # evening digest: 1
    morning = await tick(e2e, local(2026, 10, 7, 9, 0))  # morning digest: 2
    assert len(morning) == 1 and "Сделать карту" in morning[0].text
    sent = await tick(e2e, local(2026, 10, 7, 21, 0))  # evening digest: 3 -> the question right after it
    digest_msg, question = sent
    assert digest_msg.text.startswith("🌙 <b>Итоги дня</b>")
    assert question.text == "🕸 📌 «Сделать карту» висит 1 день. Что делаем?\n⏰ вчера 18:00 · просрочено"
    assert [text for text, _ in question.buttons] == ["✅ Сделано", "💤 Когда-нибудь", "🗑 Удалить", "Оставить"]
    asked = [e for e in await e2e.all(Event) if e.type == "stale_ask"]
    assert [(e.item_id, e.data) for e in asked] == [(item.id, {"digests": 3, "moves": 0})]

    e2e.clock.set(local(2026, 10, 7, 21, 5))
    kept = await press(e2e, question, f"st:keep:{item.id}")
    assert kept.text.startswith("👌 Оставил «Сделать карту» как есть")
    assert (await e2e.item("Сделать карту")).status == "open"

    assert questions(await tick(e2e, local(2026, 10, 8, 9, 0))) == []  # 1 since [Оставить]
    assert questions(await tick(e2e, local(2026, 10, 8, 21, 0))) == []  # 2
    again = questions(await tick(e2e, local(2026, 10, 9, 9, 0)))  # 3: asked again (a new day)
    assert len(again) == 1 and "висит 3 дня" in again[0].text

    e2e.clock.set(local(2026, 10, 9, 9, 30))
    put_off = await press(e2e, again[0], f"st:smd:{item.id}")
    assert put_off.text == "💤 «Сделать карту» — когда-нибудь, запись в /someday"
    assert (await e2e.item("Сделать карту")).someday is True

    assert await tick(e2e, local(2026, 10, 11, 18, 59)) == []
    [review] = await tick(e2e, local(2026, 10, 11, 19, 0))
    assert review.text.split("\n") == [
        "🗓 <b>Обзор недели</b>, вс 11 окт",
        "",
        "🌙 <b>Когда-нибудь</b>",
        "1. 📌 Сделать карту",
    ]
    e2e.clock.set(local(2026, 10, 11, 19, 10))
    closed = await press(e2e, review, review.button("ld:done:w:"))
    assert closed.text.endswith("1. ✅ 📌 <s>Сделать карту</s>")
    assert [text for text, _ in closed.buttons] == ["↩️ 1"]
    assert (await e2e.item("Сделать карту")).status == "done"
    assert await tick(e2e, local(2026, 10, 11, 20, 0)) == []  # once a week


async def test_a_task_moved_twice_is_asked_about(e2e: Any) -> None:
    await save(e2e, "позвонить в банк в 11", task("Позвонить в банк", {"text": "в 11", "time": "11:00"}))
    item = await e2e.item("Позвонить в банк")

    for day in (6, 7):  # its reminder, [📅 Завтра] — twice
        [reminder] = [m for m in await tick(e2e, local(2026, 10, day, 11, 0)) if m.text.startswith("🔔")]
        e2e.clock.set(local(2026, 10, day, 11, 5))
        await press(e2e, reminder, reminder.button("o:tmr:"))
    async with e2e.ctx.sessionmaker() as s:
        moves = [e for e in (await s.scalars(select(Event))).all() if (e.data or {}).get("move")]
    assert [e.data["action"] for e in moves] == ["o:tmr", "o:tmr"]

    [question] = questions(await tick(e2e, local(2026, 10, 7, 12, 0)))
    assert question.text.split("\n")[:2] == [
        "🕸 📌 «Позвонить в банк» висит 1 день. Что делаем?",
        "Переносили уже 2 раза.",
    ]
    done = await press(e2e, question, f"st:done:{item.id}")
    assert done.text.startswith("✅ <s>Позвонить в банк</s>")
    assert (await e2e.item("Позвонить в банк")).status == "done"
