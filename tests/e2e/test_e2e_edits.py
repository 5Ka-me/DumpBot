"""End-to-end: edits by description without an extra step (docs/features/iteration-3.md §3).

"Закрой задачу про документацию" -> the one open match is closed at once, the reply "✅ Закрыл «…»" has
[↩️ Отменить] -> pressed: the task is open again with its reminder, exactly as before; a second press only says so.
"купил молоко" with the open "Купить молоко" among the RELATED CANDIDATES closes it (prompt v7); a list pick gets the
edit already read (no second AI call).

Harness: conftest.py (``e2e``); the clock starts on Tue 2026-10-06 10:12 Europe/Warsaw.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")


def wall(dt: datetime) -> str:
    return dt.astimezone(WARSAW).strftime("%Y-%m-%d %H:%M")


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=WARSAW).astimezone(UTC)


def task(title: str, when: dict[str, Any] | None = None, kind: str = "task") -> dict[str, Any]:
    item: dict[str, Any] = {"kind": kind, "title": title}
    if when is not None:
        item["when"] = when
    return {"intent": "save", "items": [item]}


async def save(e2e: Any, text: str, extraction: dict[str, Any]) -> None:
    e2e.ai.script(text, extraction)
    await e2e.card_for(e2e.tg.push_message(text))
    await e2e.idle()


async def press(e2e: Any, message: Any, data: str) -> Any:
    """Press a button and wait for its toast; returns the toast."""
    before = len(e2e.tg.toasts())
    e2e.tg.push_callback(message, data)
    toasts = await e2e.wait(lambda: e2e.tg.toasts()[before:], f"toast after {data}")
    return toasts[0]


async def test_close_by_description_and_undo(e2e: Any) -> None:
    await save(e2e, "написать документацию в 17", task("Написать документацию", {"text": "в 17", "time": "17:00"}))
    doc = await e2e.item("Написать документацию")
    [occ] = await e2e.occurrences(doc.id)
    assert wall(occ.due_at) == "2026-10-06 17:00"
    assert [r.status for r in await e2e.reminders(doc.id)] == ["pending"]

    text = "Закрой задачу про документацию"
    e2e.ai.script(text, {"intent": "edit", "edit": {"target": "документацию", "action": "done"}})
    message_id = e2e.tg.push_message(text)
    reply = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "👌")
    assert reply.text == "✅ Закрыл «Написать документацию»"
    [(label, data)] = reply.buttons
    assert label == "↩️ Отменить" and data == f"u:{(await e2e.raw(message_id)).id}:{doc.id}"
    assert (await e2e.item("Написать документацию")).status == "done"
    [occ] = await e2e.occurrences(doc.id)
    assert occ.status == "done"
    assert [r.status for r in await e2e.reminders(doc.id)] == ["cancelled"]
    calls = len(e2e.ai.chat_requests)

    # [↩️ Отменить]: open again, the 17:00 reminder back; the reply turns into the item's card
    assert await press(e2e, reply, data) == "↩️ Отменено"
    card = (await e2e.wait(lambda: e2e.tg.edits_of(reply.message_id), "the undo's edit"))[-1]
    assert card.text.split("\n")[:2] == ["📌 <b>Написать документацию</b>", "🔔 сегодня 17:00"]
    assert ("✏️ Изменить", f"i:edit:{doc.id}") in card.buttons
    item = await e2e.item("Написать документацию")
    assert (item.status, item.done_at) == ("open", None)
    [occ] = await e2e.occurrences(doc.id)
    assert (occ.status, wall(occ.due_at)) == ("open", "2026-10-06 17:00")
    assert [r.status for r in await e2e.reminders(doc.id)] == ["pending"]
    assert len(e2e.ai.chat_requests) == calls  # no AI for the undo

    # a second press (a double tap): nothing more happens
    assert await press(e2e, reply, data) == "Уже отменено"
    assert len(e2e.tg.edits_of(reply.message_id)) == 1

    # the reminder still comes at 17:00
    await e2e.tick(local(2026, 10, 6, 17, 0))
    reminder = await e2e.wait(lambda: [c for c in e2e.tg.sent if c.text.startswith("🔔 <b>Написать")], "reminder")
    assert reminder


async def test_past_tense_closes_the_open_task(e2e: Any) -> None:
    await save(e2e, "купить молоко", task("Купить молоко", kind="purchase"))
    milk = await e2e.item("Купить молоко")

    seen: list[str | None] = []

    def done(req: Any) -> dict[str, Any]:
        seen.append(req.line("RELATED CANDIDATES "))
        return {"intent": "edit", "edit": {"target": "Купить молоко", "action": "done"}}

    e2e.ai.script("купил молоко", done)
    message_id = e2e.tg.push_message("купил молоко")
    reply = await e2e.card_for(message_id)
    assert reply.text == "✅ Закрыл «Купить молоко»"
    assert (await e2e.item("Купить молоко")).status == "done"
    [related] = seen
    assert related is not None and f'"id":{milk.id}' in related  # the open task is shown to the model
    assert 'купил X", "сделал X", "готово: X"' in e2e.ai.chat_requests[-1].body["messages"][0]["content"]


async def test_stale_undo_after_another_change(e2e: Any) -> None:
    await save(e2e, "виза завтра в 10", task("Подать на визу", {"text": "завтра в 10", "day": "+1", "time": "10:00"}))
    visa = await e2e.item("Подать на визу")
    text = "перенеси визу на послезавтра"
    move = {"intent": "edit", "edit": {"target": "визу", "when": {"text": "на послезавтра", "day": "+2"}}}
    e2e.ai.script(text, move)
    reply = await e2e.card_for(e2e.tg.push_message(text))
    assert reply.text == "🕒 Перенёс «Подать на визу» на чт 8 окт 10:00"
    [(_, undo_data)] = reply.buttons

    # done from the edit menu afterwards: the undo of the move is stale now
    e2e.tg.push_callback(reply, f"i:done:{visa.id}")
    await e2e.wait(lambda: e2e.tg.toasts(), "done toast")
    assert await press(e2e, reply, undo_data) == "🤷 Запись уже изменилась — отменить нельзя"
    assert (await e2e.item("Подать на визу")).status == "done"
    [occ] = await e2e.occurrences(visa.id)
    assert wall(occ.due_at) == "2026-10-08 10:00"
