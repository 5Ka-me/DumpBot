"""Fast switch to the fallback model (docs/features/iteration-3.md §1): the pause of a model after a 429 / timeout /
5xx and the parallel ("hedged") request to the fallback when the primary is slow.

The fake chutes delays or fails per model (``model=``); the hedge waits ``HEDGE`` (0.2 s) instead of 12 s, and a
``HANG`` reply ends when the fake server shuts down, so no test sleeps for long.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import socket
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import aiohttp
import pytest
from sqlalchemy import select

from dumpbot.ai.client import HEDGE_LOST, AIConfigError, AIUnavailable, ChutesClient
from dumpbot.ai.extract import ExtractInput, SplitInput, answer_query, extract, split_steps
from dumpbot.db.models import AiUsage
from dumpbot.domain.prefs import Prefs

GEMMA = "google/gemma-4-31B-turbo-TEE"
QWEN = "Qwen/Qwen3-32B-TEE"
QWEN_VL = "Qwen/Qwen3.6-27B-TEE"
NOW = datetime(2026, 10, 9, 12, 5, tzinfo=UTC)
WARSAW = Prefs(tz="Europe/Warsaw")
GOOD = {"intent": "save", "items": [{"kind": "purchase", "title": "Купить наушники"}]}
OTHER = {"intent": "save", "items": [{"kind": "task", "title": "Позвонить маме"}]}
STEPS = {"steps": [{"title": "Собрать документы"}, {"title": "Записаться"}]}
UNAVAILABLE = "No instances available (yet) for chute_id=abc"
NOT_FOUND = '{"detail":"model not found"}'
MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
HEDGE = 0.2  # llm_hedge_after_s in these tests
HANG = 30.0  # a reply that never comes in time (the fake server ends it on teardown)


def make_input(**overrides: Any) -> ExtractInput:
    values: dict[str, Any] = {"text": "купить наушники", "source": "text", "now": NOW, "prefs": WARSAW}
    values.update(overrides)
    return ExtractInput(**values)


async def until(condition: Callable[[], bool], within: float = 3.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.01)


async def usage_rows(sessionmaker) -> list[AiUsage]:
    async with sessionmaker() as s:
        return list((await s.scalars(select(AiUsage).order_by(AiUsage.id))).all())


def dumpbot_tasks() -> list[asyncio.Task[Any]]:
    """Unfinished tasks running the bot's code (the fake server's handlers are not counted)."""
    me = asyncio.current_task()
    marker = f"{os.sep}dumpbot{os.sep}"
    found = []
    for task in asyncio.all_tasks():
        code = getattr(task.get_coro(), "cr_code", None)
        if task is not me and code is not None and marker in code.co_filename:
            found.append(task)
    return found


@pytest.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
async def loop_problems() -> AsyncIterator[list[str]]:
    """What the event loop reports to its exception handler: "Task was destroyed but it is pending!", "Task exception
    was never retrieved", "Unclosed response" (aiohttp tells about those only in debug mode, so it is on)."""
    loop = asyncio.get_running_loop()
    problems: list[str] = []
    handler, debug = loop.get_exception_handler(), loop.get_debug()
    loop.set_exception_handler(lambda _loop, context: problems.append(str(context.get("message"))))
    loop.set_debug(True)
    try:
        yield problems
    finally:
        loop.set_debug(debug)
        loop.set_exception_handler(handler)


async def assert_cleaned_up(http: aiohttp.ClientSession, problems: list[str]) -> None:
    """Nothing of the AI calls keeps running, no HTTP response is left open, the loop reported nothing."""
    assert dumpbot_tasks() == []
    assert not http.connector._acquired  # every response read to the end or its connection closed
    gc.collect()
    await asyncio.sleep(0)
    assert problems == []


@pytest.fixture
def hedged(make_settings):
    return make_settings(llm_hedge_after_s=HEDGE)


# --- the parallel request ----------------------------------------------------------------------------------------


