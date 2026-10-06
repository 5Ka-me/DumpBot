from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from dumpbot import clock
from dumpbot.ai.client import (
    AIBadResponse,
    AIConfigError,
    AIModelNotFound,
    AIUnavailable,
    ChutesClient,
    parse_json_object,
    parse_retry_after,
    strip_think,
)
from dumpbot.db.models import AiUsage

GEMMA = "google/gemma-4-31B-turbo-TEE"
QWEN = "Qwen/Qwen3-32B-TEE"
MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "additionalProperties": False}


async def test_chat_happy_path(fake, client, settings):
    fake.chat_ok('{"a": 1}', prompt_tokens=1000, completion_tokens=200)

    res = await client.chat(purpose="extract", model=GEMMA, messages=MSGS, json_schema=SCHEMA, schema_name="t")

    assert res.content == '{"a": 1}'
    assert res.model == GEMMA
    assert (res.input_tokens, res.output_tokens) == (1000, 200)
    assert res.cost_usd == pytest.approx(1000 * 0.12 / 1e6 + 200 * 0.37 / 1e6)
    assert res.latency_ms >= 0
    body = fake.chat_requests[0]
    assert body["model"] == GEMMA
    assert body["messages"] == MSGS
    assert body["seed"] == 7
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == settings.llm_max_tokens
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "t", "schema": SCHEMA, "strict": True},
    }
    assert fake.chat_auth[0] == "Bearer cpk_test"


async def test_chat_without_schema_sends_no_response_format(fake, client):
    fake.chat_ok("Привет")
    res = await client.chat(purpose="answer", model=GEMMA, messages=MSGS, max_tokens=50, temperature=0.2)
    body = fake.chat_requests[0]
    assert res.content == "Привет"
    assert "response_format" not in body
    assert body["max_tokens"] == 50
    assert body["temperature"] == 0.2


async def test_usage_recorded_for_success_and_error(fake, settings, sessionmaker):
    client = ChutesClient(settings, sessionmaker)
    fake.chat_ok("ok", prompt_tokens=500, completion_tokens=100)
    fake.chat_error(503, "No instances available (yet) for chute_id=abc")
    try:
        await client.chat(purpose="extract", model=GEMMA, messages=MSGS)
        with pytest.raises(AIUnavailable):
            await client.chat(purpose="vision", model=GEMMA, messages=MSGS)
    finally:
        await client.close()

    async with sessionmaker() as s:
        rows = (await s.scalars(select(AiUsage).order_by(AiUsage.id))).all()
    assert len(rows) == 2
    ok, err = rows
    assert (ok.ok, ok.purpose, ok.model, ok.input_tokens, ok.output_tokens) == (True, "extract", GEMMA, 500, 100)
    assert ok.cost_usd == Decimal(f"{500 * 0.12 / 1e6 + 100 * 0.37 / 1e6:.6f}")
    assert (err.ok, err.purpose, err.cost_usd) == (False, "vision", Decimal("0"))
    assert "503" in err.error and "No instances available" in err.error


@pytest.mark.parametrize(
    ("status", "body", "exc"),
    [
        (503, "No instances available (yet) for chute_id=1234", AIUnavailable),
        (500, '{"detail":"exhausted all available targets"}', AIUnavailable),
        (502, "<html><body>Bad gateway</body></html>", AIUnavailable),
        (408, "request timeout", AIUnavailable),
        (401, '{"detail":"Invalid token"}', AIConfigError),
        (402, '{"detail":"insufficient balance"}', AIConfigError),
        (403, "forbidden", AIConfigError),
        (404, '{"detail":"model not found"}', AIConfigError),
        (404, '{"detail":"No matching cord found!"}', AIConfigError),
        (418, "teapot", AIBadResponse),
    ],
)
async def test_http_errors_are_mapped(fake, client, status, body, exc):
    fake.chat_error(status, body)
    with pytest.raises(exc):
        await client.chat(purpose="extract", model=GEMMA, messages=MSGS)
    assert len(fake.chat_requests) == 1  # no retries outside the 400 ladder


async def test_only_404_means_model_not_found(fake, client):
    # A missing model is not a global problem (another model may work); a bad key / balance is.
    fake.chat_error(404, '{"detail":"model not found: google/gemma-4-31B-turbo-TEE"}')
    fake.chat_error(401, '{"detail":"Invalid token"}')
    with pytest.raises(AIModelNotFound):
        await client.chat(purpose="extract", model=GEMMA, messages=MSGS)
    with pytest.raises(AIConfigError) as ei:
        await client.chat(purpose="extract", model=GEMMA, messages=MSGS)
    assert not isinstance(ei.value, AIModelNotFound)


async def test_finish_reason_is_reported(fake, client):
    fake.chat_ok('{"a": "cut her', finish_reason="length")
    fake.chat_raw({"choices": [{"message": {"content": "{}"}}]})
    assert (await client.chat(purpose="extract", model=GEMMA, messages=MSGS)).finish_reason == "length"
    assert (await client.chat(purpose="extract", model=GEMMA, messages=MSGS)).finish_reason is None


