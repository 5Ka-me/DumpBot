"""One item through the Mini App API against the DB: the detail, PATCH, the time (picker and AI job), the actions,
the split job and the checklist (add / rename / time / delete / toggle, 🎉 when the last step closes the parent).
The AI jobs run through the real handlers with FakeAI (conftest)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from dumpbot.db.models import Item, Occurrence, Reminder
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items

TOMORROW_13 = {"text": "завтра в 13", "day": "+1", "time": "13:00"}
DAILY_9 = {"kind": "task", "title": "таблетки", "when": {"text": "в 9", "time": "09:00"}, "rrule": "FREQ=DAILY"}
WARSAW = ZoneInfo("Europe/Warsaw")


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment of October 2026 in Warsaw, as UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=WARSAW).astimezone(UTC)


async def item_of(db, item_id: int) -> Item:
    [item] = await db(select(Item).where(Item.id == item_id))
    return item


# --- detail --------------------------------------------------------------------------------------------------------


async def test_detail_of_a_timed_task(api, make_items) -> None:
    [created] = await make_items(
        {
            "kind": "task",
            "title": "Купить наушники",
            "details": "Sony XM5",
            "tags": ["электроника"],
            "when": TOMORROW_13,
        }
    )
    reply = await api.get(f"/api/items/{created.item.id}")
    assert reply.status == 200
    item = reply.data
    assert item["id"] == created.item.id and item["kind"] == "task" and item["title"] == "Купить наушники"
    assert item["details"] == "Sony XM5" and item["tags"] == ["электроника"] and item["status"] == "open"
    assert item["occurrence"]["due_at"] == "2026-10-07T11:00:00Z"
    assert item["occurrence"]["due_local"] == "2026-10-07T13:00"
    assert item["occurrence"]["precision"] == "exact" and item["occurrence"]["status"] == "open"
    [reminder] = item["reminders"]  # the item's time is its notification time: no "за 5 минут"
    assert reminder["fire_at"] == "2026-10-07T11:00:00Z" and reminder["lead_minutes"] == 0
    assert item["when_text"] == "🔔 завтра 13:00"
    assert "lead_minutes" not in item and item["reminders_off"] is False
    assert item["can_split"] is True and item["split_pending"] is False
    assert item["steps"] == [] and item["related"] == [] and item["images"] == [] and item["parent"] is None
    assert item["source"] == {
        "kind": "text",
        "text": None,
        "forward": None,
        "raw_id": None,
        "from_app": False,
        "message_date": None,
    }
    assert item["role"] == "event" and item["rrule"] is None and item["rec_active"] is False


async def test_unknown_item_is_404(api) -> None:
    for path in ["/api/items/999", "/api/items/0", "/api/items/x"]:
        reply = await api.get(path)
        assert reply.status == 404 and reply.data["error"]["code"] == "not_found"


async def test_detail_with_a_forward_source_and_pictures(api, add_raw, make_items) -> None:
    raw = await add_raw(
        message_id=50,
        content_type="photo",
        file_id="ph-big",
        file_unique_id="u-ph",
        width=800,
        height=600,
        media_group_id="g1",
        forward_origin={"type": "channel", "title": "Хабр", "username": "habr", "url": "https://t.me/habr/7"},
    )
    await add_raw(
        message_id=51, content_type="image_document", file_id="doc", mime_type="image/png", media_group_id="g1"
    )
    await add_raw(
        message_id=52, content_type="image_document", file_id="svg", mime_type="image/svg+xml", media_group_id="g1"
    )
    [created] = await make_items({"kind": "meme", "title": "Мем"}, raw_id=raw.id)
    async with items_session(api) as session:
        item = await session.get(Item, created.item.id)
        item.source = {"forward_origin": raw.forward_origin}
        await session.commit()

    data = (await api.get(f"/api/items/{created.item.id}")).data
    assert data["source"]["forward"] == {"type": "channel", "name": "Хабр", "url": "https://t.me/habr/7", "date": None}
    assert data["source"]["raw_id"] == raw.id and data["source"]["message_date"] == "2026-10-06T08:00:00Z"
    assert data["images"] == [
        {"n": 0, "url": f"/api/files/{created.item.id}/0", "mime_type": "image/jpeg", "width": 800, "height": 600},
        {"n": 1, "url": f"/api/files/{created.item.id}/1", "mime_type": "image/png", "width": None, "height": None},
    ]
    feed = (await api.get("/api/feed")).data["rows"]
    assert feed[0]["images"] == 2


def items_session(api):
    from dumpbot.web.http import CTX

    return api.client.app[CTX].sessionmaker()


# --- PATCH ---------------------------------------------------------------------------------------------------------


async def test_patch_fields(api, make_items, db) -> None:
    [created] = await make_items({"kind": "idea", "title": "Идея"})
    reply = await api.patch(
        f"/api/items/{created.item.id}",
        {"title": "  Новая   идея ", "details": "подробно", "tags": ["#Важное", "дом"], "kind": "task"},
    )
    assert reply.status == 200, reply.data
    assert reply.data["message"] == "✅ Сохранено"
    item = reply.data["item"]
    assert item["title"] == "Новая идея" and item["details"] == "подробно" and item["tags"] == ["важное", "дом"]
    assert item["kind"] == "task"
    # An idea that became a task gets the default 19:00 reminder, like the bot's category menu.
    assert item["occurrence"]["precision"] == "default" and item["occurrence"]["due_local"] == "2026-10-06T19:00"

    reply = await api.patch(f"/api/items/{created.item.id}", {"details": None})
    assert reply.data["item"]["details"] is None


async def test_patch_lead_is_gone(api, make_items) -> None:
    # "напомнить за" was removed (2026-10-07): the item's time is its notification time.
    [created] = await make_items({"kind": "task", "title": "Созвон", "when": TOMORROW_13})
    for lead in ([30], None):
        reply = await api.patch(f"/api/items/{created.item.id}", {"lead": lead})
        assert (reply.status, reply.data["error"]["field"]) == (400, "lead")


async def test_patch_period_and_someday(api, make_items) -> None:
    [created] = await make_items({"kind": "task", "title": "Шины", "when": TOMORROW_13})
    path = f"/api/items/{created.item.id}"
    item = (await api.patch(path, {"period": "next_week"})).data["item"]
    assert item["period"]["kind"] == "week" and item["period"]["label"] == "на следующей неделе"
    assert item["occurrence"]["precision"] == "period"

    item = (await api.patch(path, {"someday": True})).data["item"]
    assert item["someday"] is True and item["occurrence"] is None and item["reminders"] == []
    assert item["when_text"] == "🌙 когда-нибудь"
    tasks = (await api.get("/api/tasks")).data["rows"]
    assert tasks == []

    item = (await api.patch(path, {"someday": False})).data["item"]
    assert item["someday"] is False and item["occurrence"]["precision"] == "default"
    assert [r["id"] for r in (await api.get("/api/tasks")).data["rows"]] == [created.item.id]


async def test_period_chip_on_a_deadline_item_sets_the_span(api, make_items) -> None:
    """«Сдать отчёт до пятницы» → chip «на следующей неделе»: that span (🗓, its reminders, the digest section), as
    the bot's reply «перенеси на следующую неделю» does, not «⏳ до пт 16 окт»."""
    deadline = {"text": "до пятницы", "day": "FR", "role": "deadline"}
    [created] = await make_items({"kind": "task", "title": "Сдать отчёт", "when": deadline})
    path = f"/api/items/{created.item.id}"
    assert (await api.get(path)).data["role"] == "deadline"

    item = (await api.patch(path, {"period": "next_week"})).data["item"]
    assert item["period"]["kind"] == "week" and item["period"]["label"] == "на следующей неделе"
    assert item["occurrence"]["precision"] == "period" and item["role"] == "event"
    assert item["when_text"].startswith("🗓 на следующей неделе (до вс 18 окт)")


