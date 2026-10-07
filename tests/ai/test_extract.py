from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from dumpbot.ai.client import AIBadResponse, AIConfigError, AIModelNotFound, AIUnavailable, ChutesClient
from dumpbot.ai.extract import LONG_MAX_TOKENS, PROMPT_VERSION, ExtractInput, answer_query, extract
from dumpbot.ai.prompts import ANSWER_SYSTEM_PROMPT, SYSTEM_PROMPT
from dumpbot.db.models import AiUsage
from dumpbot.domain.prefs import Prefs

GEMMA = "google/gemma-4-31B-turbo-TEE"
QWEN = "Qwen/Qwen3-32B-TEE"
QWEN_VL = "Qwen/Qwen3.6-27B-TEE"
NOW = datetime(2026, 10, 6, 12, 5, tzinfo=UTC)  # 14:05 in Warsaw (CEST)
GOOD = {
    "intent": "save",
    "items": [{"kind": "purchase", "title": "Купить наушники", "when": {"text": "в обед", "time": "lunch"}}],
}
UNAVAILABLE = "No instances available (yet) for chute_id=abc"
NOT_FOUND = '{"detail":"model not found: google/gemma-4-31B-turbo-TEE"}'


def make_input(**overrides) -> ExtractInput:
    values = {"text": "купить наушники в обед", "source": "text", "now": NOW, "prefs": Prefs(tz="Europe/Warsaw")}
    values.update(overrides)
    return ExtractInput(**values)


def user_text(request: dict) -> str:
    content = request["messages"][1]["content"]
    return content if isinstance(content, str) else content[0]["text"]


