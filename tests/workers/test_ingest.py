"""services.ingest: Message -> RawMessage fields, saving (idempotent) and job routing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.types import (
    Animation,
    Audio,
    Chat,
    Contact,
    Dice,
    Document,
    Location,
    MessageEntity,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    PhotoSize,
    Sticker,
    User,
    Venue,
    Video,
    VideoNote,
    Voice,
)
from sqlalchemy import select, update

from dumpbot.db.models import Job
from dumpbot.services import ingest

OWNER = 4242


def photo(file_id: str, w: int, h: int, size: int | None = None) -> PhotoSize:
    return PhotoSize(file_id=file_id, file_unique_id=f"u-{file_id}", width=w, height=h, file_size=size)


# --- URLs ---------------------------------------------------------------------------------------------------------


def test_url_entities_use_utf16_offsets(make_message, u16) -> None:
    # 🎉 (2 units) and a ZWJ family emoji (8 units) before the URLs: Python indexes would be off.
    text = "🎉👨‍👩‍👧 смотри habr.com/ru/путь и https://example.com/a?b=1 — подробнее тут"
    o1, l1 = u16(text, "habr.com/ru/путь")
    o2, l2 = u16(text, "https://example.com/a?b=1")
    o3, l3 = u16(text, "тут")
    msg = make_message(
        text=text,
        entities=[
            MessageEntity(type="url", offset=o1, length=l1),
            MessageEntity(type="bold", offset=0, length=2),
            MessageEntity(type="url", offset=o2, length=l2),
            MessageEntity(type="text_link", offset=o3, length=l3, url="https://t.me/durov/1"),
            MessageEntity(type="url", offset=o2, length=l2),  # duplicate
        ],
    )
    fields = ingest.message_to_raw_fields(msg)
    assert fields["urls"] == ["https://habr.com/ru/путь", "https://example.com/a?b=1", "https://t.me/durov/1"]
    assert fields["content_type"] == "text"
    assert fields["text"] == text


def test_utf16_slice_handles_surrogates() -> None:
    text = "😀😀x https://a.b"
    assert ingest.utf16_slice(text, 6, 11) == "https://a.b"
    assert ingest.utf16_slice(text, 0, 2) == "😀"


def test_caption_entities_on_largest_photo(make_message, u16) -> None:
    caption = "📸 скрин https://site.org"
    off, ln = u16(caption, "https://site.org")
    msg = make_message(
        photo=[photo("small", 90, 60, 1000), photo("big", 1280, 853, 90000), photo("mid", 320, 213, 9000)],
        caption=caption,
        caption_entities=[MessageEntity(type="url", offset=off, length=ln)],
    )
    fields = ingest.message_to_raw_fields(msg)
    assert fields["content_type"] == "photo"
    assert (fields["file_id"], fields["width"], fields["height"], fields["file_size"]) == ("big", 1280, 853, 90000)
    assert fields["file_unique_id"] == "u-big"
    assert fields["mime_type"] == "image/jpeg"
    assert fields["text"] == caption
    assert fields["urls"] == ["https://site.org"]


def test_text_entities_ignored_for_caption_message(make_message) -> None:
    msg = make_message(photo=[photo("p", 10, 10)])
    fields = ingest.message_to_raw_fields(msg)
    assert fields["text"] is None
    assert fields["urls"] == []


# --- content types ------------------------------------------------------------------------------------------------


def test_document_with_image_mime_is_image_document(make_message) -> None:
    doc = Document(file_id="d1", file_unique_id="ud1", file_name="screen.png", mime_type="image/png", file_size=5)
    fields = ingest.message_to_raw_fields(make_message(document=doc))
    assert fields["content_type"] == "image_document"
    assert (fields["file_id"], fields["file_name"], fields["mime_type"]) == ("d1", "screen.png", "image/png")


def test_other_document(make_message) -> None:
    doc = Document(file_id="d2", file_unique_id="ud2", file_name="cv.pdf", mime_type="application/pdf")
    fields = ingest.message_to_raw_fields(make_message(document=doc, caption="резюме"))
    assert fields["content_type"] == "document"
    assert fields["file_name"] == "cv.pdf"
    assert fields["text"] == "резюме"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {"voice": Voice(file_id="v", file_unique_id="uv", duration=7, mime_type="audio/ogg", file_size=9000)},
            {"content_type": "voice", "file_id": "v", "duration": 7, "mime_type": "audio/ogg", "file_size": 9000},
        ),
        (
            {"audio": Audio(file_id="a", file_unique_id="ua", duration=200, mime_type="audio/mpeg", file_name="s.mp3")},
            {"content_type": "audio", "duration": 200, "mime_type": "audio/mpeg", "file_name": "s.mp3"},
        ),
        (
            {"video": Video(file_id="vi", file_unique_id="uvi", width=1920, height=1080, duration=12)},
            {"content_type": "video", "width": 1920, "height": 1080, "duration": 12},
        ),
        (
            {"video_note": VideoNote(file_id="vn", file_unique_id="uvn", length=240, duration=5)},
            {"content_type": "video_note", "width": 240, "height": 240, "duration": 5},
        ),
        (
            {
                "animation": Animation(file_id="an", file_unique_id="uan", width=200, height=100, duration=3),
                "document": Document(file_id="an", file_unique_id="uan", mime_type="video/mp4"),
            },
            {"content_type": "animation", "file_id": "an", "width": 200},
        ),
        (
            {
                "sticker": Sticker(
                    file_id="st",
                    file_unique_id="ust",
                    type="regular",
                    width=512,
                    height=512,
                    is_animated=False,
                    is_video=False,
                    emoji="😀",
                )
            },
            {"content_type": "sticker", "file_id": "st", "width": 512},
        ),
        ({"location": Location(latitude=52.23, longitude=21.01)}, {"content_type": "location", "file_id": None}),
        (
            {
                "venue": Venue(location=Location(latitude=1, longitude=2), title="Кафе", address="ул. 1"),
                "location": Location(latitude=1, longitude=2),
            },
            {"content_type": "location"},
        ),
        ({"contact": Contact(phone_number="+48", first_name="Оля")}, {"content_type": "contact"}),
        ({"dice": Dice(emoji="🎲", value=3)}, {"content_type": "other", "file_id": None}),
    ],
)
def test_content_types(make_message, kwargs, expected) -> None:
    fields = ingest.message_to_raw_fields(make_message(**kwargs))
    for key, value in expected.items():
        assert fields[key] == value, key
    json.dumps(fields["raw_json"])  # serializable as JSONB


# --- forwards -----------------------------------------------------------------------------------------------------

FWD_DATE = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def test_forward_from_public_channel(make_message) -> None:
    origin = MessageOriginChannel(
        date=FWD_DATE,
        chat=Chat(id=-1001234567890, type="channel", title="Хабр", username="habr_com"),
        message_id=4321,
        author_signature="Иван",
    )
    fields = ingest.message_to_raw_fields(make_message(text="пост", forward_origin=origin))
    assert fields["forward_origin"] == {
        "type": "channel",
        "title": "Хабр",
        "username": "habr_com",
        "chat_id": -1001234567890,
        "message_id": 4321,
        "url": "https://t.me/habr_com/4321",
        "author_signature": "Иван",
        "date": "2026-10-05T12:00:00+00:00",
    }


def test_forward_from_private_channel_links_by_internal_id(make_message) -> None:
    origin = MessageOriginChannel(
        date=FWD_DATE, chat=Chat(id=-1001234567890, type="channel", title="Закрытый"), message_id=5
    )
    fields = ingest.message_to_raw_fields(make_message(text="x", forward_origin=origin))
    assert fields["forward_origin"]["url"] == "https://t.me/c/1234567890/5"
    assert "username" not in fields["forward_origin"]


def test_forward_from_user_hidden_user_and_chat(make_message) -> None:
    user = MessageOriginUser(
        date=FWD_DATE, sender_user=User(id=7, is_bot=False, first_name="Оля", last_name="К", username="olya")
    )
    hidden = MessageOriginHiddenUser(date=FWD_DATE, sender_user_name="Аноним")
    chat = MessageOriginChat(date=FWD_DATE, sender_chat=Chat(id=-100777, type="supergroup", title="Семья"))

    u = ingest.message_to_raw_fields(make_message(text="a", forward_origin=user))["forward_origin"]
    assert u == {
        "type": "user",
        "sender_name": "Оля К",
        "username": "olya",
        "user_id": 7,
        "url": "https://t.me/olya",
        "date": "2026-10-05T12:00:00+00:00",
    }
    h = ingest.message_to_raw_fields(make_message(text="b", forward_origin=hidden))["forward_origin"]
    assert h == {"type": "hidden_user", "sender_name": "Аноним", "date": "2026-10-05T12:00:00+00:00"}
    c = ingest.message_to_raw_fields(make_message(text="c", forward_origin=chat))["forward_origin"]
    assert c["type"] == "chat" and c["title"] == "Семья" and c["chat_id"] == -100777 and "url" not in c


def test_reply_media_group_date_and_raw_json(make_message) -> None:
    reply_to = make_message(message_id=50, text="карточка")
    msg = make_message(message_id=51, text="перенеси", reply_to_message=reply_to, media_group_id="g1")
    fields = ingest.message_to_raw_fields(msg)
    assert fields["reply_to_message_id"] == 50
    assert fields["media_group_id"] == "g1"
    assert fields["chat_id"] == OWNER and fields["message_id"] == 51
    assert fields["tg_date"].tzinfo is not None
    dumped = json.loads(json.dumps(fields["raw_json"]))
    assert dumped["text"] == "перенеси"
    assert dumped["reply_to_message"]["message_id"] == 50
    assert fields["forward_origin"] is None


# --- DB: save + enqueue -------------------------------------------------------------------------------------------


async def test_save_incoming_is_idempotent(session, make_message) -> None:
    msg = make_message(message_id=77, text="купить молоко")
    raw, created = await ingest.save_incoming(session, msg, force_query=True, reply_item_id=None)
    await session.commit()
    assert created and raw.id is not None
    assert raw.status == "new" and raw.force_query is True and raw.text == "купить молоко"

    again, created_again = await ingest.save_incoming(session, msg)
    assert not created_again
    assert again.id == raw.id


async def test_enqueue_message_and_reply_edit(session, make_message, frozen) -> None:
    raw, _ = await ingest.save_incoming(session, make_message(message_id=1, text="a"))
    reply, _ = await ingest.save_incoming(session, make_message(message_id=2, text="b"), reply_item_id=55)
    await ingest.enqueue_processing(session, raw, frozen.now)
    await ingest.enqueue_processing(session, raw, frozen.now)  # duplicate update: deduped
    await ingest.enqueue_processing(session, reply, frozen.now)
    await session.commit()
    jobs = (await session.scalars(select(Job).order_by(Job.id))).all()
    assert [(j.kind, j.payload, j.dedupe_key) for j in jobs] == [
        ("message", {"raw_id": raw.id}, f"msg:{raw.id}"),
        ("edit", {"raw_id": reply.id, "item_id": 55}, f"edit:{reply.id}"),
    ]
    assert jobs[0].run_after == frozen.now


async def test_album_parts_debounce_one_job(session, make_message, frozen) -> None:
    first, _ = await ingest.save_incoming(
        session, make_message(message_id=10, photo=[photo("p1", 9, 9)], media_group_id="G")
    )
    await ingest.enqueue_processing(session, first, frozen.now)
    t1 = frozen.now + timedelta(seconds=1.5)
    second, _ = await ingest.save_incoming(
        session, make_message(message_id=11, photo=[photo("p2", 9, 9)], media_group_id="G")
    )
    await ingest.enqueue_processing(session, second, t1)
    await session.commit()

    jobs = (await session.scalars(select(Job))).all()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.kind == "album"
    assert job.payload == {"media_group_id": "G", "chat_id": OWNER}
    assert job.dedupe_key == f"album:{OWNER}:G"
    assert job.run_after == t1 + ingest.ALBUM_DELAY  # pushed back by the later part

    # The album already ran: a straggler is processed on its own.
    await session.execute(update(Job).values(status="done"))
    late, _ = await ingest.save_incoming(
        session, make_message(message_id=12, photo=[photo("p3", 9, 9)], media_group_id="G")
    )
    await ingest.enqueue_processing(session, late, t1)
    await session.commit()
    kinds = [(j.kind, j.payload) for j in (await session.scalars(select(Job).order_by(Job.id))).all()]
    assert kinds[-1] == ("message", {"raw_id": late.id})
