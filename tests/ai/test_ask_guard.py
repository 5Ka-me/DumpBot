"""The AM/PM question guard of ``extract`` (``_drop_pointless_ask``): "Напомни часов в 17 прогуглить про визу" came back
with when 17:00 and the question "Напомнить в 17:00 или 05:00?". A 24-hour hour 13..23 or 0 (or one with утра / вечера)
is never AM/PM-ambiguous: such a question is dropped and the item stays; a bare "в 8" keeps its question."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from dumpbot.ai.extract import ExtractInput, _drop_pointless_ask, extract
from dumpbot.ai.prompts import SYSTEM_PROMPT
from dumpbot.ai.schemas import Extraction
from dumpbot.domain.prefs import Prefs

NOW = datetime(2026, 10, 6, 12, 5, tzinfo=UTC)
VISA = {
    "intent": "save",
    "items": [
        {
            "kind": "task",
            "title": "Прогуглить про визу в Черногорию",
            "when": {"text": "в 17", "time": "17:00", "role": "event"},
            "conf": "high",
        }
    ],
    "ask": {"q": "Напомнить в 17:00 или 05:00?", "options": ["17:00", "05:00"]},
}
MEETING = {
    "items": [{"kind": "event", "title": "Встреча с Олегом", "when": {"text": "в 8", "time": "20:00"}, "conf": "low"}],
    "ask": {"q": "В 8 утра или вечера?", "options": ["08:00", "20:00"]},
}


def ask_of(when: dict[str, Any], options: list[str], conf: str = "high", **item: Any) -> Extraction:
    spec = {"kind": "task", "title": "Дело", "when": when, "conf": conf, **item}
    return Extraction.model_validate({"items": [spec], "ask": {"q": "Когда?", "options": options}})


async def test_the_live_case_drops_the_question_and_keeps_the_item(fake, client, settings):
    fake.chat_json(VISA)
    text = "Напомни часов в 17 прогуглить про визу в черногорию"
    out = await extract(client, settings, ExtractInput(text=text, source="text", now=NOW, prefs=Prefs()))
    ext = out.extraction
    assert ext.ask is None
    [item] = ext.items
    assert (item.title, item.when.time, item.when.text, item.conf) == (
        "Прогуглить про визу в Черногорию",
        "17:00",
        "в 17",
        "high",
    )


async def test_a_bare_8_keeps_its_question(fake, client, settings):
    fake.chat_json(MEETING)
    out = await extract(
        client, settings, ExtractInput(text="встреча с Олегом в 8", source="text", now=NOW, prefs=Prefs())
    )
    assert out.extraction.ask is not None and out.extraction.ask.options == ["08:00", "20:00"]
    assert out.extraction.items[0].conf == "low"


@pytest.mark.parametrize(
    ("when", "options"),
    [
        ({"text": "в 17", "time": "17:00"}, ["17:00", "05:00"]),
        ({"text": "часов в 17", "time": "17:00"}, ["17:00", "5:00"]),
        ({"text": "в 21:30", "time": "21:30"}, ["21:30", "09:30"]),
        ({"text": "завтра к 19", "day": "+1", "time": "19:00"}, ["07:00", "19:00"]),
        ({"text": "в 17 часов", "time": "17:00"}, ["17", "5"]),
        ({"text": "в 00:15", "time": "00:15"}, ["00:15", "12:15"]),
        ({"text": "в семнадцать", "time": "17:00"}, ["17:00", "05:00"]),  # a voice transcript
        ({"text": "в двадцать два", "time": "22:00"}, ["22:00", "10:00"]),
        ({"text": "к двадцати двум", "time": "22:00"}, ["22:00", "10:00"]),
        ({"text": "в 2 часа дня", "time": "14:00"}, ["02:00", "14:00"]),
        ({"text": "в 8 вечера", "time": "20:00"}, ["08:00", "20:00"]),  # утра / вечера pins it too
        ({"text": "в 7 утра", "time": "07:00"}, ["7 утра", "7 вечера"]),
        ({"time": "17:00"}, ["17:00", "05:00"]),  # no phrase: the 24-hour time decides
        ({"text": "в 17", "time": "17:00"}, ["(17:00)", "в 5"]),
    ],
)
def test_pinned_hours_lose_the_am_pm_question(when, options):
    ext = _drop_pointless_ask(ask_of(when, options))
    assert ext.ask is None
    assert len(ext.items) == 1 and ext.items[0].when is not None


@pytest.mark.parametrize(
    ("when", "options"),
    [
        ({"text": "в 8", "time": "20:00"}, ["08:00", "20:00"]),  # genuine: the likelier one is 20:00
        ({"text": "в 9", "time": "21:00"}, ["09:00", "21:00"]),
        ({"text": "в восемь", "time": "20:00"}, ["08:00", "20:00"]),
        ({"text": "13 октября в 8", "day": "10-13", "time": "20:00"}, ["08:00", "20:00"]),  # 13 is a day
        ({"text": "к 15-му в 9", "day": "d15", "time": "21:00"}, ["09:00", "21:00"]),
        ({"text": "в 8, до 17", "time": "20:00"}, ["08:00", "20:00"]),  # the asked hour is the 8
        ({"text": "в 17", "time": "17:00"}, ["17:00", "18:00"]),  # not an AM/PM question
        ({"text": "в 17", "time": "17:00"}, ["17:00", "05:00", "Другое время"]),  # not only clock times
        ({"text": "в 17", "time": "17:00"}, ["сегодня", "завтра"]),
        ({"text": "после обеда", "time": "14:00"}, ["14:00", "02:00"]),  # a phrase without the hour: asked
        ({"text": "двадцатого в 8", "day": "d20", "time": "20:00"}, ["08:00", "20:00"]),  # «двадцатого» is a day
    ],
)
def test_other_questions_stay(when, options):
    ext = ask_of(when, options)
    assert _drop_pointless_ask(ext) == ext


def test_low_confidence_about_the_pinned_hour_becomes_med():
    ext = _drop_pointless_ask(ask_of({"text": "в 18", "time": "18:00"}, ["18:00", "06:00"], conf="low"))
    assert ext.ask is None and ext.items[0].conf == "med"  # no "⚠️ проверь время" on the card


def test_the_question_about_another_item_is_judged_by_that_item():
    ext = Extraction.model_validate(
        {
            "items": [
                {"kind": "task", "title": "Созвон", "when": {"text": "в 17", "time": "17:00"}},
                {"kind": "event", "title": "Встреча", "when": {"text": "в 8", "time": "20:00"}, "conf": "low"},
            ],
            "ask": {"item": 1, "q": "В 8 утра или вечера?", "options": ["08:00", "20:00"]},
        }
    )
    assert _drop_pointless_ask(ext) == ext
    pinned = ext.model_copy(update={"ask": ext.ask.model_copy(update={"item": 0, "options": ["17:00", "05:00"]})})
    assert _drop_pointless_ask(pinned).ask is None


def test_no_question_no_change():
    ext = Extraction.model_validate({"items": [{"kind": "task", "title": "Дело", "when": {"text": "в 17"}}]})
    assert _drop_pointless_ask(ext) is ext


def test_prompt_says_24_hour_hours_are_never_asked():
    assert 'Hours 13-23 and 0 ("в 17", "00:15") are 24h, never ambiguous: never ask.' in SYSTEM_PROMPT