async def test_extract_happy_path(fake, client, settings):
    fake.chat_json(GOOD)

    out = await extract(client, settings, make_input())

    assert out.model == GEMMA
    assert out.prompt_version == PROMPT_VERSION == "v5"
    assert out.extraction.intent == "save"
    assert out.extraction.items[0].when.time == "lunch"
    body = fake.chat_requests[0]
    assert body["model"] == settings.llm_model
    assert body["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert body["response_format"]["json_schema"]["name"] == "extraction"
    assert body["response_format"]["json_schema"]["strict"] is True
    text = user_text(body)
    assert text.startswith("NOW 2026-10-06T14:05 Tue Europe/Warsaw\nCAL Wed 2026-10-07,")
    assert "\nSRC text\n" in text
    assert text.endswith("MSG\nкупить наушники в обед")


async def test_think_and_fenced_json_are_accepted(fake, client, settings):
    fake.chat_ok("<think>hmm</think>\n```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```")
    out = await extract(client, settings, make_input())
    assert out.extraction.items[0].title == "Купить наушники"
    assert len(fake.chat_requests) == 1


async def test_validation_error_is_retried_with_the_error(fake, client, settings):
    fake.chat_json({"items": [{"kind": "task", "title": "Сделать", "when": {"day": "tomorrow"}}]})
    fake.chat_json(GOOD)

    out = await extract(client, settings, make_input())

    assert out.model == GEMMA
    assert fake.chat_models == [GEMMA, GEMMA]
    retry = fake.chat_requests[1]["messages"]
    assert retry[:2] == fake.chat_requests[0]["messages"]
    assert retry[2]["role"] == "assistant" and '"tomorrow"' in retry[2]["content"]
    assert retry[3]["role"] == "user" and "items.0.when.day" in retry[3]["content"]


async def test_invalid_json_retry_then_fallback_then_bad_response(fake, client, settings):
    fake.chat_ok("Sorry, I cannot help with that.")
    fake.chat_json({"intent": "save", "items": [{"kind": "spaceship", "title": "x"}]})
    fake.chat_ok('{"items": [')

    with pytest.raises(AIBadResponse):
        await extract(client, settings, make_input())

    assert fake.chat_models == [GEMMA, GEMMA, QWEN]
    retry = fake.chat_requests[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": "Sorry, I cannot help with that."}
    assert "invalid" in retry[-1]["content"]
    assert len(fake.chat_requests[2]["messages"]) == 2  # the fallback starts from the clean prompt


async def test_fallback_fixes_invalid_primary_output(fake, client, settings):
    fake.chat_ok("nope")
    fake.chat_ok("still nope")
    fake.chat_json(GOOD)
    out = await extract(client, settings, make_input())
    assert out.model == QWEN
    assert fake.chat_models == [GEMMA, GEMMA, QWEN]


async def test_primary_unavailable_goes_straight_to_fallback(fake, client, settings):
    fake.chat_error(503, UNAVAILABLE)
    fake.chat_json(GOOD)
    out = await extract(client, settings, make_input())
    assert out.model == QWEN
    assert fake.chat_models == [GEMMA, QWEN]


async def test_both_unavailable_raises_unavailable(fake, client, settings):
    fake.chat_error(503, UNAVAILABLE)
    fake.chat_error(429, "Too Many Requests", headers={"Retry-After": "60"})
    with pytest.raises(AIUnavailable) as ei:
        await extract(client, settings, make_input())
    assert ei.value.retry_after == 60.0
    assert fake.chat_models == [GEMMA, QWEN]


async def test_primary_unavailable_and_fallback_invalid_raises_unavailable(fake, client, settings):
    fake.chat_error(503, UNAVAILABLE)
    fake.chat_ok("garbage")
    with pytest.raises(AIUnavailable):
        await extract(client, settings, make_input())


async def test_primary_invalid_and_fallback_unavailable_raises_unavailable(fake, client, settings):
    fake.chat_ok("garbage")
    fake.chat_ok("garbage again")
    fake.chat_error(500, "exhausted all available targets")
    with pytest.raises(AIUnavailable):
        await extract(client, settings, make_input())


async def test_config_error_is_not_masked_by_fallback(fake, client, settings):
    fake.chat_error(401, '{"detail":"Invalid token"}')
    with pytest.raises(AIConfigError):
        await extract(client, settings, make_input())
    assert fake.chat_models == [GEMMA]


async def test_primary_model_not_found_goes_to_fallback(fake, client, settings):
    # chutes retired / renamed the primary model: the fallback keeps the bot working (and the owner is told).
    fake.chat_error(404, NOT_FOUND)
    fake.chat_json(GOOD)
    out = await extract(client, settings, make_input())
    assert (out.model, out.missing_model) == (QWEN, GEMMA)
    assert fake.chat_models == [GEMMA, QWEN]


async def test_fallback_used_for_other_reasons_reports_no_missing_model(fake, client, settings):
    fake.chat_error(503, UNAVAILABLE)
    fake.chat_json(GOOD)
    out = await extract(client, settings, make_input())
    assert (out.model, out.missing_model) == (QWEN, None)


async def test_both_models_not_found_is_a_config_error(fake, client, settings):
    fake.chat_error(404, NOT_FOUND)
    fake.chat_error(404, "No matching cord found!")
    with pytest.raises(AIModelNotFound) as ei:
        await extract(client, settings, make_input())
    assert isinstance(ei.value, AIConfigError) and "gemma" in str(ei.value)
    assert fake.chat_models == [GEMMA, QWEN]


async def test_primary_not_found_without_fallback_is_a_config_error(fake, make_settings):
    settings = make_settings(llm_fallback_model="")
    client = ChutesClient(settings)
    fake.chat_error(404, NOT_FOUND)
    try:
        with pytest.raises(AIModelNotFound):
            await extract(client, settings, make_input())
    finally:
        await client.close()
    assert fake.chat_models == [GEMMA]


async def test_paid_primary_answers_and_missing_fallback_are_not_a_config_error(fake, client, settings):
    # A config error would retry the job hourly forever, paying for both primary calls every time.
    fake.chat_ok("garbage")
    fake.chat_ok("garbage again")
    fake.chat_error(404, NOT_FOUND)
    with pytest.raises(AIBadResponse):
        await extract(client, settings, make_input())
    assert fake.chat_models == [GEMMA, GEMMA, QWEN]


async def test_primary_unavailable_and_fallback_not_found_raises_unavailable(fake, client, settings):
    fake.chat_error(503, UNAVAILABLE, headers={"Retry-After": "30"})
    fake.chat_error(404, NOT_FOUND)
    with pytest.raises(AIUnavailable) as ei:
        await extract(client, settings, make_input())
    assert ei.value.retry_after == 30.0


async def test_primary_not_found_and_fallback_invalid_is_bad_response(fake, client, settings):
    fake.chat_error(404, NOT_FOUND)
    fake.chat_ok("garbage")
    with pytest.raises(AIBadResponse):
        await extract(client, settings, make_input())


async def test_output_cut_at_max_tokens_is_asked_again_with_more_room(fake, client, settings):
    # A long OCR text: asking to "fix the JSON" with the same cap would be cut at the same place.
    fake.chat_ok('{"image": {"text": "Длинный текст скриншота, обрезанный посере', finish_reason="length")
    fake.chat_json(GOOD)

    out = await extract(client, settings, make_input())

    assert out.model == GEMMA
    assert [r["max_tokens"] for r in fake.chat_requests] == [settings.llm_max_tokens, LONG_MAX_TOKENS]
    assert fake.chat_requests[1]["messages"] == fake.chat_requests[0]["messages"]


async def test_cut_output_gets_more_room_once_per_model(fake, client, settings):
    for _ in range(4):
        fake.chat_ok('{"items": [{"kind": "note", "title": "обре', finish_reason="length")
    fake.chat_json(GOOD)

    out = await extract(client, settings, make_input())

    assert out.model == QWEN
    assert fake.chat_models == [GEMMA, GEMMA, GEMMA, QWEN, QWEN]
    cap, long = settings.llm_max_tokens, LONG_MAX_TOKENS
    assert [r["max_tokens"] for r in fake.chat_requests] == [cap, long, long, cap, long]
    assert "invalid" in fake.chat_requests[2]["messages"][-1]["content"]  # then the usual "fix your JSON" retry


async def test_images_are_routed_to_vision_models(fake, client, settings):
    image = b"\x89PNG\r\n fake image bytes"
    fake.chat_error(503, UNAVAILABLE)
    fake.chat_json({"image": {"text": "", "description": "Кот"}, "items": [{"kind": "meme", "title": "Кот"}]})

    out = await extract(client, settings, make_input(text="", source="photo", images=[(image, "image/png")]))

    assert fake.chat_models == [settings.llm_model, settings.vision_fallback_model]
    content = fake.chat_requests[0]["messages"][1]["content"]
    assert content[0]["type"] == "text"
    assert "\nSRC photo\n" in content[0]["text"] and content[0]["text"].endswith("MSG\n(empty)")
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode()},
    }
    assert out.model == QWEN_VL
    assert out.extraction.image.description == "Кот"
    assert out.extraction.items[0].kind == "meme"