async def test_patch_validation(api, make_items) -> None:
    [created] = await make_items({"kind": "task", "title": "Задача"})
    path = f"/api/items/{created.item.id}"
    for body, field in [
        ({"title": "   "}, "title"),
        ({"title": "x" * 201}, "title"),
        ({"kind": "car"}, "kind"),
        ({"tags": "дом"}, "tags"),
        ({"lead": [5, -1]}, "lead"),
        ({"lead": [1, 2, 3, 4]}, "lead"),
        ({"period": "tomorrow"}, "period"),
        ({"someday": "yes"}, "someday"),
        ({"period": "soon", "someday": True}, "someday"),
        ({"period": "soon\n"}, "period"),  # `$` of a plain re.match would let the newline through
        ({"title": "a\u0000b"}, "title"),  # Postgres text refuses NUL
        ({"details": "a\u0000b"}, "details"),
        ({"tags": ["a\u0000b"]}, "tags"),
        ({"colour": "red"}, "colour"),
    ]:
        reply = await api.patch(path, body)
        assert reply.status == 400, body
        assert reply.data["error"]["field"] == field
    assert (await api.patch(path, {})).status == 400
    assert (await api.call("PATCH", path, auth_header=None)).status == 400  # no body
    async with api.client.patch(
        path, data=b"not json", headers={"Authorization": f"tma {api.init_data()}"}
    ) as response:
        assert response.status == 400
    # nothing was applied
    assert (await api.get(path)).data["title"] == "Задача"