async def test_slow_primary_gets_the_fallback_in_parallel_and_the_first_valid_answer_wins(
    fake, hedged, sessionmaker, http, loop_problems
):
    client = ChutesClient(hedged, sessionmaker, http)
    fake.chat_json(GOOD, model=GEMMA, delay=HANG)
    fake.chat_json(OTHER, model=QWEN, prompt_tokens=900, completion_tokens=100)

    started = time.monotonic()
    out = await extract(client, hedged, make_input())
    elapsed = time.monotonic() - started

    assert (out.model, out.missing_model) == (QWEN, None)
    assert out.extraction.items[0].title == "Позвонить маме"
    assert fake.chat_models == [GEMMA, QWEN]
    assert HEDGE <= elapsed < 2.0  # not the 30 s of the primary (nor its 5 s timeout)
    rows = await usage_rows(sessionmaker)
    assert [(r.model, r.purpose, r.ok, r.error) for r in rows] == [
        (QWEN, "extract", True, None),
        (GEMMA, "extract", False, f"cancelled: {HEDGE_LOST}"),  # recorded before extract() returned
    ]
    lost = next(r for r in rows if r.model == GEMMA)
    assert (lost.input_tokens, lost.output_tokens, lost.cost_usd) == (0, 0, Decimal("0"))  # no answer: not billed
    won = next(r for r in rows if r.model == QWEN)
    assert won.cost_usd == Decimal(f"{900 * 0.104 / 1e6 + 100 * 0.416 / 1e6:.6f}")
    assert client.cooldown_left(GEMMA) == 0  # one slow answer is not an outage (two lost races in a row are)
    await assert_cleaned_up(http, loop_problems)


async def test_primary_answering_while_the_fallback_runs_wins(fake, hedged, sessionmaker, http, loop_problems):
    client = ChutesClient(hedged, sessionmaker, http)
    fake.chat_json(GOOD, model=GEMMA, delay=0.6)
    fake.chat_json(OTHER, model=QWEN, delay=HANG)

    out = await extract(client, hedged, make_input())

    assert out.model == GEMMA
    assert out.extraction.items[0].title == "Купить наушники"
    rows = await usage_rows(sessionmaker)
    assert [(r.model, r.ok, r.error) for r in rows] == [(GEMMA, True, None), (QWEN, False, "cancelled: hedge lost")]
    await assert_cleaned_up(http, loop_problems)


async def test_an_invalid_answer_does_not_win_the_race(fake, hedged, http, loop_problems):
    client = ChutesClient(hedged, http=http)
    fake.chat_json(GOOD, model=GEMMA, delay=0.6)
    fake.chat_ok("Sorry, I cannot help with that.", model=QWEN)  # at 0.2 s: unusable, the race goes on

    out = await extract(client, hedged, make_input())

    assert out.model == GEMMA
    assert fake.chat_models == [GEMMA, QWEN]
    await assert_cleaned_up(http, loop_problems)


async def test_the_primarys_repair_retry_is_cancelled_when_the_fallback_wins(fake, hedged, http, loop_problems):
    client = ChutesClient(hedged, http=http)
    fake.chat_ok("garbage", model=GEMMA, delay=0.3)  # invalid, after the hedge started
    fake.chat_json(GOOD, model=GEMMA, delay=HANG)  # its "fix your JSON" retry hangs
    fake.chat_json(OTHER, model=QWEN, delay=0.4)  # valid at 0.6 s

    out = await extract(client, hedged, make_input())

    assert out.model == QWEN
    assert fake.chat_models == [GEMMA, QWEN, GEMMA]
    assert "invalid" in fake.chat_requests[2]["messages"][-1]["content"]
    await assert_cleaned_up(http, loop_problems)


async def test_both_failing_during_the_hedge_resolve_like_the_ladder(fake, hedged, http, loop_problems):
    client = ChutesClient(hedged, http=http)
    fake.chat_error(503, UNAVAILABLE, model=GEMMA, delay=0.4)
    fake.chat_error(429, "Too Many Requests", headers={"Retry-After": "60"}, model=QWEN, delay=0.3)

    with pytest.raises(AIUnavailable) as ei:
        await extract(client, hedged, make_input())

    assert ei.value.retry_after == 60.0  # the fallback's error, as when the calls run one after another
    assert fake.chat_models == [GEMMA, QWEN]
    assert client.cooldown_left(GEMMA) > 0 and client.cooldown_left(QWEN) > 0
    await assert_cleaned_up(http, loop_problems)