async def test_at_most_four_images_are_sent(fake, client, settings):
    fake.chat_json(GOOD)
    images = [(bytes([i]), "image/jpeg") for i in range(6)]
    await extract(client, settings, make_input(source="photo", images=images))
    content = fake.chat_requests[0]["messages"][1]["content"]
    assert [p["type"] for p in content] == ["text"] + ["image_url"] * 4


async def test_usage_purpose_is_vision_for_images(fake, settings, sessionmaker):
    client = ChutesClient(settings, sessionmaker)
    fake.chat_json(GOOD)
    fake.chat_json(GOOD)
    try:
        await extract(client, settings, make_input())
        await extract(client, settings, make_input(source="photo", images=[(b"img", "image/jpeg")]))
    finally:
        await client.close()
    async with sessionmaker() as s:
        purposes = (await s.scalars(select(AiUsage.purpose).order_by(AiUsage.id))).all()
    assert purposes == ["extract", "vision"]


async def test_force_query_overrides_a_save(fake, client, settings):
    fake.chat_json(GOOD)
    out = await extract(client, settings, make_input(text="?купить наушники", force_query=True))
    assert out.extraction.intent == "query"
    assert out.extraction.items == []
    assert out.extraction.query is not None and out.extraction.query.answer == "summary"
    assert "\nMODE query\n" in user_text(fake.chat_requests[0])


async def test_force_query_keeps_the_models_query(fake, client, settings):
    fake.chat_json({"intent": "query", "query": {"range": "tomorrow", "answer": "list"}})
    out = await extract(client, settings, make_input(text="?что завтра", force_query=True))
    assert out.extraction.query.range == "tomorrow"
    assert out.extraction.query.answer == "list"


async def test_reply_item_forces_edit_without_target(fake, client, settings):
    fake.chat_json({"intent": "edit", "edit": {"target": "созвон", "when": {"text": "на 15:00", "time": "15:00"}}})
    snapshot = {"id": 7, "kind": "event", "title": "Созвон", "due_local": "2026-10-09 13:00"}

    out = await extract(client, settings, make_input(text="перенеси на 15:00", reply_item=snapshot))

    assert out.extraction.intent == "edit"
    assert out.extraction.edit.target is None
    assert out.extraction.edit.when.time == "15:00"
    assert 'REPLYING TO ITEM {"id": 7, "kind": "event", "title": "Созвон"' in user_text(fake.chat_requests[0])


async def test_reply_item_turns_a_saved_item_into_an_edit(fake, client, settings):
    fake.chat_json(
        {"items": [{"kind": "task", "title": "Созвон", "when": {"text": "завтра в 10", "day": "+1", "time": "10:00"}}]}
    )
    out = await extract(client, settings, make_input(text="завтра в 10", reply_item={"id": 1, "title": "Созвон"}))
    assert out.extraction.intent == "edit"
    assert out.extraction.items == []
    assert (out.extraction.edit.when.day, out.extraction.edit.when.time) == ("+1", "10:00")