# --- time ----------------------------------------------------------------------------------------------------------


async def test_when_from_the_picker(api, make_items) -> None:
    [created] = await make_items({"kind": "task", "title": "Созвон", "when": TOMORROW_13})
    path = f"/api/items/{created.item.id}/when"
    reply = await api.post(path, {"due_local": "2026-10-09T15:30"})
    assert reply.status == 200 and reply.data["message"] == "🕒 Время изменено"
    item = reply.data["item"]
    assert item["occurrence"]["due_local"] == "2026-10-09T15:30" and item["occurrence"]["precision"] == "exact"
    assert [r["fire_at"] for r in item["reminders"]] == ["2026-10-09T13:30:00Z"]  # the picker sets the notification
    assert item["needs_confirm"] is False and item["warnings"] == []

    item = (await api.post(path, {"due_local": "2026-10-12"})).data["item"]
    assert item["occurrence"]["due_local"] == "2026-10-12T10:00" and item["occurrence"]["precision"] == "date"

    past = (await api.post(path, {"due_local": "2026-09-01T10:00"})).data["item"]
    assert past["warnings"] and past["reminders"] == []

    for body in [{"due_local": "9 октября"}, {"due_local": "2026-13-01"}, {}, {"text": "x", "due_local": "2026-10-09"}]:
        assert (await api.post(path, body)).status == 400
    # 0001-01-01 00:00 in Warsaw is before datetime.min in UTC; a trailing newline is not a date
    for value in ["0001-01-01T00:00", "0001-01-01", "1969-12-31", "9999-12-31T23:59", "2026-10-09\n"]:
        reply = await api.post(path, {"due_local": value})
        assert (reply.status, reply.data["error"]["field"]) == (400, "due_local"), value
    step = await api.post(f"/api/items/{created.item.id}/steps", {"title": "Шаг", "due_local": "0001-01-01T00:00"})
    assert (step.status, step.data["error"]["field"]) == (400, "due_local")
    step = await api.post(f"/api/items/{created.item.id}/steps", {"title": "a\u0000b"})
    assert (step.status, step.data["error"]["field"]) == (400, "title")


async def test_when_by_text_runs_the_edit_time_job(api, make_items, fake_ai, run_jobs, jobs_all, bot) -> None:
    [created] = await make_items({"kind": "task", "title": "Созвон", "when": TOMORROW_13})
    reply = await api.post(f"/api/items/{created.item.id}/when", {"text": "в пятницу в 18"})
    assert reply.status == 202 and reply.data["pending"] is False
    job_id = reply.data["job_id"]
    [job] = await jobs_all()
    assert job.kind == "edit_time" and job.payload["app"] is True and job.payload["text"] == "в пятницу в 18"

    fake_ai.push({"intent": "edit", "edit": {"when": {"text": "в пятницу в 18", "day": "FR", "time": "18:00"}}})
    assert await run_jobs() == 1
    status = (await api.get(f"/api/jobs/{job_id}")).data
    assert status["status"] == "done"
    assert status["result"] == {"ok": True, "reason": None, "message": None, "item_id": created.item.id, "warnings": []}
    assert fake_ai.inputs[0].text == "перенеси на: в пятницу в 18"
    item = (await api.get(f"/api/items/{created.item.id}")).data
    assert item["occurrence"]["due_local"] == "2026-10-09T18:00"
    assert bot.texts == []  # the app shows it, not the chat


async def test_when_by_text_not_understood(api, make_items, fake_ai, run_jobs, bot) -> None:
    [created] = await make_items({"kind": "task", "title": "Созвон", "when": TOMORROW_13})
    job_id = (await api.post(f"/api/items/{created.item.id}/when", {"text": "когда удобно"})).data["job_id"]
    fake_ai.push({"intent": "edit", "edit": {"title": "Созвон"}})
    await run_jobs()
    result = (await api.get(f"/api/jobs/{job_id}")).data["result"]
    assert result["ok"] is False and result["reason"] == "not_understood"
    assert result["message"].startswith("🤷 Не понял время")
    assert bot.texts == [] and bot.calls_of("set_message_reaction") == []


