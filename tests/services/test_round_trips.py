"""Queries of the services cut to fewer round trips (docs/features/iteration-4.md §7): the related candidates in one
statement (with the message's subject lexemes worked out inside it), one INSERT per table for all items of a message,
the edit prompt's snapshot in one query, card extras without the transcript the card does not show."""

from __future__ import annotations

from sqlalchemy import select

from dumpbot.db import meter
from dumpbot.services import items, query
from dumpbot.services.related import message_words, subject_lexeme_array, subject_lexemes

PHRASES = (
    "Напомни часов в 17 прогуглить про визу в черногорию",
    "Купить наушники Sony",
    "завтра в 10 позвонить маме",
    "напомни через час покушать",
    "Подарок для Марты на 8 марта",
    "iPhone15 и pi5, ещё ёлка",
    "в 15:00 & | ! ( ) :*",
    "кто-то сказал «привет»: don't panic",
)


def kinds(m: meter.Meter) -> list[str]:
    """The statements of the trace (the session's BEGIN / COMMIT, and the ROLLBACK of a new connection's set-up, left
    out)."""
    return [c.what for c in m.trace or [] if c.what not in ("BEGIN", "COMMIT", "ROLLBACK")]


async def test_the_lexeme_array_is_what_subject_lexemes_returns(session):
    for phrase in PHRASES:
        words = message_words(phrase)
        in_sql = (await session.execute(select(subject_lexeme_array(words)))).scalar_one() if words else []
        assert in_sql == await subject_lexemes(session, phrase), phrase


async def test_related_candidates_take_one_statement(session, engine, make, now):
    meter.install(engine)
    await make({"kind": "purchase", "title": "Купить наушники Sony"}, {"kind": "note", "title": "Погода"}, now=now)
    with meter.measure(trace=True) as m:
        rows = await query.related_candidates(session, "ещё одни наушники", now)
    assert [r.title for r in rows] == ["Купить наушники Sony"]
    assert kinds(m) == ["WITH"]
    with meter.measure(trace=True) as m:
        assert await query.related_candidates(session, "напомни завтра в 10", now) == []
    assert kinds(m) == ["WITH"]  # generic words only: the same one statement finds no lexeme
    with meter.measure(trace=True) as m:
        assert await query.related_candidates(session, "в 15:00 & !", now) == []
    assert kinds(m) == []  # no word at all: no query


async def test_all_items_of_a_message_go_in_one_insert_per_table(session, engine, make, now, reminders_of):
    meter.install(engine)
    when = {"text": "в 18", "time": "18:00"}
    with meter.measure(trace=True) as m:
        created = await make(
            {"kind": "task", "title": "Купить молоко", "when": when},
            {"kind": "task", "title": "Купить хлеб", "when": when},
            {"kind": "note", "title": "Хлеб был чёрствый"},
            now=now,
        )
    assert kinds(m)[:3] == ["INSERT items", "INSERT occurrences", "INSERT reminders"]
    assert kinds(m).count("INSERT items") == 1
    milk, bread, note = created
    assert [c.item.title for c in created] == ["Купить молоко", "Купить хлеб", "Хлеб был чёрствый"]
    assert milk.occurrence is not None and bread.occurrence is not None and note.occurrence is None
    assert milk.occurrence.item_id == milk.item.id and bread.occurrence.item_id == bread.item.id
    assert [r.occurrence_id for r in milk.reminders] == [milk.occurrence.id]
    assert len(await reminders_of(bread.occurrence.id)) == 1 and note.reminders == []
    assert (milk.steps, milk.related) == ([], [])


async def test_steps_come_with_their_parent_in_one_insert(session, make, now):
    [parent] = await make(
        {"kind": "task", "title": "Переезд", "steps": [{"title": "Коробки"}, {"title": "Машина"}]}, now=now
    )
    assert [s.item.title for s in parent.steps] == ["Коробки", "Машина"]
    assert {s.item.parent_id for s in parent.steps} == {parent.item.id}
    assert [s.item.position for s in parent.steps] == [1, 2]


async def test_the_snapshot_is_one_query(session, engine, make, now, prefs):
    meter.install(engine)
    [c] = await make({"kind": "task", "title": "Купить молоко", "when": {"text": "в 18", "time": "18:00"}}, now=now)
    session.expunge_all()
    with meter.measure(trace=True) as m:
        snap = await items.item_snapshot(session, c.item.id, prefs, now)
    assert kinds(m) == ["SELECT"]
    assert (snap["title"], snap["due_local"]) == ("Купить молоко", "2026-10-06 18:00")
    [note] = await make({"kind": "note", "title": "Без даты"}, now=now)
    assert (await items.item_snapshot(session, note.item.id, prefs, now))["due_local"] is None


async def test_card_extras_without_the_transcript(session, engine, make, now, new_raw):
    meter.install(engine)
    raw = await new_raw("купить молоко")
    [c] = await make({"kind": "task", "title": "Купить молоко"}, now=now, raw=raw)
    with meter.measure(trace=True) as m:
        await query.card_extras(session, c.item)
    assert len(kinds(m)) == 3
    with meter.measure(trace=True) as m:
        extras = await query.card_extras(session, c.item, with_heard=False)
    assert extras.heard is None and kinds(m) == ["SELECT", "SELECT"]  # the checklist and the related items