async def test_context_lines_reach_the_prompt(fake, client, settings):
    fake.chat_json(GOOD)
    await extract(
        client,
        settings,
        make_input(
            text="интересно https://habr.com/p/1/",
            source="forward",
            forward_from="канал «Хабр»",
            link_titles={"https://habr.com/p/1/": "Как устроен asyncio"},
            clarification="20:00",
        ),
    )
    text = user_text(fake.chat_requests[0])
    assert "\nFORWARDED FROM канал «Хабр»\n" in text
    assert "\nLINKS\nhttps://habr.com/p/1/: Как устроен asyncio\n" in text
    assert "\nCLARIFICATION 20:00\n" in text


async def test_message_text_never_reaches_info_logs(fake, client, settings, caplog):
    secret = "секретный текст сообщения"
    fake.chat_json({"items": [{"kind": "bogus", "title": secret}]}, reasoning=f"thinking about {secret}")
    fake.chat_ok(f"not json: {secret}")
    fake.chat_error(503, UNAVAILABLE)

    with caplog.at_level(logging.DEBUG), pytest.raises(AIUnavailable):
        await extract(client, settings, make_input(text=secret))

    assert all(secret not in r.getMessage() for r in caplog.records if r.levelno >= logging.INFO)
    assert any(secret in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


# --- answer_query -----------------------------------------------------------------------------------------------


async def test_answer_query_plain_text(fake, client, settings, warsaw):
    fake.chat_ok("<think>ok</think>Завтра в 10:00 — **созвон** с Олегом.")
    rows = [{"id": 1, "kind": "event", "title": "Созвон с Олегом", "due": "2026-10-07 10:00"}]

    answer = await answer_query(client, settings, "что у меня завтра?", rows, NOW, warsaw)

    assert answer == "Завтра в 10:00 — созвон с Олегом."
    body = fake.chat_requests[0]
    assert body["model"] == GEMMA
    assert "response_format" not in body
    assert body["messages"][0] == {"role": "system", "content": ANSWER_SYSTEM_PROMPT}
    text = body["messages"][1]["content"]
    assert text.startswith("NOW 2026-10-06T14:05 Tue Europe/Warsaw\nQUESTION что у меня завтра?\nROWS [")
    assert "Созвон с Олегом" in text


async def test_answer_query_without_rows_skips_the_llm(fake, client, settings, warsaw):
    answer = await answer_query(client, settings, "что у меня завтра?", [], NOW, warsaw)
    assert answer.startswith("Ничего не нашёл")
    assert fake.chat_requests == []


async def test_answer_query_uses_fallback_when_primary_unavailable(fake, client, settings, warsaw):
    fake.chat_error(503, UNAVAILABLE)
    fake.chat_ok("Ничего на завтра нет.")
    answer = await answer_query(client, settings, "что завтра?", [{"title": "x"}], NOW, warsaw)
    assert answer == "Ничего на завтра нет."
    assert fake.chat_models == [GEMMA, QWEN]


async def test_answer_query_uses_fallback_when_primary_not_found(fake, client, settings, warsaw):
    fake.chat_error(404, NOT_FOUND)
    fake.chat_ok("Завтра созвон.")
    answer = await answer_query(client, settings, "что завтра?", [{"title": "x"}], NOW, warsaw)
    assert answer == "Завтра созвон."
    assert fake.chat_models == [GEMMA, QWEN]


@pytest.mark.parametrize(
    ("primary", "exc"),
    [((404, NOT_FOUND), AIModelNotFound), ((503, UNAVAILABLE), AIUnavailable), ((200, "  "), AIBadResponse)],
)
async def test_answer_query_with_missing_fallback_raises_the_primarys_error(
    fake, client, settings, warsaw, primary, exc
):
    status, body = primary
    if status == 200:
        fake.chat_ok(body)
    else:
        fake.chat_error(status, body)
    fake.chat_error(404, NOT_FOUND)
    with pytest.raises(exc):
        await answer_query(client, settings, "что завтра?", [{"title": "x"}], NOW, warsaw)
    assert fake.chat_models == [GEMMA, QWEN]


async def test_answer_query_records_purpose_answer(fake, settings, sessionmaker, warsaw):
    client = ChutesClient(settings, sessionmaker)
    fake.chat_ok("Ответ.")
    try:
        await answer_query(client, settings, "вопрос", [{"title": "x"}], NOW, warsaw)
    finally:
        await client.close()
    async with sessionmaker() as s:
        assert (await s.scalars(select(AiUsage.purpose))).all() == ["answer"]