async def test_missing_fallback_during_the_hedge_leaves_the_primary_to_answer(fake, hedged, http, loop_problems):
    client = ChutesClient(hedged, http=http)
    fake.chat_json(GOOD, model=GEMMA, delay=0.6)
    fake.chat_error(404, NOT_FOUND, model=QWEN)

    out = await extract(client, hedged, make_input())

    assert (out.model, out.missing_model) == (GEMMA, None)
    await assert_cleaned_up(http, loop_problems)


async def test_missing_primary_during_the_hedge_is_reported(fake, hedged, http, loop_problems):
    client = ChutesClient(hedged, http=http)
    fake.chat_error(404, NOT_FOUND, model=GEMMA, delay=0.4)
    fake.chat_json(OTHER, model=QWEN, delay=0.4)  # valid at 0.6 s, after the primary's 404

    out = await extract(client, hedged, make_input())

    assert (out.model, out.missing_model) == (QWEN, GEMMA)
    await assert_cleaned_up(http, loop_problems)


async def test_images_are_hedged_with_the_vision_fallback(fake, hedged, http, loop_problems):
    client = ChutesClient(hedged, http=http)
    fake.chat_json(GOOD, model=GEMMA, delay=HANG)
    fake.chat_json({"image": {"description": "Кот"}, "items": [{"kind": "meme", "title": "Кот"}]}, model=QWEN_VL)

    out = await extract(client, hedged, make_input(text="", source="photo", images=[(b"img", "image/jpeg")]))

    assert out.model == QWEN_VL
    assert fake.chat_models == [GEMMA, QWEN_VL]
    assert fake.chat_requests[1]["messages"][1]["content"][1]["type"] == "image_url"
    await assert_cleaned_up(http, loop_problems)


async def test_no_parallel_request_when_turned_off(fake, make_settings):
    settings = make_settings(llm_hedge_after_s=0)
    client = ChutesClient(settings)
    fake.chat_json(GOOD, model=GEMMA, delay=0.5)
    try:
        out = await extract(client, settings, make_input())
    finally:
        await client.close()
    assert out.model == GEMMA
    assert fake.chat_models == [GEMMA]


async def test_no_parallel_request_while_the_fallback_asks_to_retry_later(fake, hedged):
    client = ChutesClient(hedged)
    fake.chat_error(429, "Too Many Requests", headers={"Retry-After": "60"}, model=QWEN)
    fake.chat_json(GOOD, model=GEMMA, delay=0.5)
    try:
        with pytest.raises(AIUnavailable):
            await client.chat(purpose="extract", model=QWEN, messages=MSGS)
        fake.chat_requests.clear()
        out = await extract(client, hedged, make_input())
    finally:
        await client.close()
    assert out.model == GEMMA
    assert fake.chat_models == [GEMMA]
    assert 50 < client.retry_after_left(QWEN) <= 60


async def test_a_paused_fallback_is_still_asked_in_parallel(fake, hedged, http, loop_problems):
    # Message 1: the slow primary wins after the parallel fallback answered 503 (the fallback is now paused).
    client = ChutesClient(hedged, http=http)
    fake.chat_json(GOOD, model=GEMMA, delay=0.6)
    fake.chat_error(503, UNAVAILABLE, model=QWEN)
    assert (await extract(client, hedged, make_input())).model == GEMMA
    assert client.cooldown_left(QWEN) > 0 and client.retry_after_left(QWEN) == 0

    # Message 2: the primary hangs; the fallback is still asked after the hedge, not after the primary's timeout.
    fake.chat_requests.clear()
    fake.chat_json(GOOD, model=GEMMA, delay=HANG)
    fake.chat_json(OTHER, model=QWEN)
    started = time.monotonic()
    out = await extract(client, hedged, make_input())
    elapsed = time.monotonic() - started

    assert out.model == QWEN and fake.chat_models == [GEMMA, QWEN]
    assert HEDGE <= elapsed < 2.0  # the primary's timeout is 5 s
    assert client.cooldown_left(QWEN) == 0  # its answer lifted the pause
    await assert_cleaned_up(http, loop_problems)


