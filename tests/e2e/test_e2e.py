"""End-to-end scenarios: Telegram update -> capture -> job worker -> AI (fake chutes) -> items -> cards; reminders
and digests from ``Scheduler.tick``; button presses. The harness is in conftest.py (``e2e`` fixture).

Reference time: Tue 2026-10-06 10:12 Europe/Warsaw (UTC+2).
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from dumpbot.ai import linkinfo
from dumpbot.db.models import AiUsage, BotMessage, Item, Job, OnceKey, RawMessage

WARSAW = ZoneInfo("Europe/Warsaw")
PRIMARY = "google/gemma-4-31B-turbo-TEE"
FALLBACK = "Qwen/Qwen3-32B-TEE"

OGG = b"OggS\x00\x02" + bytes(range(64)) * 4  # the voice note's bytes, served by the fake getFile/download
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-receipt" * 8

HEADPHONES = {
    "intent": "save",
    "items": [
        {
            "kind": "purchase",
            "title": "Купить наушники",
            "tags": ["электроника"],
            "when": {"text": "в обед", "time": "lunch"},
        }
    ],
}


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    """Europe/Warsaw wall time -> aware UTC."""
    return datetime(y, mo, d, h, mi, tzinfo=WARSAW).astimezone(UTC)


def wall(dt: datetime) -> str:
    """Aware datetime -> Warsaw wall time "YYYY-MM-DD HH:MM"."""
    return dt.astimezone(WARSAW).strftime("%Y-%m-%d %H:%M")


def task(title: str, when: dict[str, Any], kind: str = "task") -> dict[str, Any]:
    return {"intent": "save", "items": [{"kind": kind, "title": title, "when": when}]}


async def headphones_by_voice(e2e: Any) -> tuple[int, Any]:
    """The flagship message: a voice note "Купить наушники в обед" sent Tue 10:12. Returns (message id, card)."""
    e2e.tg.add_file("voice-1", OGG, "voice/file_1.oga")
    e2e.ai.transcripts[OGG] = "Купить наушники в обед"
    e2e.ai.script("Купить наушники в обед", HEADPHONES)
    voice = {"file_id": "voice-1", "file_unique_id": "uv1", "duration": 3, "mime_type": "audio/ogg", "file_size": 260}
    message_id = e2e.tg.push_message(voice=voice)
    card = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "👌")
    return message_id, card


async def the_job(e2e: Any) -> Job:
    async with e2e.ctx.sessionmaker() as s:
        return (await s.scalars(select(Job))).one()


async def the_job_if_any(e2e: Any) -> Job | None:
    async with e2e.ctx.sessionmaker() as s:
        return (await s.scalars(select(Job))).one_or_none()


# --- (a) flagship: voice -> card "🔔 сегодня 13:00" -> reminder at 13:00 -> Done ------------------------------------


async def test_voice_note_to_reminder_to_done(e2e: Any) -> None:
    message_id, card = await headphones_by_voice(e2e)

    # capture reacted 👀 right away, the worker 👌 with the card
    assert e2e.tg.reactions(message_id) == ["👀", "👌"]
    # STT got the downloaded OGG; the transcript went to the model as the MSG of a voice source
    assert e2e.tg.downloads == ["voice/file_1.oga"]
    [stt] = e2e.ai.stt_requests
    assert base64.b64decode(stt["audio_b64"]) == OGG
    [req] = e2e.ai.extraction_requests()
    assert req.model == PRIMARY
    assert req.msg == "Купить наушники в обед"
    assert req.line("SRC ") == "SRC voice"
    assert req.line("NOW ") == "NOW 2026-10-06T10:12 Tue Europe/Warsaw"
    assert req.images == []

    # the card: today 13:00 ("в обед") — the item's time is its notification time, no "за 5 минут"
    assert card.text.startswith("🛒 <b>Купить наушники</b>")
    assert card.text.splitlines()[1] == "🔔 сегодня 13:00"
    assert "12:55" not in card.text and "напомню" not in card.text
    assert "#электроника" in card.text
    assert [text for text, _ in card.buttons] == ["✏️ Изменить", "🔕 Без напоминания", "🗑 Удалить"]

    raw = await e2e.raw(message_id)
    assert (raw.status, raw.content_type, raw.transcript) == ("done", "voice", "Купить наушники в обед")
    item = await e2e.item("Купить наушники")
    assert (item.kind, item.status, item.source_kind, item.tags) == ("purchase", "open", "voice", ["электроника"])
    [occ] = await e2e.occurrences(item.id)
    assert (wall(occ.due_at), occ.precision, occ.status) == ("2026-10-06 13:00", "slot", "open")
    [reminder] = await e2e.reminders(item.id)
    assert (reminder.status, wall(reminder.fire_at), reminder.auto) == ("pending", "2026-10-06 13:00", False)

    # both paid calls were accounted for
    usage = await e2e.all(AiUsage)
    assert sorted(u.purpose for u in usage) == ["extract", "stt"]
    assert all(u.ok and u.cost_usd > 0 for u in usage)

    # nothing at 12:55 or 12:59 ...
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 6, 12, 55))
    await e2e.tick(local(2026, 10, 6, 12, 59))
    assert len(e2e.tg.sent) == sent_before
    # ... the notification at 13:00 itself
    await e2e.tick(local(2026, 10, 6, 13, 0))
    [reminder_msg] = e2e.tg.sent[sent_before:]
    assert reminder_msg.text == "🔔 <b>Купить наушники</b>"  # no "через 5 мин"
    # 13:00: the evening (19:00) is more than an hour away -> [🌙 Вечером] instead of [⏰ 15 мин]
    assert [text for text, _ in reminder_msg.buttons] == ["✅ Готово", "⏰ 1 час", "🌙 Вечером", "📅 Завтра"]
    assert reminder_msg.reply_to is None
    [reminder] = await e2e.reminders(item.id)
    assert (reminder.status, reminder.tg_message_id) == ("sent", reminder_msg.message_id)
    await e2e.tick(local(2026, 10, 6, 13, 1))
    assert len(e2e.tg.sent) == sent_before + 1  # not sent twice

    # [✅ Готово] on the reminder edits that message to "done"
    e2e.tg.push_callback(reminder_msg, reminder_msg.button(f"o:done:{occ.id}"))
    [edit] = await e2e.wait(lambda: e2e.tg.edits_of(reminder_msg.message_id), "the reminder edited")
    assert edit.text.startswith("✅ <s>Купить наушники</s>")
    assert "сегодня 13:00 · сделано" in edit.text
    assert [text for text, _ in edit.buttons] == ["↩️ Не сделано"]
    assert e2e.tg.toasts() == ["✅ Готово"]
    [occ] = await e2e.occurrences(item.id)
    assert occ.status == "done"
    assert (await e2e.item("Купить наушники")).status == "done"


async def test_explicit_lead_moves_the_notification_and_keeps_the_stated_time_in_the_title(e2e: Any) -> None:
    """ "Напомни за 30 минут до встречи с Олегом в 15": the item's time is 14:30, the meeting's 15:00 stays in the title
    (the model is asked for that; here it forgot, the code adds it)."""
    text = "Напомни за 30 минут до встречи с Олегом в 15"
    out = task("Встреча с Олегом", {"text": "в 15", "time": "15:00"}, kind="event")
    out["items"][0]["lead"] = [30]
    e2e.ai.script(text, out)
    message_id = e2e.tg.push_message(text)
    card = await e2e.card_for(message_id)
    assert card.text.splitlines()[:2] == ["📅 <b>Встреча с Олегом (15:00)</b>", "🔔 сегодня 14:30"]
    item = await e2e.item("Встреча с Олегом (15:00)")
    [occ] = await e2e.occurrences(item.id)
    assert (wall(occ.due_at), item.lead_minutes) == ("2026-10-06 14:30", [30])

    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 6, 14, 29))
    assert len(e2e.tg.sent) == sent_before
    await e2e.tick(local(2026, 10, 6, 14, 30))
    [reminder] = e2e.tg.sent[sent_before:]
    assert reminder.text == "🔔 <b>Встреча с Олегом (15:00)</b>"
    await e2e.tick(local(2026, 10, 6, 15, 0))
    assert len(e2e.tg.sent) == sent_before + 1  # nothing more at 15:00


# --- (b) a plain idea: only 👌 ---------------------------------------------------------------------------------------


async def test_idea_gets_only_a_reaction(e2e: Any) -> None:
    text = "Идея: бот, который сам предлагает подзадачи"
    e2e.ai.script(
        text,
        {"intent": "save", "items": [{"kind": "idea", "title": "Бот, который предлагает подзадачи", "tags": ["бот"]}]},
    )
    message_id = e2e.tg.push_message(text)
    await e2e.reaction(message_id, "👌")
    await e2e.idle()

    assert e2e.tg.reactions(message_id) == ["👀", "👌"]
    assert e2e.tg.sent == []  # no card for an idea
    item = await e2e.item("Бот, который предлагает подзадачи")
    assert (item.kind, item.status, item.tags, item.source_text) == ("idea", "open", ["бот"], text)
    assert await e2e.occurrences(item.id) == []
    assert (await e2e.raw(message_id)).status == "done"


# --- (c) "?…" query, (d) reply to the card ---------------------------------------------------------------------------


async def test_query_then_reply_edit_replans_the_reminder(e2e: Any) -> None:
    _, card = await headphones_by_voice(e2e)

    # (c) a forced question lists today's items
    e2e.clock.set(local(2026, 10, 6, 10, 15))
    e2e.ai.script("что у меня сегодня", {"intent": "query", "query": {"range": "today", "answer": "list"}})
    question = e2e.tg.push_message("?что у меня сегодня")
    answer = await e2e.card_for(question)
    await e2e.reaction(question, "👌")
    request = e2e.ai.extraction_requests()[-1]
    assert request.msg == "что у меня сегодня"  # the "?" is stripped ...
    assert request.line("MODE ") == "MODE query"  # ... and forces query mode
    assert answer.text.startswith("<b>🔎 Вот что нашёл</b>")
    assert "сегодня 13:00 · 🛒 Купить наушники" in answer.text
    assert answer.buttons == []
    assert (await e2e.raw(question)).status == "done"
    assert len(await e2e.all(Item)) == 1  # a question saves nothing

    # (d) a reply to the card moves the item
    e2e.clock.set(local(2026, 10, 6, 10, 20))
    e2e.ai.script(
        "перенеси на 15:00",
        {"intent": "edit", "edit": {"action": "update", "when": {"text": "на 15:00", "time": "15:00"}}},
    )
    reply = e2e.tg.push_message("перенеси на 15:00", reply_to=card)
    new_card = await e2e.card_for(reply)
    await e2e.reaction(reply, "👌")
    replying = e2e.ai.extraction_requests()[-1].line("REPLYING TO ITEM ")
    assert replying is not None
    assert '"title": "Купить наушники"' in replying and '"due_local": "2026-10-06 13:00"' in replying
    assert new_card.text.startswith("🛒 <b>Купить наушники</b>")
    assert new_card.text.splitlines()[1] == "🔔 сегодня 15:00"

    item = await e2e.item("Купить наушники")
    assert (await e2e.raw(reply)).reply_item_id == item.id
    assert len(await e2e.all(Item)) == 1  # edited, not duplicated
    [occ] = await e2e.occurrences(item.id)
    assert wall(occ.due_at) == "2026-10-06 15:00"
    reminders = await e2e.reminders(item.id)
    assert [(r.status, wall(r.fire_at)) for r in reminders] == [
        ("cancelled", "2026-10-06 13:00"),
        ("pending", "2026-10-06 15:00"),
    ]

    # the old time no longer fires, the new one does
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 6, 13, 0))
    assert len(e2e.tg.sent) == sent_before
    await e2e.tick(local(2026, 10, 6, 15, 0))
    [reminder_msg] = e2e.tg.sent[sent_before:]
    assert reminder_msg.text == "🔔 <b>Купить наушники</b>"


# --- (e) chutes down: ✍, retried after it recovers -------------------------------------------------------------------


async def test_ai_outage_is_retried_after_recovery(e2e: Any) -> None:
    text = "Позвонить в банк завтра в 11"
    e2e.ai.script(text, task("Позвонить в банк", {"text": "завтра в 11", "day": "+1", "time": "11:00"}))
    e2e.ai.outage = 503
    message_id = e2e.tg.push_message(text)
    await e2e.reaction(message_id, "✍")
    await e2e.wait(lambda: e2e.ai.failed_calls >= 2, "primary and fallback tried")

    async def requeued() -> Job | None:
        job = await the_job_if_any(e2e)
        return job if job is not None and job.status == "queued" and job.attempts == 1 else None

    job = await e2e.wait_db(requeued, "the job back in the queue")
    # primary and fallback model were both tried; nothing was created, the message waits
    assert [r.model for r in e2e.ai.chat_requests] == [PRIMARY, FALLBACK]
    assert e2e.tg.sent == []
    assert (await e2e.raw(message_id)).status == "deferred"
    assert await e2e.all(Item) == []
    assert (job.run_after - e2e.t0).total_seconds() == 120  # backoff 2**1 minutes
    usage = await e2e.all(AiUsage)
    assert [(u.ok, (u.error or "")[:8]) for u in usage] == [(False, "HTTP 503"), (False, "HTTP 503")]

    # still down a minute later: nothing runs before run_after
    e2e.clock.set(local(2026, 10, 6, 10, 13))
    e2e.wake_jobs()
    await e2e.idle()
    assert len(e2e.ai.chat_requests) == 2

    # chutes is back; at run_after the worker retries and succeeds
    e2e.ai.outage = None
    e2e.clock.set(job.run_after)
    e2e.wake_jobs()
    card = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "👌")
    assert card.text.startswith("📌 <b>Позвонить в банк</b>")
    assert card.text.splitlines()[1] == "🔔 завтра 11:00"  # resolved against the message date
    assert e2e.tg.reactions(message_id) == ["👀", "✍", "👌"]
    assert (await e2e.raw(message_id)).status == "done"
    await e2e.idle()
    job = await the_job(e2e)
    assert (job.status, job.attempts) == ("done", 2)


# --- (f) photo with caption: image part to the vision model, OCR saved ------------------------------------------------


async def test_photo_with_caption_goes_to_the_vision_model(e2e: Any) -> None:
    caption = "чек из магазина"
    e2e.tg.add_file("photo-big", JPEG, "photos/file_7.jpg")
    e2e.ai.script(
        caption,
        {
            "intent": "save",
            "items": [{"kind": "note", "title": "Чек из Biedronka", "tags": ["чеки"]}],
            "image": {"text": "BIEDRONKA\nRAZEM 42,50 PLN", "description": "Кассовый чек из Biedronka"},
        },
    )
    photo = [
        {"file_id": "photo-small", "file_unique_id": "ps", "width": 90, "height": 160, "file_size": 1000},
        {"file_id": "photo-big", "file_unique_id": "pb", "width": 720, "height": 1280, "file_size": len(JPEG)},
    ]
    message_id = e2e.tg.push_message(photo=photo, caption=caption)
    await e2e.reaction(message_id, "👌")
    await e2e.idle()

    assert e2e.tg.downloads == ["photos/file_7.jpg"]  # only the largest size
    [req] = e2e.ai.extraction_requests()
    assert req.model == PRIMARY
    assert req.line("SRC ") == "SRC photo"
    assert req.msg == caption
    assert req.images == ["data:image/jpeg;base64," + base64.b64encode(JPEG).decode()]
    assert [u.purpose for u in await e2e.all(AiUsage)] == ["vision"]

    raw = await e2e.raw(message_id)
    assert (raw.status, raw.content_type, raw.file_id) == ("done", "photo", "photo-big")
    assert raw.image_text == "BIEDRONKA\nRAZEM 42,50 PLN\nКассовый чек из Biedronka"
    item = await e2e.item("Чек из Biedronka")
    assert (item.kind, item.source_kind) == ("note", "photo")
    assert "RAZEM 42,50 PLN" in (item.source_text or "")
    assert e2e.tg.sent == []  # a note: reaction only


# --- (g) strangers are ignored ---------------------------------------------------------------------------------------


async def test_messages_from_strangers_are_ignored(e2e: Any) -> None:
    stranger_message = e2e.tg.push_message("купи слона", user_id=e2e.stranger)
    e2e.ai.script("ок", {"intent": "save", "items": [{"kind": "note", "title": "ок"}]})
    owner_message = e2e.tg.push_message("ок")  # delivered after the stranger's update
    await e2e.reaction(owner_message, "👌")
    await e2e.idle()

    assert e2e.stranger not in e2e.tg.chat_ids()
    assert e2e.tg.reactions(stranger_message) == []
    assert [r.message_id for r in await e2e.all(RawMessage)] == [owner_message]
    assert [r.msg for r in e2e.ai.extraction_requests()] == ["ок"]


# --- (h) digests: morning plan, evening summary, move to tomorrow -----------------------------------------------------


async def test_morning_and_evening_digests(e2e: Any) -> None:
    e2e.ai.script(
        "Позвонить маме завтра в 10", task("Позвонить маме", {"text": "завтра в 10", "day": "+1", "time": "10:00"})
    )
    e2e.ai.script("оплатить интернет в 18", task("Оплатить интернет", {"text": "в 18", "time": "18:00"}))
    mom = e2e.tg.push_message("Позвонить маме завтра в 10")
    await e2e.card_for(mom)
    internet = e2e.tg.push_message("оплатить интернет в 18")
    await e2e.card_for(internet)
    await e2e.idle()
    sent_before = len(e2e.tg.sent)

    # Wed 08:59: no digest yet; Tuesday's 18:00 reminder is long missed and not sent anymore
    await e2e.tick(local(2026, 10, 7, 8, 59))
    assert len(e2e.tg.sent) == sent_before

    # Wed 09:00: the morning digest with today's plan and the overdue task
    await e2e.tick(local(2026, 10, 7, 9, 0))
    [morning] = e2e.tg.sent[sent_before:]
    assert morning.text.startswith("☀️ <b>Доброе утро!</b> План на сегодня, ")
    assert "1. 10:00 · 📌 Позвонить маме" in morning.text
    assert "Просрочено" in morning.text and "2. вчера 18:00 · 📌 Оплатить интернет" in morning.text
    assert [text for text, _ in morning.buttons] == ["✅ 1", "✅ 2"]  # today's and the overdue one
    await e2e.tick(local(2026, 10, 7, 9, 5))
    assert len(e2e.tg.sent) == sent_before + 1  # once a day

    # Wed 21:00: the evening digest lists the not-done task and offers to move it. No tick since 09:05: to the
    # scheduler the bot was down all day, and the 10:00 reminder it never sent gets the one silent notice (§5).
    await e2e.tick(local(2026, 10, 7, 21, 0))
    [evening, notice] = e2e.tg.sent[sent_before + 1 :]
    assert notice.text == (
        "😴 Меня не было с 09:05 до 21:00 (компьютер спал?). Не прислал устаревших напоминаний: 1 — эти дела в /today."
    )
    assert notice.params["disable_notification"] == "true"
    assert evening.text.startswith("🌙 <b>Итоги дня</b>")
    assert "<b>Не сделано</b>" in evening.text and "10:00 · 📌 Позвонить маме" in evening.text
    move = evening.button("d:tmr:20261007")  # the digest's own day travels with the button
    async with e2e.ctx.sessionmaker() as s:
        keys = sorted(k for k in (await s.scalars(select(OnceKey.key))).all() if k.startswith("digest:"))
    assert keys == ["digest:evening:2026-10-07", "digest:morning:2026-10-07"]

    # [📅 Перенести невыполненное на завтра], pressed a few minutes later
    e2e.clock.set(local(2026, 10, 7, 21, 3))
    e2e.tg.push_callback(evening, move)
    [edit] = await e2e.wait(lambda: e2e.tg.edits_of(evening.message_id), "the evening digest edited")
    assert "📅 Перенесено на завтра: 2" in edit.text
    assert e2e.tg.toasts() == ["📅 Перенёс на завтра: 2"]

    mom_item = await e2e.item("Позвонить маме")
    [occ] = await e2e.occurrences(mom_item.id)
    assert (wall(occ.due_at), occ.status) == ("2026-10-08 10:00", "open")
    pending = [wall(r.fire_at) for r in await e2e.reminders(mom_item.id) if r.status == "pending"]
    assert pending == ["2026-10-08 10:00"]
    [occ] = await e2e.occurrences((await e2e.item("Оплатить интернет")).id)
    assert wall(occ.due_at) == "2026-10-08 18:00"

    # Thursday: the morning digest, then the moved reminder at 10:00
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 8, 9, 0))
    await e2e.tick(local(2026, 10, 8, 10, 0))
    texts = [c.text for c in e2e.tg.sent[sent_before:]]
    assert len(texts) == 2
    assert texts[0].startswith("☀️ <b>Доброе утро!</b>") and "10:00 · 📌 Позвонить маме" in texts[0]
    assert texts[1].startswith("🔔 <b>Позвонить маме</b>")


# --- more flows across routers, jobs and the scheduler ---------------------------------------------------------------


async def test_snooze_from_the_reminder(e2e: Any) -> None:
    await headphones_by_voice(e2e)
    await e2e.tick(local(2026, 10, 6, 13, 0))
    reminder_msg = e2e.tg.sent[-1]
    assert reminder_msg.text.startswith("🔔 <b>Купить наушники</b>")

    # [⏰ 1 час] pressed at 13:02 -> the reminder is edited, the item's time moves to 14:02
    e2e.clock.set(local(2026, 10, 6, 13, 2))
    e2e.tg.push_callback(reminder_msg, reminder_msg.button("o:s60:"))
    [edit] = await e2e.wait(lambda: e2e.tg.edits_of(reminder_msg.message_id), "the reminder edited")
    assert "💤 отложено · напомню сегодня в 14:02" in edit.text
    assert edit.buttons == []  # the snoozed reminder comes with its own buttons
    assert e2e.tg.toasts() == ["⏰ Напомню сегодня 14:02"]
    item = await e2e.item("Купить наушники")
    [occ] = await e2e.occurrences(item.id)
    assert (wall(occ.due_at), occ.status) == ("2026-10-06 14:02", "open")

    # /today shows the new time
    e2e.tg.push_message("/today", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
    today = await e2e.wait(lambda: next((c for c in e2e.tg.sent if c.text.startswith("<b>📅")), None), "/today")
    assert "14:02 · 🛒 Купить наушники" in today.text

    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 6, 14, 1))
    assert len(e2e.tg.sent) == sent_before
    await e2e.tick(local(2026, 10, 6, 14, 2))
    [again] = e2e.tg.sent[sent_before:]
    assert again.text.startswith("🔔 <b>Купить наушники</b>")
    assert [text for text, _ in again.buttons] == ["✅ Готово", "⏰ 1 час", "🌙 Вечером", "📅 Завтра"]
    assert [(r.status, wall(r.fire_at)) for r in await e2e.reminders(item.id)] == [
        ("sent", "2026-10-06 13:00"),
        ("sent", "2026-10-06 14:02"),
    ]

    # an old [⏰ 15 мин] (a reminder sent before 🌙 existed) still works
    e2e.clock.set(local(2026, 10, 6, 14, 3))
    e2e.tg.push_callback(again, f"o:s15:{occ.id}")
    await e2e.wait(lambda: e2e.tg.edits_of(again.message_id), "the second reminder edited")
    [occ] = await e2e.occurrences(item.id)
    assert wall(occ.due_at) == "2026-10-06 14:18"


async def test_recurring_task_done_and_stopped(e2e: Any) -> None:
    text = "каждый день в 9 зарядка"
    e2e.ai.script(
        text,
        {
            "intent": "save",
            "items": [
                {"kind": "task", "title": "Зарядка", "when": {"text": "в 9", "time": "09:00"}, "rrule": "FREQ=DAILY"}
            ],
        },
    )
    message_id = e2e.tg.push_message(text)
    card = await e2e.card_for(message_id)
    assert "🔔 завтра 09:00" in card.text.splitlines()  # 09:00 has passed today
    assert "🔁 каждый день" in card.text
    assert "⏹ Стоп повтор" in [t for t, _ in card.buttons]

    await e2e.tick(local(2026, 10, 7, 9, 0))  # the reminder (and the morning digest, also at 09:00)
    [reminder_msg] = [c for c in e2e.tg.sent if c.text.startswith("🔔 ")]
    assert reminder_msg.text.startswith("🔔 <b>Зарядка</b>")
    e2e.clock.set(local(2026, 10, 7, 9, 2))
    e2e.tg.push_callback(reminder_msg, reminder_msg.button("o:done:"))
    await e2e.wait(lambda: e2e.tg.edits_of(reminder_msg.message_id), "the reminder edited")
    assert e2e.tg.toasts() == ["✅ Готово · следующий раз завтра 09:00"]

    item = await e2e.item("Зарядка")
    occs = await e2e.occurrences(item.id)
    assert [(wall(o.due_at), o.status) for o in occs] == [("2026-10-07 09:00", "done"), ("2026-10-08 09:00", "open")]
    pending = [wall(r.fire_at) for r in await e2e.reminders(item.id) if r.status == "pending"]
    assert pending == ["2026-10-08 09:00"]
    assert (item.status, item.rec_active) == ("open", True)

    # /recurring lists it; its stop button ends the series
    e2e.tg.push_message("/recurring", entities=[{"type": "bot_command", "offset": 0, "length": 10}])
    listing = await e2e.wait(
        lambda: next((c for c in e2e.tg.sent if c.text.startswith("<b>🔁")), None), "/recurring answer"
    )
    assert "Зарядка" in listing.text and "каждый день" in listing.text

    async def listing_recorded() -> bool:  # the sender records the list (purpose "recurring") right after sending
        async with e2e.ctx.sessionmaker() as s:
            return await s.scalar(select(BotMessage).where(BotMessage.message_id == listing.message_id)) is not None

    await e2e.wait_db(listing_recorded, "the /recurring list recorded")
    e2e.tg.push_callback(listing, listing.button(f"i:stoprec:{item.id}"))
    [edit] = await e2e.wait(lambda: e2e.tg.edits_of(listing.message_id), "the list re-rendered")
    assert "Повторяющихся задач нет." in edit.text
    assert (await e2e.item("Зарядка")).rec_active is False
    await e2e.tick(local(2026, 10, 9, 9, 0))  # no new instances after the stop
    assert len(await e2e.occurrences(item.id)) == 2


async def test_edit_time_through_the_card_buttons(e2e: Any) -> None:
    _, card = await headphones_by_voice(e2e)
    item = await e2e.item("Купить наушники")

    # [✏️ Изменить] -> menu; [🕒 Время] -> prompt (the card message is edited in place each time)
    e2e.tg.push_callback(card, card.button(f"i:edit:{item.id}"))
    [menu] = await e2e.wait(lambda: e2e.tg.edits_of(card.message_id), "edit menu")
    assert "🕒 Время" in [t for t, _ in menu.buttons]
    e2e.tg.push_callback(menu, menu.button(f"i:time:{item.id}"))
    prompt = (await e2e.wait(lambda: e2e.tg.edits_of(card.message_id)[1:], "time prompt"))[0]
    assert prompt.text.startswith("🕒 Когда?")

    # the answer (typed within 5 minutes of the prompt, states.ANSWER_TTL) goes to the AI as an edit of this item;
    # the new card arrives as a reply to it
    e2e.clock.set(local(2026, 10, 6, 10, 15))

    def reschedule(req: Any) -> dict[str, Any]:
        assert req.line("REPLYING TO ITEM ") is not None
        return {"intent": "edit", "edit": {"when": {"text": "в 16:30", "time": "16:30"}}}

    e2e.ai.script("перенеси на: в 16:30", reschedule)
    answer = e2e.tg.push_message("в 16:30")
    new_card = await e2e.card_for(answer)
    await e2e.reaction(answer, "👌")
    assert e2e.tg.reactions(answer) == ["👀", "👌"]
    assert "🔔 сегодня 16:30" in new_card.text.splitlines()
    restored = e2e.tg.edits_of(card.message_id)[-1]  # the prompt turned back into the card
    assert restored.text.startswith("🛒 <b>Купить наушники</b>")
    assert len(await e2e.all(RawMessage)) == 1  # the answer was not captured as a new message
    [occ] = await e2e.occurrences(item.id)
    assert wall(occ.due_at) == "2026-10-06 16:30"


async def test_clarifying_question(e2e: Any) -> None:
    text = "встреча с Олегом в 8"

    def meeting(req: Any) -> dict[str, Any]:
        clarification = req.line("CLARIFICATION ")
        at = "20:00" if clarification == "CLARIFICATION вечера" else "08:00"
        out: dict[str, Any] = task("Встреча с Олегом", {"text": "в 8", "time": at}, kind="event")
        if clarification is None:
            out["ask"] = {"item": 0, "q": "В 8 утра или вечера?", "options": ["утра", "вечера"]}
        return out

    e2e.ai.script(text, meeting)
    message_id = e2e.tg.push_message(text)
    question = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "🤔")
    assert question.text == "🤔 В 8 утра или вечера?"
    assert [t for t, _ in question.buttons] == ["утра", "вечера"]

    e2e.tg.push_callback(question, question.buttons[1][1])
    cards = await e2e.wait(lambda: e2e.tg.replies_to(message_id)[1:], "the card after the answer")
    assert "🔔 сегодня 20:00" in cards[0].text.splitlines()
    [chosen] = e2e.tg.edits_of(question.message_id)
    assert "вечера" in chosen.text.split("\n")[-1]
    assert chosen.buttons == []
    items = await e2e.all(Item)
    assert [(i.title, i.status) for i in items] == [("Встреча с Олегом", "deleted"), ("Встреча с Олегом", "open")]


async def test_forwarded_channel_post_with_a_link(e2e: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The fake web lives on 127.0.0.1, which link titles never fetch otherwise (tests/ai/test_linkinfo_safety.py).
    monkeypatch.setattr(linkinfo, "address_allowed", lambda host: host == "127.0.0.1")
    url = f"{e2e.ai_url}/pages/habr"
    e2e.ai.pages["habr"] = "<html><head><title>Как мы ускорили Postgres &amp; asyncpg</title></head></html>"
    text = f"Годная статья {url}"
    e2e.ai.script(text, {"intent": "save", "items": [{"kind": "link", "title": "Как ускорить Postgres"}]})
    origin = {
        "type": "channel",
        "chat": {"id": -1001234, "type": "channel", "title": "Хабр", "username": "habr_com"},
        "message_id": 555,
        "date": int(e2e.t0.timestamp()) - 3600,
    }
    offset = len("Годная статья ".encode("utf-16-le")) // 2
    entities = [{"type": "url", "offset": offset, "length": len(url)}]
    message_id = e2e.tg.push_message(text, forward_origin=origin, entities=entities)
    await e2e.reaction(message_id, "👌")
    await e2e.idle()

    assert e2e.ai.page_hits == ["habr"]
    [req] = e2e.ai.extraction_requests()
    assert req.line("SRC ") == "SRC forward"
    assert req.line("FORWARDED FROM ") == "FORWARDED FROM канал «Хабр»"
    assert f"{url}: Как мы ускорили Postgres & asyncpg" in req.user_text
    item = await e2e.item("Как ускорить Postgres")
    assert (item.kind, item.urls) == ("link", [url])
    assert item.source is not None
    assert item.source["forward_origin"]["url"] == "https://t.me/habr_com/555"
    assert item.source["link_titles"] == {url: "Как мы ускорили Postgres & asyncpg"}
    assert e2e.tg.sent == []  # a link: reaction only


async def test_commands(e2e: Any) -> None:
    await headphones_by_voice(e2e)
    e2e.ai.script("что я хотел купить?", {"intent": "query", "query": {"kinds": ["purchase"], "answer": "summary"}})
    e2e.ai.answer = "Купить наушники — сегодня в 13:00 <важно>"

    def command(text: str) -> int:
        name = text.split()[0]
        return e2e.tg.push_message(text, entities=[{"type": "bot_command", "offset": 0, "length": len(name)}])

    commands = [
        "/today",
        "/tasks",
        "/recent",
        "/search наушники",
        "/recurring",
        "/settings",
        "/usage",
        "/help",
        "/start",
    ]
    sent_before = len(e2e.tg.sent)
    for text in commands:
        count = len(e2e.tg.sent)
        command(text)
        await e2e.wait(lambda count=count: len(e2e.tg.sent) > count, f"answer to {text}")
    answers = [c.text for c in e2e.tg.sent[sent_before:]]
    assert len(answers) == len(commands)
    assert answers[0].startswith("<b>📅 Сегодня, ") and "13:00 · 🛒 Купить наушники" in answers[0]
    assert all("Купить наушники" in a for a in answers[1:4])
    assert "Повторяющихся задач нет." in answers[4]
    assert "$" in answers[6]
    assert answers[8].startswith("👋 Привет!")

    unknown = command("/foo")
    await e2e.wait(lambda: any("Не знаю такой команды" in c.text for c in e2e.tg.sent), "unknown command hint")
    assert e2e.tg.reactions(unknown) == []

    # /ask: answered in prose by the model, from the saved rows
    question = command("/ask что я хотел купить?")
    reply = await e2e.card_for(question)
    await e2e.reaction(question, "👌")
    assert reply.text == "Купить наушники — сегодня в 13:00 &lt;важно&gt;"
    [answer_req] = [r for r in e2e.ai.chat_requests if r.is_answer]
    assert "QUESTION что я хотел купить?" in answer_req.user_text
    assert "Купить наушники" in answer_req.user_text
    assert len(await e2e.all(RawMessage)) == 2  # the voice note and the /ask question


async def test_budget_exhausted_then_raised(e2e: Any) -> None:
    # /settings -> 💰 budget -> "other amount" -> type 0
    e2e.tg.push_message("/settings", entities=[{"type": "bot_command", "offset": 0, "length": 9}])
    menu = await e2e.wait(lambda: next(iter(e2e.tg.sent), None), "settings menu")
    e2e.tg.push_callback(menu, menu.button("s:menu:budget"))
    [budget_menu] = await e2e.wait(lambda: e2e.tg.edits_of(menu.message_id), "budget menu")
    e2e.tg.push_callback(budget_menu, budget_menu.button("s:ask:budget_usd"))
    await e2e.wait(lambda: len(e2e.tg.edits_of(menu.message_id)) >= 2, "budget prompt")
    zero = e2e.tg.push_message("0")
    await e2e.reaction(zero, "👌")
    assert len(await e2e.all(RawMessage)) == 0  # a settings answer, not a note

    # a message now waits for the budget: ✍ and a notice once
    text = "купить корм коту вечером"
    e2e.ai.script(text, task("Купить корм коту", {"text": "вечером", "time": "evening"}, kind="purchase"))
    message_id = e2e.tg.push_message(text)
    await e2e.reaction(message_id, "✍")
    notice = await e2e.wait(lambda: next((c for c in e2e.tg.sent if c.text.startswith("⛔")), None), "budget notice")
    assert notice.text.startswith("⛔ ИИ выключен: бюджет $0.")
    assert e2e.ai.chat_requests == []
    await e2e.idle()
    job = await the_job(e2e)
    # the new month starts at 00:00, inside the quiet hours: the deferred cards come at 08:00
    assert job.status == "queued" and wall(job.run_after) == "2026-11-01 08:00"
    assert (await e2e.raw(message_id)).status == "deferred"

    # raising the budget makes the deferred message run now
    last = e2e.tg.edits_of(menu.message_id)[-1]
    e2e.tg.push_callback(last, last.button("s:budget:5"))
    card = await e2e.card_for(message_id)
    assert "<b>Купить корм коту</b>" in card.text
    assert "🔔 сегодня 19:00" in card.text.splitlines()
    await e2e.reaction(message_id, "👌")
    assert e2e.tg.reactions(message_id) == ["👀", "✍", "👌"]


async def test_album_is_processed_as_one_message(e2e: Any) -> None:
    other = b"\xff\xd8\xff\xe0" + b"second-receipt" * 8
    e2e.tg.add_file("album-1", JPEG, "photos/a1.jpg")
    e2e.tg.add_file("album-2", other, "photos/a2.jpg")
    e2e.ai.script(
        "чеки за неделю",
        {
            "intent": "save",
            "items": [{"kind": "note", "title": "Чеки за неделю"}],
            "image": {"text": "RAZEM 42,50\nRAZEM 17,00", "description": "Два чека"},
        },
    )

    def part(file_id: str, unique: str) -> list[dict[str, Any]]:
        return [{"file_id": file_id, "file_unique_id": unique, "width": 720, "height": 1280}]

    first = e2e.tg.push_message(photo=part("album-1", "a1"), caption="чеки за неделю", media_group_id="mg-1")
    second = e2e.tg.push_message(photo=part("album-2", "a2"), media_group_id="mg-1")
    await e2e.reaction(second, "👀")
    assert e2e.ai.chat_requests == []  # debounced: the album waits for its other parts

    e2e.clock.advance(seconds=3)
    e2e.wake_jobs()
    await e2e.reaction(first, "👌")
    await e2e.idle()
    [req] = e2e.ai.extraction_requests()
    assert req.msg == "чеки за неделю"
    assert len(req.images) == 2
    raws = sorted(await e2e.all(RawMessage), key=lambda r: r.message_id)  # the parts are captured concurrently
    assert [(r.message_id, r.status) for r in raws] == [(first, "done"), (second, "done")]
    assert raws[0].image_text == "RAZEM 42,50\nRAZEM 17,00\nДва чека"
    [item] = await e2e.all(Item)
    assert (item.title, item.raw_message_id) == ("Чеки за неделю", raws[0].id)
    assert [j.status for j in await e2e.all(Job)] == ["done"]


async def test_delete_and_restore_from_the_card(e2e: Any) -> None:
    _, card = await headphones_by_voice(e2e)
    item = await e2e.item("Купить наушники")

    e2e.tg.push_callback(card, card.button(f"i:del:{item.id}"))
    [deleted] = await e2e.wait(lambda: e2e.tg.edits_of(card.message_id), "card shows deleted")
    assert deleted.text.startswith("🗑 Удалено")
    assert [t for t, _ in deleted.buttons] == ["↩️ Вернуть"]
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 6, 12, 0))
    assert [r.status for r in await e2e.reminders(item.id)] == ["cancelled"]

    e2e.tg.push_callback(deleted, deleted.button(f"i:undel:{item.id}"))
    restored = (await e2e.wait(lambda: e2e.tg.edits_of(card.message_id)[1:], "card restored"))[0]
    assert "🔔 сегодня 13:00" in restored.text.splitlines()
    assert e2e.tg.toasts() == ["🗑 Удалено", "↩️ Восстановлено"]
    await e2e.tick(local(2026, 10, 6, 13, 0))
    [reminder_msg] = e2e.tg.sent[sent_before:]
    assert reminder_msg.text.startswith("🔔 <b>Купить наушники</b>")


async def test_unusable_model_output_is_saved_as_a_note_without_retries(e2e: Any) -> None:
    text = "что-то непонятное"
    e2e.ai.script(text, {"intent": "edit"})  # invalid: "edit" without a patch, every time
    message_id = e2e.tg.push_message(text)

    await e2e.reaction(message_id, "😢")
    notice = await e2e.card_for(message_id)
    assert notice.text.startswith("😢 Не смог обработать сообщение — оно сохранено")
    await e2e.idle()

    # temperature 0 + a fixed seed: every retry would pay for the same unusable answer, so there are none.
    job = await the_job_if_any(e2e)
    assert (job.status, job.attempts) == ("done", 1)
    assert len(e2e.ai.chat_requests) == 3  # primary + its retry + fallback, once
    assert (await e2e.raw(message_id)).status == "done"
    [item] = await e2e.all(Item)
    assert (item.kind, item.title, item.source_text, item.ai_model) == ("note", text, text, None)


async def test_edit_by_description_with_candidates(e2e: Any) -> None:
    for text, title, at in [
        ("встреча с Олегом в 15", "Встреча с Олегом", "15:00"),
        ("созвон с Олегом в 17", "Созвон с Олегом", "17:00"),
    ]:
        e2e.ai.script(text, task(title, {"text": text[-5:], "time": at}, kind="event"))
        await e2e.card_for(e2e.tg.push_message(text))
    await e2e.idle()

    # "перенеси … с Олегом на 18" matches both: the bot asks which one
    text = "перенеси с Олегом на 18"
    e2e.ai.script(text, {"intent": "edit", "edit": {"target": "Олегом", "when": {"text": "на 18", "time": "18:00"}}})
    message_id = e2e.tg.push_message(text)
    choice = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "🤔")
    assert choice.text.startswith("✏️ Какую запись изменить?")
    assert [t for t, _ in choice.buttons] == ["1", "2"]
    lines = choice.text.split("\n")
    assert "Встреча с Олегом" in lines[1] and "Созвон с Олегом" in lines[2]  # nearest due first
    second = await e2e.item("Созвон с Олегом")
    data = next(d for _, d in choice.buttons if d.endswith(f":{second.id}"))

    calls = len(e2e.ai.chat_requests)
    e2e.tg.push_callback(choice, data)
    # the edit read from the message goes to the picked item: no second AI call (iteration 3, §3)
    reply = (await e2e.wait(lambda: e2e.tg.replies_to(message_id)[1:], "reply about the picked item"))[0]
    assert reply.text == "🕒 Перенёс «Созвон с Олегом» на сегодня 18:00"
    assert [t for t, _ in reply.buttons] == ["↩️ Отменить"]
    assert len(e2e.ai.chat_requests) == calls
    [occ] = await e2e.occurrences(second.id)
    assert wall(occ.due_at) == "2026-10-06 18:00"
    first = await e2e.item("Встреча с Олегом")
    [occ] = await e2e.occurrences(first.id)
    assert wall(occ.due_at) == "2026-10-06 15:00"  # untouched
    [chosen] = e2e.tg.edits_of(choice.message_id)
    assert chosen.buttons == []


async def test_user_text_is_escaped_everywhere(e2e: Any) -> None:
    title = "Проверить <b>теги</b> & «кавычки» <script>"
    text = "проверить разметку"
    e2e.ai.script(
        text,
        {
            "intent": "save",
            "items": [
                {
                    "kind": "task",
                    "title": title,
                    "details": "a < b && c > d",
                    "tags": ["<x>"],
                    "when": {"text": "в 11", "time": "11:00"},
                }
            ],
        },
    )
    message_id = e2e.tg.push_message(text)
    card = await e2e.card_for(message_id)
    assert "Проверить &lt;b&gt;теги&lt;/b&gt; &amp; «кавычки» &lt;script&gt;" in card.text
    await e2e.tick(local(2026, 10, 6, 11, 0))  # the reminder and (still within its window) the morning digest
    e2e.tg.push_message("/today", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
    e2e.tg.push_message("/recent", entities=[{"type": "bot_command", "offset": 0, "length": 7}])
    await e2e.wait(lambda: len(e2e.tg.sent) >= 5, "the lists")
    await e2e.tick(local(2026, 10, 6, 21, 0))  # the evening digest
    heads = [c.text.split()[0] for c in e2e.tg.sent]
    # card, reminder, morning digest, /today and /recent (handled concurrently: any order), evening digest
    assert heads[:3] == ["📌", "🔔", "☀️"] and sorted(heads[3:5]) == ["<b>📅", "<b>🕘"] and heads[5] == "🌙"
    for call in e2e.tg.sent:
        assert "<script>" not in call.text and "<b>теги" not in call.text
    # the fake Telegram rejects anything it could not parse (checked again at teardown)
    assert e2e.tg.rejected == []
