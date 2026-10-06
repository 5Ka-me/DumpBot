"""create_items / replace_items_for_raw / snapshot and the small read helpers of services.items."""

from __future__ import annotations

from datetime import datetime, time

import pytest

from dumpbot.ai.schemas import ExtractedItem, Extraction
from dumpbot.db.models import Item
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items


async def test_purchase_at_lunch_gets_1255_reminder(make, reminders_of, local, now):
    [c] = await make(
        {
            "kind": "purchase",
            "title": "Купить наушники",
            "tags": ["электроника"],
            "when": {"text": "в обед", "time": "lunch"},
        },
        now=now,
        source_text="в обед купить наушники",
    )
    assert c.item.kind == "purchase"
    assert c.item.status == "open"
    assert c.item.tags == ["электроника"]
    assert c.item.created_at == now
    assert c.item.ai_model == "test-model"
    assert c.item.prompt_version == "v1"
    assert c.item.when_spec == {"text": "в обед", "time": "lunch", "role": "event"}
    assert c.occurrence is not None
    assert c.occurrence.due_at == local(2026, 10, 6, 13, 0)
    assert c.occurrence.precision == "slot"
    assert c.resolution.precision == "slot"
    assert [(r.fire_at, r.lead_minutes, r.auto, r.status) for r in c.reminders] == [
        (local(2026, 10, 6, 12, 55), 5, False, "pending")
    ]
    assert await reminders_of(c.occurrence.id) == [(local(2026, 10, 6, 12, 55), "pending", 5, False)]


async def test_task_without_time_gets_19_auto_reminder(make, reminders_of, local, now):
    [c] = await make({"kind": "task", "title": "Позвонить в банк"}, now=now)
    assert c.occurrence.due_at == local(2026, 10, 6, 19, 0)
    assert c.occurrence.precision == "default"
    assert await reminders_of(c.occurrence.id) == [(local(2026, 10, 6, 19, 0), "pending", 0, True)]


async def test_task_without_time_after_19_goes_to_tomorrow(make, reminders_of, local):
    [c] = await make({"kind": "task", "title": "Позвонить в банк"}, now=local(2026, 10, 6, 20, 15))
    assert c.occurrence.due_at == local(2026, 10, 7, 19, 0)
    assert await reminders_of(c.occurrence.id) == [(local(2026, 10, 7, 19, 0), "pending", 0, True)]


async def test_default_reminder_is_shifted_out_of_quiet_hours(make, reminders_of, local, now):
    late = Prefs(tz="Europe/Warsaw", task_default_time=time(23, 30))
    [c] = await make({"kind": "task", "title": "Полить цветы"}, now=now, prefs_=late)
    assert c.occurrence.due_at == local(2026, 10, 6, 23, 30)
    assert await reminders_of(c.occurrence.id) == [(local(2026, 10, 7, 8, 0), "pending", 0, True)]


async def test_explicit_time_in_quiet_hours_is_not_shifted(make, reminders_of, local, now):
    [c] = await make({"kind": "event", "title": "Созвон", "when": {"time": "23:30"}}, now=now)
    assert await reminders_of(c.occurrence.id) == [(local(2026, 10, 6, 23, 25), "pending", 5, False)]


async def test_explicit_lead_overrides_default(make, reminders_of, local, now):
    [c] = await make({"kind": "event", "title": "Врач", "when": {"day": "+1", "time": "15:00"}, "lead": [30]}, now=now)
    assert c.item.lead_minutes == [30]
    assert await reminders_of(c.occurrence.id) == [(local(2026, 10, 7, 14, 30), "pending", 30, False)]


async def test_note_without_time_has_no_occurrence(make, now):
    [c] = await make({"kind": "note", "title": "Мысль про отпуск"}, now=now)
    assert c.occurrence is None
    assert c.reminders == []
    assert c.resolution.precision == "none"


async def test_idea_with_time_gets_occurrence(make, local, now):
    [c] = await make({"kind": "idea", "title": "Подумать о курсе", "when": {"day": "+1"}}, now=now)
    assert c.occurrence.due_at == local(2026, 10, 7, 10, 0)
    assert [(r.fire_at, r.auto) for r in c.reminders] == [(local(2026, 10, 7, 10, 0), True)]


async def test_multi_item_extraction(make, new_raw, local, now):
    raw = await new_raw("молоко в обед, идея про бота, и статья https://a.example/x")
    created = await make(
        {"kind": "purchase", "title": "Купить молоко", "when": {"time": "lunch"}},
        {"kind": "idea", "title": "Бот для заметок"},
        {"kind": "link", "title": "Статья про Postgres"},
        now=now,
        raw=raw,
        urls=("https://a.example/x",),
        source_text="молоко в обед, идея про бота, и статья https://a.example/x",
    )
    assert [c.item.kind for c in created] == ["purchase", "idea", "link"]
    assert all(c.item.raw_message_id == raw.id for c in created)
    assert all(c.item.source_kind == "text" for c in created)
    assert [c.item.urls for c in created] == [[], [], ["https://a.example/x"]]
    assert created[0].occurrence.due_at == local(2026, 10, 6, 13, 0)
    assert created[1].occurrence is None
    assert created[2].occurrence is None
    assert len({c.item.id for c in created}) == 3


async def test_multi_item_url_mentioned_in_item_text_goes_to_that_item(make, now):
    created = await make(
        {"kind": "task", "title": "Прочитать https://b.example/doc"},
        {"kind": "link", "title": "Видео"},
        now=now,
        urls=("https://b.example/doc", "https://c.example/v"),
    )
    assert created[0].item.urls == ["https://b.example/doc"]
    assert created[1].item.urls == ["https://b.example/doc", "https://c.example/v"]