async def test_cancelling_the_caller_cancels_both_calls(fake, hedged, sessionmaker, http, loop_problems):
    client = ChutesClient(hedged, sessionmaker, http)
    fake.chat_json(GOOD, model=GEMMA, delay=HANG)
    fake.chat_json(OTHER, model=QWEN, delay=HANG)

    task = asyncio.create_task(extract(client, hedged, make_input()))
    await until(lambda: len(fake.chat_requests) == 2)
    task.cancel()  # e.g. the bot shuts down
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = await usage_rows(sessionmaker)
    assert {(r.model, r.ok, r.error) for r in rows} == {(GEMMA, False, "cancelled"), (QWEN, False, "cancelled")}
    await assert_cleaned_up(http, loop_problems)


async def test_answer_query_is_hedged(fake, hedged, http, loop_problems):
    client = ChutesClient(hedged, http=http)
    fake.chat_ok("Завтра созвон.", model=GEMMA, delay=HANG)
    fake.chat_ok("Завтра созвон с Олегом.", model=QWEN)

    answer = await answer_query(client, hedged, "что завтра?", [{"title": "Созвон"}], NOW, WARSAW)

    assert answer == "Завтра созвон с Олегом."
    assert fake.chat_models == [GEMMA, QWEN]
    await assert_cleaned_up(http, loop_problems)


async def test_split_is_hedged(fake, hedged, http, loop_problems):
    client = ChutesClient(hedged, http=http)
    fake.chat_json(STEPS, model=GEMMA, delay=HANG)
    fake.chat_json(STEPS, model=QWEN)

    out = await split_steps(client, hedged, SplitInput(task={"title": "Виза"}, now=NOW, prefs=WARSAW))

    assert out.model == QWEN and [s.title for s in out.steps] == ["Собрать документы", "Записаться"]
    await assert_cleaned_up(http, loop_problems)


# --- the pause of a failing model -------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [503, 500, 502, 429, 408])
async def test_primary_paused_after_an_error_goes_straight_to_the_fallback(fake, client, settings, status):
    fake.chat_error(status, "busy", model=GEMMA)
    fake.chat_json(GOOD, model=QWEN)
    fake.chat_json(OTHER, model=QWEN)

    assert (await extract(client, settings, make_input())).model == QWEN
    fake.chat_requests.clear()
    out = await extract(client, settings, make_input())

    assert out.model == QWEN and out.missing_model is None
    assert fake.chat_models == [QWEN]  # the primary is not asked at all
    assert settings.llm_cooldown_s - 10 < client.cooldown_left(GEMMA) <= settings.llm_cooldown_s == 600


async def test_a_timeout_pauses_the_primary(fake, make_settings):
    settings = make_settings(llm_timeout_s=0.3)  # the hedge (12 s) never starts
    client = ChutesClient(settings)
    fake.chat_json(GOOD, model=GEMMA, delay=HANG)
    fake.chat_json(GOOD, model=QWEN)
    fake.chat_json(OTHER, model=QWEN)
    try:
        assert (await extract(client, settings, make_input())).model == QWEN
        fake.chat_requests.clear()
        assert (await extract(client, settings, make_input())).model == QWEN
    finally:
        await client.close()
    assert fake.chat_models == [QWEN]
    assert client.cooldown_left(GEMMA) > 0


