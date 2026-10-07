"""AI side of checklists and related items (docs/features/iteration-2.md §C): the schema (``related`` / ``steps`` /
``EditPatch.action = "split"``, lenient), the RELATED CANDIDATES prompt line, the v3 prompt and examples, and the
"split" call (prompt, ladder, usage purpose)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from dumpbot.ai import schemas
from dumpbot.ai.client import AIUnavailable, ChutesClient
from dumpbot.ai.extract import ExtractInput, SplitInput, extract, split_steps
from dumpbot.ai.prompts import EXAMPLES, SPLIT_SYSTEM_PROMPT, SYSTEM_PROMPT, build_split_text, build_user_text
from dumpbot.ai.schemas import EditPatch, ExtractedItem, Extraction, SplitResult, split_json_schema
from dumpbot.db.models import AiUsage
from dumpbot.domain.prefs import Prefs

GEMMA = "google/gemma-4-31B-turbo-TEE"
QWEN = "Qwen/Qwen3-32B-TEE"
NOW = datetime(2026, 10, 6, 12, 5, tzinfo=UTC)  # 14:05 in Warsaw
WARSAW = Prefs(tz="Europe/Warsaw")
TASK = {"kind": "task", "title": "Переезд", "due_local": "2026-10-10 10:00"}
STEPS = {"steps": [{"title": "Упаковать вещи"}, {"title": "Заказать машину", "when": {"day": "TH", "time": "15:00"}}]}


# --- schema --------------------------------------------------------------------------------------------------------


def test_related_ids_are_cleaned_not_rejected():
    item = ExtractedItem.model_validate(
        {"kind": "task", "title": "Купить чехол", "related": ["12", 12, "Наушники", None, -3, True, 7, 9]}
    )
    assert item.related == [12]  # clipped to 3 first (12, 12, "Наушники"), then cleaned
    item = ExtractedItem.model_validate({"kind": "task", "title": "Купить чехол", "related": [5, "7", 0]})
    assert item.related == [5, 7]


def test_a_lone_or_odd_list_value_is_coerced_or_dropped_never_raised():
    for value, expected in [(12, [12]), ("7", [7]), (True, []), ({"id": 3}, []), (None, []), ([], [])]:
        item = ExtractedItem.model_validate({"kind": "task", "title": "x", "related": value})
        assert item.related == expected, value
    for value, expected in [("Дом", ["дом"]), (5, ["5"]), ({"a": 1}, []), (False, []), (None, [])]:
        assert ExtractedItem.model_validate({"kind": "task", "title": "x", "tags": value}).tags == expected, value
    assert EditPatch.model_validate({"tags_add": 7, "tags_remove": True}).model_dump()["tags_add"] == ["7"]


async def test_a_scalar_related_from_the_model_keeps_the_message(fake, client, settings):
    fake.chat_json({"items": [{"kind": "task", "title": "Купить чехол", "related": 12}]})
    out = await extract(client, settings, ExtractInput(text="купить чехол", source="text", now=NOW, prefs=WARSAW))
    assert out.extraction.items[0].related == [12] and len(fake.chat_requests) == 1


async def test_a_type_error_of_a_validator_is_an_invalid_answer_not_a_crash(fake, client, settings, monkeypatch):
    def boom(value):
        raise TypeError("'int' object is not iterable")

    monkeypatch.setattr(schemas, "_as_list", boom)  # a validator that meets a value it cannot handle
    fake.chat_json({"items": [{"kind": "task", "title": "Купить чехол", "related": 12}]})
    fake.chat_json({"items": [{"kind": "task", "title": "Купить чехол"}]})
    out = await extract(client, settings, ExtractInput(text="купить чехол", source="text", now=NOW, prefs=WARSAW))
    assert out.extraction.items[0].title == "Купить чехол" and len(fake.chat_requests) == 2
    assert "not iterable" in fake.chat_requests[1]["messages"][-1]["content"]  # the usual repair round


def test_steps_are_clipped_to_eight_and_validated():
    item = ExtractedItem.model_validate(
        {"kind": "task", "title": "Проект", "steps": [{"title": f"Шаг {n}"} for n in range(12)]}
    )
    assert len(item.steps) == 8
    assert item.steps[0].when is None
    assert EditPatch.model_validate({"action": "split"}).action == "split"
    schema = json.dumps(Extraction.model_json_schema())
    assert '"related"' in schema and '"steps"' in schema and '"split"' in schema
    assert split_json_schema()["additionalProperties"] is False


# --- extraction prompt ---------------------------------------------------------------------------------------------


def test_related_candidates_line_comes_right_before_msg():
    related = [{"id": 12, "kind": "purchase", "title": "Купить наушники Sony", "date": "2026-09-12"}]
    text = build_user_text(text="купить чехол", source="text", now=NOW, prefs=WARSAW, related=related)
    lines = text.split("\n")
    assert lines[-3:] == [
        'RELATED CANDIDATES [{"id":12,"kind":"purchase","title":"Купить наушники Sony","date":"2026-09-12"}]',
        "MSG",
        "купить чехол",
    ]
    assert "RELATED" not in build_user_text(text="купить чехол", source="text", now=NOW, prefs=WARSAW)


def test_v3_prompt_explains_related_steps_and_split():
    for phrase in ("RELATED CANDIDATES", "related:", "steps:", "распиши по шагам", "split (разбей на шаги)"):
        assert phrase in SYSTEM_PROMPT, phrase
    outputs = [json.dumps(out, ensure_ascii=False) for _, out in EXAMPLES]
    assert any('"steps"' in o for o in outputs)
    assert any('"related": [12]' in o for o in outputs)
    assert any('"action": "split"' in o for o in outputs)
    assert any(src.startswith("RELATED CANDIDATES ") for src, _ in EXAMPLES)


async def test_extract_sends_the_candidates(fake, client, settings):
    fake.chat_json({"items": [{"kind": "purchase", "title": "Купить чехол", "related": [12, 99]}]})
    related = [{"id": 12, "kind": "purchase", "title": "Купить наушники Sony", "date": "2026-09-12"}]
    inp = ExtractInput(text="купить чехол", source="text", now=NOW, prefs=WARSAW, related=related)
    out = await extract(client, settings, inp)
    assert out.extraction.items[0].related == [12, 99]  # foreign ids are dropped by the items service
    user = fake.chat_requests[0]["messages"][1]["content"]
    assert "\nRELATED CANDIDATES [" in user


# --- the "split" call ----------------------------------------------------------------------------------------------


def test_split_text():
    text = build_split_text(task=TASK, now=NOW, prefs=WARSAW, note="  разбей   на 3 шага ")
    lines = text.split("\n")
    assert lines[0] == "NOW 2026-10-06T14:05 Tue Europe/Warsaw" and lines[1].startswith("CAL Wed 2026-10-07")
    assert lines[2:] == [
        "NOTE разбей на 3 шага",
        'TASK {"kind":"task","title":"Переезд","due_local":"2026-10-10 10:00"}',
    ]
    assert "NOTE" not in build_split_text(task=TASK, now=NOW, prefs=WARSAW)


async def test_split_steps_happy_path(fake, settings, sessionmaker):
    fake.chat_json({"steps": [*STEPS["steps"], {"title": "   "}]})
    client = ChutesClient(settings, sessionmaker)
    try:
        out = await split_steps(client, settings, SplitInput(task=TASK, now=NOW, prefs=WARSAW, note="разбей"))
    finally:
        await client.close()
    assert [s.title for s in out.steps] == ["Упаковать вещи", "Заказать машину"]
    assert out.steps[1].when is not None and out.steps[1].when.day == "TH"
    assert (out.model, out.prompt_version, out.missing_model) == (GEMMA, "v4", None)
    body = fake.chat_requests[0]
    assert body["messages"][0] == {"role": "system", "content": SPLIT_SYSTEM_PROMPT}
    assert body["messages"][1]["content"].endswith(
        'TASK {"kind":"task","title":"Переезд","due_local":"2026-10-10 10:00"}'
    )
    assert body["response_format"]["json_schema"]["name"] == "steps"
    async with sessionmaker() as s:
        assert (await s.scalars(select(AiUsage.purpose))).all() == ["split"]


async def test_split_steps_falls_back_and_repairs(fake, client, settings):
    fake.chat_error(503, "No instances available (yet) for chute")
    fake.chat_json(STEPS)
    out = await split_steps(client, settings, SplitInput(task=TASK, now=NOW, prefs=WARSAW))
    assert out.model == QWEN and fake.chat_models == [GEMMA, QWEN]

    fake.chat_requests.clear()
    fake.chat_json({"steps": [{"title": ""}]})  # invalid: one repair round with the error
    fake.chat_json({"steps": []})
    out = await split_steps(client, settings, SplitInput(task=TASK, now=NOW, prefs=WARSAW))
    assert out.steps == [] and len(fake.chat_requests) == 2
    assert "Your JSON is invalid" in fake.chat_requests[1]["messages"][-1]["content"]


async def test_split_steps_unavailable_everywhere(fake, client, settings):
    fake.chat_error(503, "No instances available")
    fake.chat_error(503, "No instances available")
    with pytest.raises(AIUnavailable):
        await split_steps(client, settings, SplitInput(task=TASK, now=NOW, prefs=WARSAW))
    assert SplitResult.model_validate(STEPS).steps[0].title == "Упаковать вещи"


async def test_reply_with_steps_instead_of_an_edit_becomes_split(fake, client, settings):
    fake.chat_json({"items": [{"kind": "task", "title": "Переезд", "steps": STEPS["steps"]}]})
    inp = ExtractInput(
        text="разбей на шаги", source="text", now=NOW, prefs=WARSAW, reply_item={"id": 3, "title": "Переезд"}
    )
    out = await extract(client, settings, inp)
    assert out.extraction.intent == "edit" and out.extraction.edit.action == "split"
