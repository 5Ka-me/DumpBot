"""End-to-end: the fast switch to the fallback model (docs/features/iteration-3.md §1). A slow primary gets the
fallback asked in parallel (``LLM_HEDGE_AFTER_S`` is 0.2 s here) and the card comes from the first valid answer; a
primary that answered 503 is paused, so the next message goes straight to the fallback.
Harness: conftest.py (``e2e``); reference time Tue 2026-10-06 10:12 Europe/Warsaw.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from dumpbot.db.models import AiUsage

PRIMARY = "google/gemma-4-31B-turbo-TEE"
FALLBACK = "Qwen/Qwen3-32B-TEE"


@pytest.fixture
def llm_settings() -> dict[str, Any]:
    return {"llm_hedge_after_s": 0.2}


def task(title: str, when: dict[str, Any]) -> dict[str, Any]:
    return {"intent": "save", "items": [{"kind": "task", "title": title, "when": when}]}


async def test_slow_primary_card_comes_from_the_fallback_asked_in_parallel(e2e: Any) -> None:
    text = "Позвонить в банк завтра в 11"
    e2e.ai.script(text, task("Позвонить в банк", {"text": "завтра в 11", "day": "+1", "time": "11:00"}))
    e2e.ai.slow[PRIMARY] = 30.0  # never answers in time

    message_id = e2e.tg.push_message(text)
    card = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "👌")
    await e2e.idle()

    assert card.text.startswith("📌 <b>Позвонить в банк</b>")
    assert card.text.splitlines()[1] == "🔔 завтра 11:00"
    assert [r.model for r in e2e.ai.extraction_requests()] == [PRIMARY, FALLBACK]
    assert (await e2e.item("Позвонить в банк")).ai_model == FALLBACK
    # both calls are accounted; the cancelled one had no answer yet, so it costs nothing
    usage = await e2e.all(AiUsage)
    assert [(u.model, u.purpose, u.ok, u.error) for u in usage] == [
        (FALLBACK, "extract", True, None),
        (PRIMARY, "extract", False, "cancelled: hedge lost"),
    ]
    assert usage[1].cost_usd == Decimal("0")
    assert e2e.ctx.ai.cooldown_left(PRIMARY) == 0  # slow is not broken


async def test_primary_answering_503_is_skipped_for_the_next_message(e2e: Any) -> None:
    first, second = "Купить молоко", "Купить хлеб"
    e2e.ai.script(first, {"intent": "save", "items": [{"kind": "purchase", "title": "Молоко"}]})
    e2e.ai.script(second, {"intent": "save", "items": [{"kind": "purchase", "title": "Хлеб"}]})
    e2e.ai.down.add(PRIMARY)

    first_id = e2e.tg.push_message(first)
    await e2e.reaction(first_id, "👌")
    await e2e.idle()
    assert [r.model for r in e2e.ai.extraction_requests()] == [PRIMARY, FALLBACK]

    e2e.ai.down.clear()  # the primary is back, but its pause (10 min) is not over: not even asked
    second_id = e2e.tg.push_message(second)
    await e2e.reaction(second_id, "👌")
    await e2e.idle()

    assert [r.model for r in e2e.ai.extraction_requests()] == [PRIMARY, FALLBACK, FALLBACK]
    assert (await e2e.item("Хлеб")).ai_model == FALLBACK
    usage = await e2e.all(AiUsage)
    assert [(u.model, u.ok) for u in usage] == [(PRIMARY, False), (FALLBACK, True), (FALLBACK, True)]
    assert 590 < e2e.ctx.ai.cooldown_left(PRIMARY) <= 600