async def test_429_with_retry_after(fake, client):
    html = "<html><head><title>429 Too Many Requests</title></head><body><h1>429</h1>nginx</body></html>"
    fake.chat_error(429, html, headers={"Retry-After": "30", "Content-Type": "text/html"})
    with pytest.raises(AIUnavailable) as ei:
        await client.chat(purpose="extract", model=GEMMA, messages=MSGS)
    assert ei.value.retry_after == 30.0
    assert "<" not in str(ei.value)  # HTML tags are dropped from the message


async def test_429_without_retry_after(fake, client):
    fake.chat_error(429, "slow down")
    with pytest.raises(AIUnavailable) as ei:
        await client.chat(purpose="extract", model=GEMMA, messages=MSGS)
    assert ei.value.retry_after is None


async def test_400_json_schema_falls_back_to_json_object_and_is_remembered(fake, client):
    fake.chat_error(400, "json_schema response_format is not supported")
    fake.chat_ok('{"a": 1}')
    fake.chat_ok('{"a": 2}')

    first = await client.chat(purpose="extract", model=GEMMA, messages=MSGS, json_schema=SCHEMA)
    second = await client.chat(purpose="extract", model=GEMMA, messages=MSGS, json_schema=SCHEMA)

    assert (first.content, second.content) == ('{"a": 1}', '{"a": 2}')
    formats = [b.get("response_format", {}).get("type") for b in fake.chat_requests]
    assert formats == ["json_schema", "json_object", "json_object"]  # the second call starts at the level that worked
    assert all(b["chat_template_kwargs"] == {"enable_thinking": False} for b in fake.chat_requests)


async def test_400_level_is_remembered_per_model(fake, client):
    fake.chat_error(400, "no json_schema")
    fake.chat_ok("{}")
    fake.chat_ok("{}")
    await client.chat(purpose="extract", model=GEMMA, messages=MSGS, json_schema=SCHEMA)
    await client.chat(purpose="extract", model=QWEN, messages=MSGS, json_schema=SCHEMA)
    assert fake.chat_requests[2]["response_format"]["type"] == "json_schema"


async def test_400_ladder_ends_in_plain_request_then_bad_response(fake, client):
    for _ in range(3):
        fake.chat_error(400, "bad request")
    with pytest.raises(AIBadResponse):
        await client.chat(purpose="extract", model=GEMMA, messages=MSGS, json_schema=SCHEMA)
    assert len(fake.chat_requests) == 3
    last = fake.chat_requests[2]
    assert "response_format" not in last
    assert "chat_template_kwargs" not in last


async def test_400_without_schema_drops_chat_template_kwargs(fake, client):
    fake.chat_error(400, "unknown field chat_template_kwargs")
    fake.chat_ok("ответ")
    res = await client.chat(purpose="answer", model=GEMMA, messages=MSGS)
    assert res.content == "ответ"
    assert len(fake.chat_requests) == 2
    assert "chat_template_kwargs" in fake.chat_requests[0]
    assert "chat_template_kwargs" not in fake.chat_requests[1]


async def test_think_block_is_stripped(fake, client):
    fake.chat_ok('<think>maybe {"x": 0}?</think>\n{"a": 1}', reasoning="secret chain of thought")
    res = await client.chat(purpose="extract", model=GEMMA, messages=MSGS, json_schema=SCHEMA)
    assert res.content == '{"a": 1}'


async def test_missing_usage_is_estimated_from_text(fake, client):
    fake.chat_ok("x" * 300, usage=False)
    res = await client.chat(purpose="answer", model=GEMMA, messages=[{"role": "user", "content": "y" * 90}])
    assert (res.input_tokens, res.output_tokens) == (30, 100)
    assert res.cost_usd == pytest.approx(30 * 0.12 / 1e6 + 100 * 0.37 / 1e6)


async def test_image_parts_pass_through_and_are_not_counted_as_text(fake, client):
    fake.chat_ok("{}", usage=False)
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 3000}}
    msgs = [{"role": "user", "content": [{"type": "text", "text": "z" * 30}, image]}]
    res = await client.chat(purpose="vision", model=GEMMA, messages=msgs)
    assert fake.chat_requests[0]["messages"] == msgs
    assert res.input_tokens == 10


@pytest.mark.parametrize(
    "body", [{"error": "something"}, {"choices": []}, {"choices": [{"message": None}]}, "<html>not json</html>"]
)
async def test_malformed_200_is_bad_response(fake, client, body):
    fake.chat_raw(body)
    with pytest.raises(AIBadResponse):
        await client.chat(purpose="extract", model=GEMMA, messages=MSGS)


async def test_timeout_is_unavailable(fake, make_settings):
    fake.chat_ok("{}", delay=1.0)
    client = ChutesClient(make_settings(llm_timeout_s=0.2))
    try:
        with pytest.raises(AIUnavailable):
            await client.chat(purpose="extract", model=GEMMA, messages=MSGS)
    finally:
        await client.close()