# --- actions -------------------------------------------------------------------------------------------------------


async def test_done_and_undone(api, make_items, ctx) -> None:
    [created] = await make_items({"kind": "task", "title": "Купить молоко", "when": TOMORROW_13})
    path = f"/api/items/{created.item.id}"
    ctx.scheduler_wakeup.clear()
    reply = await api.post(f"{path}/done")
    assert reply.data["message"] == "✅ Готово"
    assert reply.data["item"]["status"] == "done" and reply.data["item"]["reminders"] == []
    assert ctx.scheduler_wakeup.is_set()
    reply = await api.post(f"{path}/undone")
    assert reply.data["message"] == "↩️ Не сделано"
    assert reply.data["item"]["status"] == "open" and len(reply.data["item"]["reminders"]) == 1


async def test_done_of_a_series_names_the_next_time(api, make_items) -> None:
    [created] = await make_items(
        {"kind": "task", "title": "Зарядка", "when": {"text": "в 9", "time": "09:00"}, "rrule": "FREQ=DAILY"}
    )
    reply = await api.post(f"/api/items/{created.item.id}/done")
    assert reply.data["message"] == "✅ Готово · следующий раз чт 8 окт 09:00"
    assert reply.data["item"]["status"] == "open" and reply.data["item"]["rec_active"] is True


async def _tick(sessionmaker, now: datetime) -> None:
    """The scheduler's recurrence tick: the next instance of a due series, superseded ones -> missed."""
    async with sessionmaker() as session:
        await items.ensure_recurring(session, Prefs(), now)
        await session.commit()


async def _occurrences(db, item_id: int) -> list[tuple[datetime, str]]:
    occs = await db(select(Occurrence).where(Occurrence.item_id == item_id).order_by(Occurrence.due_at))
    return [(o.due_at, o.status) for o in occs]


async def _pending(db, item_id: int) -> list[datetime]:
    stmt = (
        select(Reminder.fire_at)
        .join(Occurrence, Occurrence.id == Reminder.occurrence_id)
        .where(Occurrence.item_id == item_id, Reminder.status == "pending")
        .order_by(Reminder.fire_at)
    )
    return await db(stmt)


async def test_a_row_closes_its_own_missed_instance_of_a_series(api, make_items, frozen, sessionmaker, db) -> None:
    """Wed 21:31 (the evening digest): this morning's 09:00 is missed, Thursday's is open with its reminder. The
    ☐ of Wednesday's row in «Сегодня» closes Wednesday — not Thursday, whose reminder stays."""
    [created] = await make_items(DAILY_9)
    item_id = created.item.id
    frozen.now = local(7, 21, 31)
    await _tick(sessionmaker, frozen.now)
    assert await _occurrences(db, item_id) == [(local(7, 9), "missed"), (local(8, 9), "open")]
    assert await _pending(db, item_id) == [local(8, 9)]

    [row] = [r for r in (await api.get("/api/today")).data["open"] if r["id"] == item_id]
    assert row["occ_status"] == "missed" and row["when_text"] == "сегодня 09:00"
    reply = await api.post(f"/api/items/{item_id}/done", {"occurrence_id": row["occurrence_id"]})

    assert reply.status == 200
    assert reply.data["message"] == "✅ Готово · следующий раз завтра 09:00"
    assert await _occurrences(db, item_id) == [(local(7, 9), "done"), (local(8, 9), "open")]
    assert await _pending(db, item_id) == [local(8, 9)]  # Thursday's reminder is still coming
    today = (await api.get("/api/today")).data
    assert [r["id"] for r in today["open"]] == [] and [r["id"] for r in today["done"]] == [item_id]


async def test_a_week_row_closes_that_day_not_an_earlier_open_one(api, make_items, frozen, sessionmaker, db) -> None:
    """Wed 15:00: this morning's instance is still open; ticking Thursday's row of «7 дней» closes Thursday."""
    [created] = await make_items(DAILY_9)
    item_id = created.item.id
    frozen.now = local(7, 15)
    await _tick(sessionmaker, frozen.now)
    week = (await api.get("/api/week")).data["days"]
    [thursday] = [r for r in week[1]["open"] if r["id"] == item_id]

    reply = await api.post(f"/api/items/{item_id}/done", {"occurrence_id": thursday["occurrence_id"]})

    assert reply.status == 200 and reply.data["message"].startswith("✅ Готово")
    assert await _occurrences(db, item_id) == [(local(7, 9), "open"), (local(8, 9), "done"), (local(9, 9), "open")]


