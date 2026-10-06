from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dumpbot.ai.prompts import (
    EXAMPLES,
    MAX_MSG_CHARS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_answer_text,
    build_user_text,
)
from dumpbot.ai.schemas import MAX_ITEMS, Extraction, When, extraction_json_schema
from dumpbot.domain.kinds import KINDS
from dumpbot.domain.prefs import SLOT_NAMES, Prefs

WARSAW = Prefs(tz="Europe/Warsaw")


def _line(prefix: str) -> str:
    return next(line for line in SYSTEM_PROMPT.splitlines() if line.startswith(prefix))


def test_prompt_version():
    assert PROMPT_VERSION == "v3"


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
    # ~2200 tokens: English ~4 chars/token, Cyrillic ~3 chars/token. v2 added the period grammar and its examples
    # (~350 tokens), v3 related items and steps (~300); the static prompt is a cacheable prefix.
    cyrillic = len(re.findall("[а-яА-ЯёЁ]", SYSTEM_PROMPT))
    estimate = cyrillic / 3 + (len(SYSTEM_PROMPT) - cyrillic) / 4
    assert estimate < 2300


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


def test_overlong_model_output_is_clipped_not_rejected():
    # Without constrained decoding one long link title or a long list must not fail the whole message.
    out = Extraction.model_validate(
        {
            "items": [{"kind": "purchase", "title": f"Купить {n}"} for n in range(1, 26)],
            "image": {"text": "OCR " * 1500, "description": "Скриншот статьи " * 30},
            "ask": {"item": 12, "q": "Когда?", "options": ["a", "b", "c", "d", "e"]},
        }
    )
    assert len(out.items) == MAX_ITEMS == 20 and out.items[-1].title == "Купить 20"
    assert len(out.image.text) <= 4000 and out.image.description.endswith("…")
    assert out.ask.item == 12 and out.ask.options == ["a", "b", "c", "d"]

    link = Extraction.model_validate(
        {
            "items": [
                {
                    "kind": "link",
                    "title": "Очень длинный заголовок страницы " * 6,  # linkinfo keeps up to 200 chars
                    "details": "д" * 700,
                    "tags": list("abcdefg"),
                    "when": {"text": "когда-нибудь потом " * 6},
                }
            ]
        }
    )
    [item] = link.items
    assert len(item.title) <= 120 and item.title.startswith("Очень длинный") and item.title.endswith("…")
    assert len(item.details) == 600 and item.tags == ["a", "b", "c", "d", "e", "f"]
    assert len(item.when.text) <= 80


def test_json_schema_keeps_the_limits_as_decoding_hints():
    schema = extraction_json_schema()
    item = schema["$defs"]["ExtractedItem"]["properties"]
    assert schema["properties"]["items"]["maxItems"] == MAX_ITEMS
    assert item["title"]["maxLength"] == 120 and item["tags"]["maxItems"] == 6
    assert schema["$defs"]["ImageInfo"]["properties"]["text"]["anyOf"][0]["maxLength"] == 4000


def test_static_prompt_states_the_limits():
    # json_object / plain requests carry no schema: the prompt is the only place the model learns them.
    items_line = _line("ITEMS:")
    assert f"at most {MAX_ITEMS}" in items_line and "ONE purchase item with the list in details" in items_line
    assert "max 120 chars" in SYSTEM_PROMPT and "max 4000 chars" in SYSTEM_PROMPT


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


# --- v2: fuzzy time (docs/features/iteration-2.md §A) --------------------------------------------------------------


def test_static_prompt_describes_the_period_grammar():
    period_line = _line("- period")
    for token in (
        "this_week",
        "next_week",
        "this_month",
        "next_month",
        "YYYY-MM",
        "spring",
        "summer",
        "autumn",
        "winter",
        "soon",
        "someday",
    ):
        assert re.search(rf"\b{token}\b", period_line), token
    for phrase in ("на этой неделе", "на следующей неделе", "в октябре", "летом", "зимой", "на днях", "когда-нибудь"):
        assert phrase in period_line, phrase
    role_line = _line("- role:")
    assert "до пятницы" in role_line and "до конца" in role_line and "period this_week/this_month" in role_line


def test_examples_cover_fuzzy_time():
    by_input = dict(EXAMPLES)
    expected = {
        "на этой неделе посмотреть Raspberry Pi": {"text": "на этой неделе", "period": "this_week"},
        "в октябре поменять шины": {"text": "в октябре", "period": "2026-10"},
        "до пятницы сдать отчёт": {"text": "до пятницы", "day": "FR", "role": "deadline"},
        "на днях позвонить Пете": {"text": "на днях", "period": "soon"},
        "когда-нибудь выучить испанский": {"text": "когда-нибудь", "period": "someday"},
    }
    for src, when in expected.items():
        [item] = by_input[src]["items"]
        assert item["when"] == when, src
    edits = [out["edit"] for src, out in EXAMPLES if src.startswith("REPLYING TO ITEM") and "edit" in out]
    assert {"text": "на следующей неделе", "period": "next_week"} in [e.get("when") for e in edits]


@pytest.mark.parametrize(
    "period",
    [
        "this_week",
        "next_week",
        "this_month",
        "next_month",
        "2026-10",
        "2027-03",
        "spring",
        "summer",
        "autumn",
        "winter",
        "soon",
        "someday",
    ],
)
def test_when_accepts_every_period(period: str):
    assert When(text="x", period=period).period == period


@pytest.mark.parametrize("period", ["week", "2026-1", "2026-10-01", "next_year", "This_week", "someday "])
def test_when_rejects_unknown_periods(period: str):
    with pytest.raises(ValidationError):
        When(text="x", period=period)


def test_json_schema_offers_the_period_to_the_model():
    when = extraction_json_schema()["$defs"]["When"]["properties"]["period"]
    patterns = [opt.get("pattern") for opt in when.get("anyOf", [when])]
    assert any(p and "this_week" in p and "someday" in p for p in patterns)