@pytest.mark.parametrize("failure", ["404", "invalid output", "401"])
async def test_other_failures_do_not_pause_the_primary(fake, client, settings, failure):
    if failure == "404":
        fake.chat_error(404, NOT_FOUND, model=GEMMA)
    elif failure == "401":
        fake.chat_error(401, '{"detail":"Invalid token"}', model=GEMMA)
    else:
        fake.chat_ok("garbage", model=GEMMA)
        fake.chat_ok("garbage again", model=GEMMA)
    fake.chat_json(GOOD, model=QWEN)
    with pytest.raises(AIConfigError) if failure == "401" else contextlib.nullcontext():
        await extract(client, settings, make_input())

    assert client.cooldown_left(GEMMA) == 0
    fake.chat_requests.clear()
    fake.chat_json(OTHER, model=GEMMA)
    assert (await extract(client, settings, make_input())).model == GEMMA
    assert fake.chat_models == [GEMMA]


async def test_connection_errors_do_not_pause_the_model(make_settings):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = ChutesClient(make_settings(chutes_base_url=f"http://127.0.0.1:{port}/v1"))
    try:
        with pytest.raises(AIUnavailable):
            await client.chat(purpose="extract", model=GEMMA, messages=MSGS)
    finally:
        await client.close()
    assert client.cooldown_left(GEMMA) == 0  # no network says nothing about the model


async def test_the_pause_ends(fake, make_settings):
    settings = make_settings(llm_cooldown_s=0.3)
    client = ChutesClient(settings)
    fake.chat_error(503, UNAVAILABLE, model=GEMMA)
    fake.chat_json(GOOD, model=QWEN)
    fake.chat_json(OTHER, model=GEMMA)
    try:
        assert (await extract(client, settings, make_input())).model == QWEN
        await asyncio.sleep(0.35)
        fake.chat_requests.clear()
        out = await extract(client, settings, make_input())
    finally:
        await client.close()
    assert out.model == GEMMA
    assert fake.chat_models == [GEMMA]


async def test_no_pause_when_turned_off(fake, make_settings):
    settings = make_settings(llm_cooldown_s=0)
    client = ChutesClient(settings)
    fake.chat_error(503, UNAVAILABLE, model=GEMMA)
    fake.chat_json(GOOD, model=QWEN)
    fake.chat_json(OTHER, model=GEMMA)
    try:
        await extract(client, settings, make_input())
        fake.chat_requests.clear()
        out = await extract(client, settings, make_input())
    finally:
        await client.close()
    assert out.model == GEMMA
    assert client.cooldown_left(GEMMA) == 0


async def test_both_paused_tries_the_primary_and_an_answer_lifts_its_pause(fake, client, settings):
    fake.chat_error(503, UNAVAILABLE, model=GEMMA)
    fake.chat_error(503, UNAVAILABLE, model=QWEN)
    with pytest.raises(AIUnavailable):
        await extract(client, settings, make_input())
    assert client.cooldown_left(GEMMA) > 0 and client.cooldown_left(QWEN) > 0

    fake.chat_requests.clear()
    fake.chat_json(GOOD, model=GEMMA)
    out = await extract(client, settings, make_input())

    assert out.model == GEMMA and fake.chat_models == [GEMMA]  # nothing better to go to: the usual order
    assert client.cooldown_left(GEMMA) == 0
    assert client.cooldown_left(QWEN) > 0


async def test_paused_primary_and_unusable_fallback_wait_for_the_pause_to_end(fake, client, settings):
    # Retrying earlier would pay the fallback for the same unusable answer again.
    fake.chat_error(503, UNAVAILABLE, model=GEMMA)
    fake.chat_json(GOOD, model=QWEN)
    await extract(client, settings, make_input())

    fake.chat_requests.clear()
    fake.chat_ok("garbage", model=QWEN)
    fake.chat_ok("garbage again", model=QWEN)
    with pytest.raises(AIUnavailable) as ei:
        await extract(client, settings, make_input())

    assert fake.chat_models == [QWEN, QWEN]  # standing in for the primary, it got the primary's repair retry
    assert settings.llm_cooldown_s - 10 < ei.value.retry_after <= settings.llm_cooldown_s