async def test_undone_of_a_row_reopens_that_occurrence(api, make_items, frozen, sessionmaker, db) -> None:
    [created] = await make_items(DAILY_9)
    item_id = created.item.id
    frozen.now = local(7, 15)
    await _tick(sessionmaker, frozen.now)
    [wed, thu] = await db(select(Occurrence.id).where(Occurrence.item_id == item_id).order_by(Occurrence.due_at))
    await api.post(f"/api/items/{item_id}/done", {"occurrence_id": thu})
    frozen.advance(minutes=1)
    await api.post(f"/api/items/{item_id}/done", {"occurrence_id": wed})  # the latest done one now

    reply = await api.post(f"/api/items/{item_id}/undone", {"occurrence_id": thu})

    assert reply.status == 200 and reply.data["message"] == "↩️ Не сделано"
    assert await _occurrences(db, item_id) == [(local(7, 9), "done"), (local(8, 9), "open"), (local(9, 9), "open")]


async def test_occurrence_id_must_be_the_items_own(api, make_items, db) -> None:
    [first, second] = await make_items(
        {"kind": "task", "title": "Купить молоко", "when": TOMORROW_13},
        {"kind": "task", "title": "Позвонить", "when": TOMORROW_13},
    )
    [other] = await db(select(Occurrence.id).where(Occurrence.item_id == second.item.id))
    path = f"/api/items/{first.item.id}"
    for action in ("done", "undone"):
        reply = await api.post(f"{path}/{action}", {"occurrence_id": other})
        assert (reply.status, reply.data["error"]["code"]) == (404, "not_found"), action
        for body in [{"occurrence_id": "5"}, {"occurrence_id": True}, {"occurrence_id": 0}, {"occurrence_id": 2**63}]:
            reply = await api.post(f"{path}/{action}", body)
            assert (reply.status, reply.data["error"]["field"]) == (400, "occurrence_id"), (action, body)
        assert (await api.post(f"{path}/{action}", {"minutes": 5})).data["error"]["field"] == "minutes"
    assert (await item_of(db, second.item.id)).status == "open"
    [own] = await db(select(Occurrence.id).where(Occurrence.item_id == first.item.id))
    await api.post(f"{path}/delete")
    reply = await api.post(f"{path}/done", {"occurrence_id": own})
    assert (reply.status, reply.data["error"]["code"]) == (409, "deleted")


async def test_no_reminders_delete_restore_stoprec(api, make_items) -> None:
    [created] = await make_items(
        {"kind": "task", "title": "Планёрка", "when": {"text": "в 9", "time": "09:00"}, "rrule": "FREQ=WEEKLY;BYDAY=MO"}
    )
    path = f"/api/items/{created.item.id}"
    reply = await api.post(f"{path}/norem")
    assert reply.data["message"] == "🔕 Напоминания выключены"
    assert reply.data["item"]["reminders_off"] is True and reply.data["item"]["reminders"] == []

    reply = await api.post(f"{path}/stoprec")
    assert reply.data["message"] == "⏹ Повтор остановлен" and reply.data["item"]["rec_active"] is False

    reply = await api.post(f"{path}/delete")
    assert reply.data["message"] == "🗑 Удалено" and reply.data["item"]["status"] == "deleted"
    assert reply.data["item"]["deleted_at"] == "2026-10-06T08:00:00Z"
    for action in ("done", "undone", "norem", "stoprec", "split"):
        blocked = await api.post(f"{path}/{action}")
        assert (blocked.status, blocked.data["error"]["code"]) == (409, "deleted"), action
    assert (await api.patch(path, {"title": "x"})).status == 409
    assert (await api.post(f"{path}/when", {"text": "завтра"})).status == 409

    reply = await api.post(f"{path}/restore")
    assert reply.data["message"] == "↩️ Восстановлено" and reply.data["item"]["status"] == "open"