async def test_single_item_gets_all_message_urls(make, now):
    [c] = await make({"kind": "task", "title": "Прочитать статью"}, now=now, urls=("https://a.example/1",))
    assert c.item.urls == ["https://a.example/1"]


async def test_recurring_weekly_creation(make, reminders_of, local, now):
    [c] = await make(
        {
            "kind": "task",
            "title": "Вынести мусор",
            "rrule": "FREQ=WEEKLY;BYDAY=MO",
            "when": {"text": "по понедельникам в 9", "time": "09:00"},
        },
        now=now,
    )
    item = c.item
    assert item.rrule == "FREQ=WEEKLY;BYDAY=MO"
    assert item.rec_active is True
    assert item.rec_tz == "Europe/Warsaw"
    assert item.rec_time_local == time(9, 0)
    assert item.rec_dtstart_local == datetime(2026, 10, 6, 9, 0)
    assert c.occurrence.due_at == local(2026, 10, 12, 9, 0)
    assert c.occurrence.precision == "exact"
    assert await reminders_of(c.occurrence.id) == [(local(2026, 10, 12, 8, 55), "pending", 5, False)]


async def test_invalid_rrule_creates_one_off_with_warning(make, now):
    [c] = await make({"kind": "task", "title": "Что-то", "rrule": "FREQ=DAILY;FOO=1"}, now=now)
    assert c.item.rrule is None
    assert c.item.rec_active is False
    assert c.item.needs_confirm is True
    assert "не понял правило повтора" in c.resolution.warnings


async def test_photo_with_time_needs_confirm(make, now):
    concert = {"kind": "event", "title": "Концерт", "when": {"day": "+3", "time": "20:00"}}
    [c] = await make(concert, now=now, source="photo")
    assert c.item.needs_confirm is True
    assert c.item.source_kind == "photo"


async def test_replace_items_for_raw(session, make, new_raw, reminders_of, local, now):
    raw = await new_raw("купить хлеб в 8")
    [old] = await make({"kind": "purchase", "title": "Купить хлеб", "when": {"time": "08:00"}}, now=now, raw=raw)

    created = await items.replace_items_for_raw(
        session,
        raw.id,
        extraction=Extraction(
            items=[
                ExtractedItem(kind="purchase", title="Купить хлеб", when={"time": "20:00"}),
                ExtractedItem(kind="note", title="Хлеб — ржаной"),
            ]
        ),
        source="text",
        source_text="купить хлеб в 8",
        source_meta=None,
        urls=[],
        prefs=Prefs(),
        now=now,
        model="m",
        prompt_version="v1",
    )
    await session.commit()

    assert old.item.status == "deleted"
    assert old.item.deleted_at == now
    assert await reminders_of(old.occurrence.id) == [(local(2026, 10, 7, 7, 55), "cancelled", 5, False)]
    assert [c.item.raw_message_id for c in created] == [raw.id, raw.id]
    assert created[0].occurrence.due_at == local(2026, 10, 6, 20, 0)
    assert created[1].occurrence is None


async def test_snapshot_and_reads(session, make, local, now, prefs):
    [c] = await make(
        {"kind": "event", "title": "Встреча", "details": "в офисе", "tags": ["работа"], "when": {"time": "16:00"}},
        now=now,
    )
    snap = await items.item_snapshot(session, c.item.id, prefs, now)
    assert snap == {
        "id": c.item.id,
        "kind": "event",
        "title": "Встреча",
        "details": "в офисе",
        "tags": ["работа"],
        "due_local": "2026-10-06 16:00",
        "rrule": None,
        "lead": None,
        "status": "open",
    }
    assert (await items.get_item(session, c.item.id)).title == "Встреча"
    assert await items.get_item(session, 999_999) is None
    assert (await items.current_occurrence(session, c.item.id)).id == c.occurrence.id
    assert [r.fire_at for r in await items.pending_reminders(session, c.occurrence.id)] == [local(2026, 10, 6, 15, 55)]


async def test_snapshot_of_note_without_due(session, make, prefs, now):
    [c] = await make({"kind": "note", "title": "Заметка"}, now=now)
    snap = await items.item_snapshot(session, c.item.id, prefs, now)
    assert snap["due_local"] is None
    assert await items.current_occurrence(session, c.item.id) is None


async def test_current_occurrence_prefers_earliest_open_then_latest(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Зарядка", "rrule": "FREQ=DAILY", "when": {"time": "09:00"}}, now=now)
    first = c.occurrence
    await items.mark_done(session, first.id, local(2026, 10, 7, 9, 5), prefs)
    nxt = await items.current_occurrence(session, c.item.id)
    assert nxt.id != first.id
    assert nxt.status == "open"
    assert nxt.due_at == local(2026, 10, 8, 9, 0)
    await items.stop_recurrence(session, c.item.id)
    await items.mark_done(session, nxt.id, local(2026, 10, 8, 9, 5), prefs)
    assert (await items.current_occurrence(session, c.item.id)).id == nxt.id  # latest, all done


async def test_unknown_ids_raise_not_found(session, prefs, now):
    with pytest.raises(items.NotFoundError):
        await items.mark_done(session, 123456, now, prefs)
    with pytest.raises(items.NotFoundError):
        await items.delete_item(session, 123456, now)
    assert isinstance(items.NotFoundError("x"), LookupError)


async def test_created_item_is_persisted(session, make, now):
    [c] = await make({"kind": "quote", "title": "Цитата дня"}, now=now)
    session.expunge_all()
    item = await session.get(Item, c.item.id)
    assert item is not None
    assert item.kind == "quote"
    assert item.urls == []