async def test_fallback_standing_in_for_a_paused_primary_gets_the_repair_retry(fake, client, settings):
    fake.chat_error(503, UNAVAILABLE, model=GEMMA)
    fake.chat_json(GOOD, model=QWEN)
    await extract(client, settings, make_input())

    fake.chat_requests.clear()
    fake.chat_json({"intent": "save", "items": [{"kind": "chore", "title": "Позвонить маме"}]}, model=QWEN)
    fake.chat_json(OTHER, model=QWEN)
    out = await extract(client, settings, make_input())

    assert out.model == QWEN and out.extraction.items[0].kind == "task"
    assert fake.chat_models == [QWEN, QWEN]
    assert "invalid" in fake.chat_requests[1]["messages"][-1]["content"]


async def test_missing_fallback_sends_the_message_to_the_paused_primary(fake, client, settings):
    fake.chat_error(503, UNAVAILABLE, model=GEMMA)
    fake.chat_error(404, NOT_FOUND, model=QWEN)
    with pytest.raises(AIUnavailable):
        await extract(client, settings, make_input())
    assert client.cooldown_left(GEMMA) > 0 and client.is_missing(QWEN)

    # The primary works again: it answers at once instead of the message waiting out its 10-minute pause.
    fake.chat_requests.clear()
    fake.chat_json(GOOD, model=GEMMA)
    out = await extract(client, settings, make_input())

    assert fake.chat_models == [GEMMA]  # a fallback known to be missing is not skipped to
    assert (out.model, out.missing_model, out.missing_fallback) == (GEMMA, None, QWEN)
    assert client.cooldown_left(GEMMA) == 0


async def test_fallback_found_missing_while_standing_in_hands_back_to_the_primary(fake, client, settings):
    fake.chat_error(503, UNAVAILABLE, model=GEMMA)
    fake.chat_json(GOOD, model=QWEN)
    await extract(client, settings, make_input())  # the primary is paused, the fallback was fine

    fake.chat_requests.clear()
    fake.chat_error(404, NOT_FOUND, model=QWEN)  # ... and now it is retired
    fake.chat_json(OTHER, model=GEMMA)
    out = await extract(client, settings, make_input())

    assert fake.chat_models == [QWEN, GEMMA]
    assert (out.model, out.missing_fallback) == (GEMMA, QWEN)
    assert out.extraction.items[0].title == "Позвонить маме"


async def test_missing_fallback_and_failing_primary_leave_it_to_the_queue(fake, client, settings):
    fake.chat_error(503, UNAVAILABLE, model=GEMMA)
    fake.chat_json(GOOD, model=QWEN)
    await extract(client, settings, make_input())

    fake.chat_requests.clear()
    fake.chat_error(404, NOT_FOUND, model=QWEN)
    fake.chat_error(503, UNAVAILABLE, model=GEMMA)
    with pytest.raises(AIUnavailable) as ei:
        await extract(client, settings, make_input())

    assert fake.chat_models == [QWEN, GEMMA]
    assert ei.value.retry_after is None  # the primary's own error: the queue's usual backoff, not the 10-min pause


async def test_a_hanging_primary_is_paused_after_two_lost_races(fake, hedged, http, loop_problems):
    client = ChutesClient(hedged, http=http)
    for _ in range(2):
        fake.chat_json(GOOD, model=GEMMA, delay=HANG)
        fake.chat_json(OTHER, model=QWEN)
    fake.chat_json(OTHER, model=QWEN)

    assert (await extract(client, hedged, make_input())).model == QWEN
    assert client.cooldown_left(GEMMA) == 0  # one slow answer is not an outage
    assert (await extract(client, hedged, make_input())).model == QWEN
    assert hedged.llm_cooldown_s - 10 < client.cooldown_left(GEMMA) <= hedged.llm_cooldown_s

    fake.chat_requests.clear()
    started = time.monotonic()
    assert (await extract(client, hedged, make_input())).model == QWEN
    assert time.monotonic() - started < HEDGE  # straight to the fallback, no doomed primary request
    assert fake.chat_models == [QWEN]
    await assert_cleaned_up(http, loop_problems)