async def test_snooze(api, make_items, db) -> None:
    # Yesterday's 13:00, not done: the snooze moves the item's time (2026-10-07), exactly one pending reminder there.
    yesterday = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    [created] = await make_items({"kind": "task", "title": "Позвонить", "when": {"time": "13:00"}}, now=yesterday)
    path = f"/api/items/{created.item.id}/snooze"
    reply = await api.post(path, {"minutes": 15})
    assert reply.data["message"] == "⏰ Напомню сегодня 10:15"
    assert reply.data["item"]["occurrence"]["due_local"] == "2026-10-06T10:15"
    fires = sorted(r.fire_at for r in await db(select(Reminder).where(Reminder.status == "pending")))
    assert [f.isoformat() for f in fires] == ["2026-10-06T08:15:00+00:00"]
    reply = await api.post(path, {"tomorrow": True})
    assert reply.data["message"] == "⏰ Напомню завтра 10:15"
    assert reply.data["item"]["when_text"] == "🔔 завтра 10:15"
    for body in [{}, {"minutes": 0}, {"minutes": 10081}, {"minutes": 5, "tomorrow": True}, {"minutes": "5"}]:
        assert (await api.post(path, body)).status == 400, body


async def test_snooze_never_moves_an_item_earlier(api, make_items, db) -> None:
    # Tomorrow's 13:00 snoozed 15 minutes today (an app opened before the change still shows the chips): one more
    # reminder at 10:15, and the item stays tomorrow at 13:00 with its own.
    [created] = await make_items({"kind": "task", "title": "Позвонить", "when": TOMORROW_13})
    path = f"/api/items/{created.item.id}/snooze"
    reply = await api.post(path, {"minutes": 15})
    assert reply.data["message"] == "⏰ Напомню сегодня 10:15"
    assert reply.data["item"]["occurrence"]["due_local"] == "2026-10-07T13:00"
    fires = sorted(r.fire_at for r in await db(select(Reminder).where(Reminder.status == "pending")))
    assert [f.isoformat() for f in fires] == ["2026-10-06T08:15:00+00:00", "2026-10-07T11:00:00+00:00"]
    reply = await api.post(path, {"tomorrow": True})
    assert reply.data["message"] == "⏰ Напомню завтра 13:00"
    assert reply.data["item"]["when_text"] == "🔔 завтра 13:00"

    [note] = await make_items({"kind": "note", "title": "Заметка"})
    reply = await api.post(f"/api/items/{note.item.id}/snooze", {"minutes": 15})
    assert (reply.status, reply.data["error"]["code"]) == (409, "nothing_to_snooze")


# --- split ---------------------------------------------------------------------------------------------------------


async def test_split_job_gives_the_checklist(api, make_items, fake_ai, run_jobs, jobs_all, bot) -> None:
    [created] = await make_items({"kind": "task", "title": "Переезд", "when": TOMORROW_13})
    path = f"/api/items/{created.item.id}"
    reply = await api.post(f"{path}/split")
    assert reply.status == 202 and reply.data["pending"] is False
    again = await api.post(f"{path}/split")
    assert again.status == 202 and again.data == {"job_id": reply.data["job_id"], "pending": True}
    assert len(await jobs_all()) == 1
    assert (await api.get(path)).data["split_pending"] is True

    fake_ai.splits.append(["Упаковать вещи", "Заказать машину"])
    await run_jobs()
    status = (await api.get(f"/api/jobs/{reply.data['job_id']}")).data
    assert status["result"] == {"ok": True, "reason": None, "message": None, "steps": 2}
    item = (await api.get(path)).data
    assert [(s["position"], s["title"], s["done"]) for s in item["steps"]] == [
        (1, "Упаковать вещи", False),
        (2, "Заказать машину", False),
    ]
    assert item["can_split"] is False and item["split_pending"] is False
    assert bot.texts == []

    blocked = await api.post(f"{path}/split")
    assert (blocked.status, blocked.data["error"]["code"]) == (409, "has_steps")


async def test_split_that_finds_no_steps(api, make_items, fake_ai, run_jobs) -> None:
    [created] = await make_items({"kind": "task", "title": "Купить хлеб"})
    job_id = (await api.post(f"/api/items/{created.item.id}/split")).data["job_id"]
    fake_ai.splits.append([])
    await run_jobs()
    result = (await api.get(f"/api/jobs/{job_id}")).data["result"]
    assert result["ok"] is False and result["reason"] == "no_steps" and result["message"].startswith("🤷")


async def test_split_with_the_budget_spent_queues_nothing(api, make_items, jobs_all) -> None:
    [created] = await make_items({"kind": "task", "title": "Переезд"})
    await api.patch("/api/settings", {"budget_usd": 0})
    reply = await api.post(f"/api/items/{created.item.id}/split")
    assert (reply.status, reply.data["error"]["code"]) == (402, "budget_exhausted")
    assert reply.data["error"]["message"].startswith("⛔ ИИ выключен")
    assert await jobs_all() == []


