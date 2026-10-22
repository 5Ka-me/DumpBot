"""services.events: the journal (docs/features/iteration-4.md §5) — rows, the source of a button press, and that a
failed write never raises."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from dumpbot import clock
from dumpbot.db.models import BotMessage, Event, Item, Occurrence
from dumpbot.services import events

T0 = datetime(2026, 10, 22, 8, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "utcnow", lambda: T0)


async def _events(sessionmaker) -> list[Event]:
    async with sessionmaker() as s:
        return list((await s.scalars(select(Event).order_by(Event.id))).all())


async def test_record_writes_the_entries(sessionmaker) -> None:
    await events.record(
        sessionmaker,
        events.Entry(events.COMMAND, data={"name": "today"}),
        events.Entry(events.DIGEST_ITEM, source="digest", item_id=7, occurrence_id=9, data={"digest": "morning"}),
    )
    rows = await _events(sessionmaker)
    assert [(e.type, e.source, e.item_id, e.occurrence_id, e.data, e.created_at) for e in rows] == [
        ("command", None, None, None, {"name": "today"}, T0),
        ("digest_item", "digest", 7, 9, {"digest": "morning"}, T0),
    ]


async def test_no_data_is_sql_null(sessionmaker) -> None:
    await events.record(sessionmaker, events.Entry(events.BUTTON, item_id=0))
    async with sessionmaker() as s:
        row = (await s.execute(text("SELECT data IS NULL, item_id FROM events"))).one()
    assert tuple(row) == (True, None)  # 0 is no id


async def test_the_source_of_a_press_is_the_purpose_of_its_message(sessionmaker) -> None:
    async with sessionmaker() as s:
        s.add(BotMessage(chat_id=1, message_id=50, purpose="reminder"))
        await s.commit()
    await events.record(
        sessionmaker,
        events.Entry(events.BUTTON, occurrence_id=3, data={"action": "o:done"}, message=(1, 50)),
        events.Entry(events.BUTTON, data={"action": "s:menu"}, message=(1, 51)),  # not recorded: no source
        events.Entry(events.BUTTON, source="card", message=(1, 50)),  # an explicit source wins
    )
    assert [(e.source, e.occurrence_id) for e in await _events(sessionmaker)] == [
        ("reminder", 3),
        (None, None),
        ("card", None),
    ]


async def test_an_occurrence_names_its_item(sessionmaker) -> None:
    async with sessionmaker() as s:
        item = Item(kind="task", title="Купить хлеб")
        s.add(item)
        await s.flush()
        occ = Occurrence(item_id=item.id, due_at=T0, precision="exact")
        s.add(occ)
        await s.commit()
    await events.record(sessionmaker, events.Entry(events.BUTTON, occurrence_id=occ.id, data={"action": "o:s60"}))
    [row] = await _events(sessionmaker)
    assert (row.item_id, row.occurrence_id) == (item.id, occ.id)


async def test_long_codes_are_clipped(sessionmaker) -> None:
    await events.record(sessionmaker, events.Entry("x" * 40, source="y" * 40))
    [row] = await _events(sessionmaker)
    assert (row.type, row.source) == ("x" * 16, "y" * 16)


async def test_a_failed_write_is_only_logged(caplog) -> None:
    def broken():
        raise ConnectionRefusedError("the database is down")

    caplog.set_level(logging.WARNING, logger="dumpbot.services.events")
    await events.record(broken, events.Entry(events.COMMAND))  # type: ignore[arg-type]
    assert "could not write 1 journal event(s) (command): ConnectionRefusedError" in caplog.text


async def test_nothing_to_write(sessionmaker) -> None:
    await events.record(sessionmaker)
    assert await _events(sessionmaker) == []