async def test_an_answer_of_the_primary_resets_its_lost_races(fake, hedged, http, loop_problems):
    client = ChutesClient(hedged, http=http)
    fake.chat_json(GOOD, model=GEMMA, delay=HANG)
    fake.chat_json(OTHER, model=QWEN)
    fake.chat_json(GOOD, model=GEMMA)  # fast
    fake.chat_json(GOOD, model=GEMMA, delay=HANG)
    fake.chat_json(OTHER, model=QWEN)

    for model in (QWEN, GEMMA, QWEN):
        assert (await extract(client, hedged, make_input())).model == model
    assert client.cooldown_left(GEMMA) == 0  # lost, answered, lost: not two in a row
    await assert_cleaned_up(http, loop_problems)


async def test_paused_primary_sends_images_to_the_vision_fallback(fake, settings, sessionmaker):
    client = ChutesClient(settings, sessionmaker)
    fake.chat_error(429, "Too Many Requests", model=GEMMA)
    fake.chat_json(GOOD, model=QWEN)
    fake.chat_json({"image": {"text": "", "description": "Кот"}, "items": [{"kind": "meme", "title": "Кот"}]})
    try:
        await extract(client, settings, make_input())
        fake.chat_requests.clear()
        out = await extract(client, settings, make_input(text="", source="photo", images=[(b"img", "image/jpeg")]))
    finally:
        await client.close()
    assert out.model == QWEN_VL
    assert fake.chat_models == [QWEN_VL]
    rows = await usage_rows(sessionmaker)
    assert [(r.model, r.purpose, r.ok) for r in rows] == [
        (GEMMA, "extract", False),
        (QWEN, "extract", True),
        (QWEN_VL, "vision", True),
    ]


async def test_answer_query_skips_a_paused_primary(fake, client, settings):
    fake.chat_error(503, UNAVAILABLE, model=GEMMA)
    fake.chat_ok("Первый ответ.", model=QWEN)
    fake.chat_ok("Второй ответ.", model=QWEN)
    assert await answer_query(client, settings, "что завтра?", [{"title": "x"}], NOW, WARSAW) == "Первый ответ."
    fake.chat_requests.clear()
    assert await answer_query(client, settings, "что завтра?", [{"title": "x"}], NOW, WARSAW) == "Второй ответ."
    assert fake.chat_models == [QWEN]


# --- the client: cancelled calls in ai_usage ---------------------------------------------------------------------


async def test_a_call_cancelled_before_the_answer_is_recorded_free(fake, settings, sessionmaker, http, loop_problems):
    client = ChutesClient(settings, sessionmaker, http)
    fake.chat_ok("{}", delay=HANG)

    task = asyncio.create_task(client.chat(purpose="split", model=GEMMA, messages=MSGS))
    await until(lambda: len(fake.chat_requests) == 1)
    task.cancel(HEDGE_LOST)
    with pytest.raises(asyncio.CancelledError):
        await task

    [row] = await usage_rows(sessionmaker)
    assert (row.model, row.purpose, row.ok, row.error) == (GEMMA, "split", False, "cancelled: hedge lost")
    assert (row.input_tokens, row.output_tokens, row.cost_usd) == (0, 0, Decimal("0"))
    assert client.cooldown_left(GEMMA) == 0
    await assert_cleaned_up(http, loop_problems)


async def test_a_call_cancelled_after_the_answer_keeps_its_cost(fake, settings, sessionmaker, http, loop_problems):
    client = ChutesClient(settings, sessionmaker, http)
    fake.set_models(delay=HANG)  # the answer is in, the price list hangs
    fake.chat_ok("{}", prompt_tokens=1000, completion_tokens=200)

    task = asyncio.create_task(client.chat(purpose="extract", model=GEMMA, messages=MSGS))
    await until(lambda: fake.models_hits == 1)
    task.cancel(HEDGE_LOST)
    with pytest.raises(asyncio.CancelledError):
        await task

    [row] = await usage_rows(sessionmaker)
    assert (row.ok, row.error, row.input_tokens, row.output_tokens) == (False, "cancelled: hedge lost", 1000, 200)
    assert row.cost_usd == Decimal(f"{(1000 * 1.0 + 200 * 3.0) / 1e6:.6f}")  # the fallback prices: no list yet
    await assert_cleaned_up(http, loop_problems)