async def test_split_is_only_for_open_tasks(api, make_items) -> None:
    [idea] = await make_items({"kind": "idea", "title": "Идея"})
    reply = await api.post(f"/api/items/{idea.item.id}/split")
    assert (reply.status, reply.data["error"]["code"]) == (409, "cant_split")


# --- checklist -----------------------------------------------------------------------------------------------------


async def test_steps_add_rename_time_delete(api, make_items, db) -> None:
    [created] = await make_items({"kind": "task", "title": "Переезд"})
    path = f"/api/items/{created.item.id}/steps"
    reply = await api.post(path, {"title": "Упаковать вещи"})
    assert reply.status == 201 and reply.data["message"] == "➕ Шаг добавлен"
    reply = await api.post(path, {"title": "Заказать машину", "due_local": "2026-10-08T15:00"})
    steps = reply.data["item"]["steps"]
    assert [(s["position"], s["title"]) for s in steps] == [(1, "Упаковать вещи"), (2, "Заказать машину")]
    assert steps[0]["due_at"] is None and steps[1]["when_text"] == "чт 8 окт 15:00"
    first, second = steps[0]["id"], steps[1]["id"]

    step = (await api.get(f"/api/items/{second}")).data
    assert step["parent"] == {"id": created.item.id, "title": "Переезд"} and step["position"] == 2

    reply = await api.patch(f"/api/steps/{first}", {"title": "Упаковать книги", "due_local": "2026-10-07T10:00"})
    steps = reply.data["item"]["steps"]
    assert reply.data["item"]["id"] == created.item.id
    assert steps[0]["title"] == "Упаковать книги" and steps[0]["due_at"] == "2026-10-07T08:00:00Z"
    reply = await api.patch(f"/api/steps/{first}", {"due_local": None})
    assert reply.data["item"]["steps"][0]["due_at"] is None

    reply = await api.delete(f"/api/steps/{second}")
    assert reply.data["message"] == "🗑 Шаг удалён"
    assert [s["title"] for s in reply.data["item"]["steps"]] == ["Упаковать книги"]
    # a new step goes after the deleted one's place
    reply = await api.post(path, {"title": "Ключи"})
    assert [(s["position"], s["title"]) for s in reply.data["item"]["steps"]] == [(1, "Упаковать книги"), (2, "Ключи")]
    [new] = await db(select(Item).where(Item.title == "Ключи"))
    assert new.position == 3

    assert (await api.post(path, {"title": " "})).status == 400
    assert (await api.patch(f"/api/steps/{created.item.id}", {"title": "x"})).data["error"]["code"] == "not_a_step"
    assert (await api.patch(f"/api/steps/{first}", {"position": 2})).status == 400


async def test_steps_cannot_go_under_a_step_or_a_series(api, make_items) -> None:
    [series] = await make_items(
        {"kind": "task", "title": "Зарядка", "when": {"text": "в 9", "time": "09:00"}, "rrule": "FREQ=DAILY"}
    )
    reply = await api.post(f"/api/items/{series.item.id}/steps", {"title": "Шаг"})
    assert (reply.status, reply.data["error"]["code"]) == (409, "cant_have_steps")


async def test_checklist_is_capped(api, make_items, monkeypatch) -> None:
    monkeypatch.setattr(items, "MAX_CHECKLIST", 2)
    [created] = await make_items({"kind": "task", "title": "Переезд"})
    path = f"/api/items/{created.item.id}/steps"
    await api.post(path, {"title": "Один"})
    await api.post(path, {"title": "Два"})
    reply = await api.post(path, {"title": "Три"})
    assert (reply.status, reply.data["error"]["code"]) == (409, "checklist_full")


