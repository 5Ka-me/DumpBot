from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from dumpbot.ai.prompts import (
    EXAMPLES,
    MAX_MSG_CHARS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_answer_text,
    build_user_text,
)
from dumpbot.ai.schemas import Extraction, extraction_json_schema
from dumpbot.domain.kinds import KINDS
from dumpbot.domain.prefs import SLOT_NAMES, Prefs

WARSAW = Prefs(tz="Europe/Warsaw")


def _line(prefix: str) -> str:
    return next(line for line in SYSTEM_PROMPT.splitlines() if line.startswith(prefix))


def test_prompt_version():
    assert PROMPT_VERSION == "v1"


def test_static_prompt_explains_every_kind():
    kinds_line = _line("kind:")
    for kind in KINDS:
        assert re.search(rf"\b{kind}\b", kinds_line), kind


def test_static_prompt_lists_every_slot_with_ru_and_en_words():
    time_line = _line("- time:")
    for slot in SLOT_NAMES:
        assert re.search(rf"\b{slot} \(", time_line), slot
    for phrase in (
        "утром",
        "с утра",
        "в обед",
        "днём",
        "после обеда",
        "вечером",
        "после работы",
        "ночью",
        "перед сном",
    ):
        assert phrase in time_line, phrase
    for phrase in ("in the morning", "at lunch", "after work", "tonight"):
        assert phrase in time_line, phrase


def test_static_prompt_describes_the_when_grammar_and_rules():
    for token in ("+Nw", "+Nmo", "+Ny", "next FR", "d-1", "MM-DD", "YYYY-MM-DD", "weekend", "in_min", "at:", "role:"):
        assert token in SYSTEM_PROMPT, token
    for rule in ("NEVER compute dates", "за 30 минут", "RFC 5545 without DTSTART", "Hours 1-7", "Past facts"):
        assert rule in SYSTEM_PROMPT, rule
    for word in ("query", "edit", "save", "date_field", "answer", "no_reminder", "stop_recurrence", "image.text"):
        assert word in SYSTEM_PROMPT, word


def test_static_prompt_size_budget():
    # ~1500 tokens: English ~4 chars/token, Cyrillic ~3 chars/token.
    cyrillic = len(re.findall("[а-яА-ЯёЁ]", SYSTEM_PROMPT))
    estimate = cyrillic / 3 + (len(SYSTEM_PROMPT) - cyrillic) / 4
    assert estimate < 1650


def test_examples_are_valid_extractions_and_rendered():
    for src, out in EXAMPLES:
        Extraction.model_validate(out)
        assert f"> {src}\n{json.dumps(out, ensure_ascii=False, separators=(',', ':'))}" in SYSTEM_PROMPT


def test_examples_cover_the_required_cases():
    outputs = [json.dumps(out, ensure_ascii=False) for _, out in EXAMPLES]
    inputs = [src for src, _ in EXAMPLES]
    joined = "\n".join(outputs)
    assert '"time": "lunch"' in joined and '"kind": "purchase"' in joined
    assert '"lead": [30]' in joined
    assert "FREQ=WEEKLY;BYDAY=MO" in joined
    assert "FREQ=MONTHLY;BYMONTHDAY=5" in joined and '"day": "d5"' in joined
    assert any(len(out.get("items", [])) == 2 for _, out in EXAMPLES)
    assert '"time": "after_work"' in joined and "buy milk after work" in inputs
    assert '"ask"' in joined
    assert '"intent": "query"' in joined
    assert '"target":' in joined
    assert any(src.startswith("REPLYING TO ITEM") for src in inputs)
    assert '"kind": "meme"' in joined and '"image"' in joined
    assert '"kind": "link"' in joined


def test_json_schema_is_serializable():
    schema = extraction_json_schema()
    text = json.dumps(schema)
    assert json.loads(text)["additionalProperties"] is False


def test_build_user_text_with_all_context_lines():
    now = datetime(2026, 10, 6, 12, 5, tzinfo=UTC)
    text = build_user_text(
        text="  купить молоко\nи хлеб ",
        source="forward",
        now=now,
        prefs=WARSAW,
        forward_from="канал «Хабр»",
        link_titles={"https://a.example/x": "Title\nwith   newline"},
        reply_item={"id": 1, "title": "Созвон", "due_local": "2026-10-09 13:00"},
        clarification="20:00",
        force_query=True,
    )
    assert text.split("\n") == [
        "NOW 2026-10-06T14:05 Tue Europe/Warsaw",
        "CAL Wed 2026-10-07, Thu 2026-10-08, Fri 2026-10-09, Sat 2026-10-10, Sun 2026-10-11, Mon 2026-10-12, "
        "Tue 2026-10-13",
        "SRC forward",
        "FORWARDED FROM канал «Хабр»",
        "LINKS",
        "https://a.example/x: Title with newline",
        'REPLYING TO ITEM {"id": 1, "title": "Созвон", "due_local": "2026-10-09 13:00"}',
        "CLARIFICATION 20:00",
        "MODE query",
        "MSG",
        "купить молоко",
        "и хлеб",
    ]


def test_build_user_text_minimal_after_dst_change():
    now = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)  # 11:00 CET, the day clocks go back
    text = build_user_text(text="", source="voice", now=now, prefs=WARSAW)
    assert text.split("\n") == [
        "NOW 2026-10-25T11:00 Sun Europe/Warsaw",
        "CAL Mon 2026-10-26, Tue 2026-10-27, Wed 2026-10-28, Thu 2026-10-29, Fri 2026-10-30, Sat 2026-10-31, "
        "Sun 2026-11-01",
        "SRC voice",
        "MSG",
        "(empty)",
    ]


@pytest.mark.parametrize("size", [4096, MAX_MSG_CHARS + 500])
def test_build_user_text_keeps_telegram_sized_messages_whole(size):
    now = datetime(2026, 10, 6, 12, 5, tzinfo=UTC)
    text = build_user_text(text="я" * size, source="text", now=now, prefs=WARSAW)
    msg = text.split("MSG\n", 1)[1]
    assert len(msg) == min(size, MAX_MSG_CHARS)


def test_build_answer_text():
    now = datetime(2026, 10, 6, 12, 5, tzinfo=UTC)
    text = build_answer_text("что  у меня\nзавтра?", [{"title": "Созвон", "due": "2026-10-07 10:00"}], now, WARSAW)
    assert text == (
        "NOW 2026-10-06T14:05 Tue Europe/Warsaw\n"
        "QUESTION что у меня завтра?\n"
        'ROWS [{"title":"Созвон","due":"2026-10-07 10:00"}]'
    )