async def test_connection_error_is_unavailable(make_settings):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = ChutesClient(make_settings(chutes_base_url=f"http://127.0.0.1:{port}/v1", llm_timeout_s=3.0))
    try:
        with pytest.raises(AIUnavailable):
            await client.chat(purpose="extract", model=GEMMA, messages=MSGS)
    finally:
        await client.close()


async def test_external_http_session_is_not_closed(fake, settings):
    import aiohttp

    async with aiohttp.ClientSession() as http:
        client = ChutesClient(settings, http=http)
        fake.chat_ok("{}")
        await client.chat(purpose="extract", model=GEMMA, messages=MSGS)
        await client.close()
        assert not http.closed


# --- prices -----------------------------------------------------------------------------------------------------


async def test_price_cache_and_refresh_after_6h(fake, client, monkeypatch):
    t0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(clock, "utcnow", lambda: t0)
    assert await client.price(GEMMA) == (0.12, 0.37)
    assert await client.price(QWEN) == (0.104, 0.416)
    assert fake.models_hits == 1

    monkeypatch.setattr(clock, "utcnow", lambda: t0 + timedelta(hours=5, minutes=59))
    await client.price(GEMMA)
    assert fake.models_hits == 1

    monkeypatch.setattr(clock, "utcnow", lambda: t0 + timedelta(hours=6, minutes=1))
    await client.price(GEMMA)
    assert fake.models_hits == 2


@pytest.mark.parametrize(
    ("model", "cached", "expected"),
    [
        (GEMMA, 2500, 500 * 0.12 + 2500 * 0.012 + 200 * 0.37),  # cached prefix at the input_cache_read price
        (QWEN, 2500, 3000 * 0.104 + 200 * 0.416),  # no input_cache_read listed: no discount assumed
        (GEMMA, 9999, 3000 * 0.012 + 200 * 0.37),  # never more cached than prompt tokens
        (GEMMA, "n/a", 3000 * 0.12 + 200 * 0.37),
    ],
)
async def test_cached_prompt_tokens_are_billed_at_the_cache_read_price(
    fake, settings, sessionmaker, model, cached, expected
):
    client = ChutesClient(settings, sessionmaker)
    fake.chat_ok("{}", prompt_tokens=3000, completion_tokens=200, cached_tokens=cached)
    try:
        res = await client.chat(purpose="extract", model=model, messages=MSGS)
    finally:
        await client.close()
    assert res.cost_usd == pytest.approx(expected / 1e6)
    async with sessionmaker() as s:
        row = await s.scalar(select(AiUsage))
    assert (row.input_tokens, row.cost_usd) == (3000, Decimal(f"{expected / 1e6:.6f}"))


async def test_price_list_is_fetched_without_the_api_key(fake, client):
    # GET /models is public but answers 401 to a bad key: a broken key must not also break prices.
    assert await client.price(GEMMA) == (0.12, 0.37)
    assert fake.models_auth == [None]


async def test_unknown_model_uses_fallback_prices(fake, make_settings):
    client = ChutesClient(make_settings(llm_fallback_price_in=1.5, llm_fallback_price_out=4.5))
    try:
        assert await client.price("some/unknown-model") == (1.5, 4.5)
        assert await client.price("broken/no-pricing") == (1.5, 4.5)
    finally:
        await client.close()


async def test_models_endpoint_down_uses_fallback_prices_and_retries_later(fake, client, monkeypatch):
    t0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(clock, "utcnow", lambda: t0)
    fake.set_models("boom", status=500)
    fake.chat_ok("{}", prompt_tokens=1_000_000, completion_tokens=1_000_000)

    res = await client.chat(purpose="extract", model=GEMMA, messages=MSGS)
    assert res.cost_usd == pytest.approx(1.0 + 3.0)  # settings.llm_fallback_price_*
    assert await client.price(GEMMA) == (1.0, 3.0)
    assert fake.models_hits == 1  # the failure is cached for a while

    fake.set_models()
    monkeypatch.setattr(clock, "utcnow", lambda: t0 + timedelta(minutes=11))
    assert await client.price(GEMMA) == (0.12, 0.37)
    assert fake.models_hits == 2


# --- helpers ----------------------------------------------------------------------------------------------------


def test_strip_think_variants():
    assert strip_think("<think>a</think>b") == "b"
    assert strip_think("<THINK>x</THINK>  y ") == "y"
    assert strip_think("reasoning without opening tag</think>answer") == "answer"
    assert strip_think("answer<think>unterminated thoughts") == "answer"
    assert strip_think("plain") == "plain"


def test_parse_json_object_handles_fences_prose_and_think():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('```\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": {"b": 2}} hope it helps {"c": 3}') == {"a": {"b": 2}}
    assert parse_json_object('<think>{"no": 1}</think>```json\n{"a": 3}\n```') == {"a": 3}


@pytest.mark.parametrize("text", ["no json here", '{"a": ', "[1, 2]", ""])
def test_parse_json_object_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_json_object(text)


def test_parse_retry_after(monkeypatch):
    monkeypatch.setattr(clock, "utcnow", lambda: datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC))
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after("Tue, 06 Oct 2026 12:01:00 GMT") == 60.0
    assert parse_retry_after("Tue, 06 Oct 2026 11:00:00 GMT") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