async def test_toggle_closes_and_reopens_the_parent(api, make_items, bot) -> None:
    [created] = await make_items({"kind": "task", "title": "Переезд", "when": TOMORROW_13})
    path = f"/api/items/{created.item.id}/steps"
    await api.post(path, {"title": "Упаковать вещи"})
    steps = (await api.post(path, {"title": "Заказать машину"})).data["item"]["steps"]

    reply = await api.post(f"/api/steps/{steps[0]['id']}/toggle")
    assert reply.data["closed"] is False and reply.data["message"] == "☑ Упаковать вещи"
    assert reply.data["item"]["steps_done"] == 1 and reply.data["item"]["status"] == "open"

    reply = await api.post(f"/api/steps/{steps[1]['id']}/toggle", {"done": True})
    assert reply.data["closed"] is True
    assert reply.data["message"] == "🎉 Все шаги выполнены — «Переезд» закрыт"
    assert reply.data["item"]["status"] == "done" and reply.data["item"]["reminders"] == []
    # the chat gets the bot's 🎉 too, as after the ☑ buttons
    assert bot.texts == ["🎉 Все шаги выполнены — «Переезд» закрыт"]

    reply = await api.post(f"/api/steps/{steps[1]['id']}/toggle", {"done": False})
    assert reply.data["closed"] is False and reply.data["message"] == "☐ Заказать машину"
    assert reply.data["item"]["status"] == "open"
    assert (await api.post(f"/api/steps/{steps[0]['id']}/toggle", {"done": "yes"})).status == 400


async def test_deleting_and_restoring_a_step_moves_the_parent(api, make_items, bot) -> None:
    [created] = await make_items({"kind": "task", "title": "Переезд", "when": TOMORROW_13})
    path = f"/api/items/{created.item.id}/steps"
    await api.post(path, {"title": "Упаковать вещи"})
    steps = (await api.post(path, {"title": "Отдать ключи"})).data["item"]["steps"]
    await api.post(f"/api/steps/{steps[0]['id']}/toggle", {"done": True})

    # 🗑 of the only open step: every step left is ticked, so the parent closes (🎉 here and in the chat)
    reply = await api.delete(f"/api/steps/{steps[1]['id']}")
    assert reply.data["closed"] is True and reply.data["message"] == "🎉 Все шаги выполнены — «Переезд» закрыт"
    assert reply.data["item"]["status"] == "done" and reply.data["item"]["reminders"] == []
    assert bot.texts == ["🎉 Все шаги выполнены — «Переезд» закрыт"]

    # ↩️ of the deleted open step: the parent is open again, with its reminder
    reply = await api.post(f"/api/items/{steps[1]['id']}/restore")
    assert reply.data["item"]["status"] == "open"
    parent = (await api.get(f"/api/items/{created.item.id}")).data
    assert parent["status"] == "open" and len(parent["reminders"]) == 1 and parent["steps_done"] == 1


async def test_restoring_a_step_of_a_deleted_parent_restores_the_parent(api, make_items) -> None:
    [created] = await make_items({"kind": "task", "title": "Переезд"})
    steps = (await api.post(f"/api/items/{created.item.id}/steps", {"title": "Шаг"})).data["item"]["steps"]
    await api.post(f"/api/items/{created.item.id}/delete")
    reply = await api.post(f"/api/items/{steps[0]['id']}/restore")
    assert reply.status == 200 and reply.data["item"]["status"] == "open"
    assert (await api.get(f"/api/items/{created.item.id}")).data["status"] == "open"


async def test_adding_a_step_reopens_a_done_parent(api, make_items) -> None:
    [created] = await make_items({"kind": "task", "title": "Переезд", "when": TOMORROW_13})
    await api.post(f"/api/items/{created.item.id}/done")
    reply = await api.post(f"/api/items/{created.item.id}/steps", {"title": "Забыл ключи"})
    assert reply.data["item"]["status"] == "open" and reply.data["item"]["steps_total"] == 1


async def test_steps_of_a_deleted_parent_are_locked(api, make_items) -> None:
    [created] = await make_items({"kind": "task", "title": "Переезд"})
    steps = (await api.post(f"/api/items/{created.item.id}/steps", {"title": "Шаг"})).data["item"]["steps"]
    await api.post(f"/api/items/{created.item.id}/delete")
    reply = await api.post(f"/api/steps/{steps[0]['id']}/toggle")
    assert (reply.status, reply.data["error"]["code"]) == (409, "deleted")


async def test_split_jobs_are_never_left_for_the_chat(api, make_items, run_jobs, jobs_all, fake_ai) -> None:
    [created] = await make_items({"kind": "task", "title": "Переезд"})
    await api.post(f"/api/items/{created.item.id}/split")
    [job] = await jobs_all()
    assert job.payload["app"] is True and "message_id" not in job.payload
    fake_ai.splits.append(RuntimeError("boom"))
    await run_jobs()
    [job] = await jobs_all()
    assert job.status == "queued" and job.attempts == 1  # an ordinary retry
