"""The local Whisper client (``LocalWhisperSTT``) against a fake service, and ``RoutedSTT`` ordering / fallback."""

from __future__ import annotations

import base64
import socket
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from dumpbot.ai.client import AIBadResponse, AIConfigError, AIModelNotFound, AIUnavailable
from dumpbot.ai.stt import ORDER_BY_MODE, ChutesWhisperSTT, LocalWhisperSTT, RoutedSTT, SpeechToText
from dumpbot.db.models import AiUsage
from dumpbot.domain.prefs import STT_MODES

AUDIO = b"OggS\x00\x02fake opus voice note"


@pytest.fixture
def local_settings(make_settings, fake):
    return make_settings(local_stt_url=fake.local_url, local_stt_timeout_s=5.0)


async def _transcribe(stt: LocalWhisperSTT, **kwargs: Any) -> str:
    try:
        return await stt.transcribe(AUDIO, **kwargs)
    finally:
        await stt.close()


# --- LocalWhisperSTT --------------------------------------------------------------------------------------------


async def test_local_happy_path_is_free_and_accounted(fake, local_settings, sessionmaker):
    fake.local_ok("  Купить хлеб \n", duration=4.2, model="small")

    text = await _transcribe(LocalWhisperSTT(local_settings, sessionmaker), mime_type="audio/ogg", language="ru")

    assert text == "Купить хлеб"
    [req] = fake.local_requests
    assert base64.b64decode(req["audio_b64"]) == AUDIO
    assert req["language"] == "ru"
    assert fake.local_auth == [None]  # the chutes key never goes to the local service
    async with sessionmaker() as s:
        row = await s.scalar(select(AiUsage))
    assert (row.purpose, row.model, row.ok, row.cost_usd) == ("stt", "local:small", True, Decimal("0"))
    assert row.audio_seconds == Decimal("4.20")


async def test_local_auto_language_is_sent_as_null(fake, local_settings):
    fake.local_ok("hello")
    assert await _transcribe(LocalWhisperSTT(local_settings), language=None) == "hello"
    assert fake.local_requests[0]["language"] is None


async def test_local_url_with_a_trailing_slash(fake, make_settings):
    fake.local_ok("ok")
    stt = LocalWhisperSTT(make_settings(local_stt_url=f" {fake.local_url}/ "))
    assert await _transcribe(stt) == "ok"


@pytest.mark.parametrize(
    ("status", "body", "exc"),
    [
        (503, {"error": "the model is not available yet, try again later"}, AIUnavailable),
        (500, "Internal Server Error", AIUnavailable),
        (413, {"error": "audio is 130 s long, the limit is 120 s"}, AIBadResponse),
        (400, {"error": "cannot decode the audio"}, AIBadResponse),
        (404, "404: Not Found", AIModelNotFound),  # LOCAL_STT_URL points at something else
    ],
)
async def test_local_errors(fake, local_settings, sessionmaker, status, body, exc):
    fake.local_error(status, body)
    with pytest.raises(exc):
        await _transcribe(LocalWhisperSTT(local_settings, sessionmaker))
    async with sessionmaker() as s:
        row = await s.scalar(select(AiUsage))
    assert (row.ok, row.cost_usd, row.model) == (False, Decimal("0"), "local:whisper")
    assert str(status) in row.error


@pytest.mark.parametrize("body", [{"language": "ru"}, {"text": 5}, "not json", ["text"]])
async def test_local_malformed_response(fake, local_settings, body):
    fake.local_raw(body)
    with pytest.raises(AIBadResponse):
        await _transcribe(LocalWhisperSTT(local_settings))


async def test_local_timeout_is_unavailable(fake, make_settings):
    fake.local_ok("late", delay=1.0)
    with pytest.raises(AIUnavailable):
        await _transcribe(LocalWhisperSTT(make_settings(local_stt_url=fake.local_url, local_stt_timeout_s=0.2)))


async def test_local_service_down_is_unavailable(make_settings, sessionmaker):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    stt = LocalWhisperSTT(make_settings(local_stt_url=f"http://127.0.0.1:{port}"), sessionmaker)
    with pytest.raises(AIUnavailable):
        await _transcribe(stt)
    async with sessionmaker() as s:
        row = await s.scalar(select(AiUsage))
    assert (row.ok, row.cost_usd) == (False, Decimal("0"))
    assert row.error.startswith("network:")


async def test_local_model_name_comes_from_the_service(fake, local_settings, sessionmaker):
    fake.local_error(503)
    fake.local_ok("раз", model="base")
    fake.local_error(503)
    stt = LocalWhisperSTT(local_settings, sessionmaker)
    try:
        with pytest.raises(AIUnavailable):
            await stt.transcribe(AUDIO)
        await stt.transcribe(AUDIO)
        with pytest.raises(AIUnavailable):
            await stt.transcribe(AUDIO)
    finally:
        await stt.close()
    async with sessionmaker() as s:
        models = (await s.scalars(select(AiUsage.model).order_by(AiUsage.id))).all()
    assert models == ["local:whisper", "local:base", "local:base"]


def test_local_whisper_satisfies_the_protocol(local_settings):
    stt: SpeechToText = LocalWhisperSTT(local_settings)
    assert callable(stt.transcribe)


# --- RoutedSTT ----------------------------------------------------------------------------------------------------


class Backend:
    """A scripted STT backend: each call pops the next result (text or exception)."""

    def __init__(self, name: str, *results: str | BaseException) -> None:
        self.name = name
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def transcribe(self, audio: bytes, *, mime_type: str | None = None, language: str | None = None) -> str:
        self.calls.append({"audio": audio, "mime_type": mime_type, "language": language})
        result = self.results.pop(0) if self.results else f"text from {self.name}"
        if isinstance(result, BaseException):
            raise result
        return result

    async def close(self) -> None:
        self.closed = True


class Paid:
    """``before_paid`` stand-in: counts the calls, optionally raises (an exhausted budget)."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls = 0
        self.error = error

    async def __call__(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


class BudgetExhausted(Exception):
    pass


def test_every_prefs_mode_has_an_order():
    assert set(ORDER_BY_MODE) == set(STT_MODES)


@pytest.mark.parametrize(
    ("mode", "order"),
    [("auto", ["chutes", "local"]), ("local", ["local", "chutes"]), ("chutes", ["chutes"]), ("bogus", None)],
)
def test_order_by_mode(mode, order):
    routed = RoutedSTT(Backend("chutes"), Backend("local"))
    assert routed.local_configured
    assert routed.order(mode) == (order or ["chutes", "local"])  # an unknown mode acts as auto


@pytest.mark.parametrize("mode", ["auto", "local", "chutes"])
def test_without_local_every_mode_is_chutes(mode):
    routed = RoutedSTT(Backend("chutes"))
    assert not routed.local_configured
    assert routed.order(mode) == ["chutes"]


@pytest.mark.parametrize(("mode", "first"), [("auto", "chutes"), ("local", "local"), ("chutes", "chutes")])
async def test_first_backend_answers(mode, first):
    chutes, local = Backend("chutes"), Backend("local")
    paid = Paid()
    text = await RoutedSTT(chutes, local).transcribe(
        AUDIO, mime_type="audio/ogg", language="en", mode=mode, before_paid=paid
    )
    assert text == f"text from {first}"
    used, unused = (chutes, local) if first == "chutes" else (local, chutes)
    assert used.calls == [{"audio": AUDIO, "mime_type": "audio/ogg", "language": "en"}] and unused.calls == []
    assert paid.calls == int(first == "chutes")  # the budget is checked only before the paid backend


async def test_auto_falls_back_to_local_on_unavailable():
    chutes, local = Backend("chutes", AIUnavailable("HTTP 503")), Backend("local")
    assert await RoutedSTT(chutes, local).transcribe(AUDIO, mode="auto") == "text from local"
    assert len(chutes.calls) == len(local.calls) == 1


async def test_local_falls_back_to_chutes_and_checks_the_budget_first():
    chutes, local = Backend("chutes"), Backend("local", AIBadResponse("HTTP 413: audio is 130 s long"))
    paid = Paid()
    text = await RoutedSTT(chutes, local).transcribe(AUDIO, mode="local", before_paid=paid)
    assert text == "text from chutes"
    assert paid.calls == 1


async def test_chutes_mode_never_falls_back():
    chutes, local = Backend("chutes", AIUnavailable("HTTP 503")), Backend("local")
    with pytest.raises(AIUnavailable):
        await RoutedSTT(chutes, local).transcribe(AUDIO, mode="chutes")
    assert local.calls == []


async def test_budget_stop_before_the_paid_backend_propagates():
    chutes, local = Backend("chutes"), Backend("local", AIUnavailable("down"))
    with pytest.raises(BudgetExhausted):
        await RoutedSTT(chutes, local).transcribe(AUDIO, mode="local", before_paid=Paid(BudgetExhausted()))
    assert len(local.calls) == 1 and chutes.calls == []


@pytest.mark.parametrize(
    ("chutes_error", "local_error", "raised"),
    [
        (AIUnavailable("503"), AIBadResponse("413"), AIUnavailable),  # retry later: chutes may come back
        (AIBadResponse("malformed"), AIUnavailable("timeout"), AIUnavailable),
        (AIConfigError("HTTP 402"), AIBadResponse("400"), AIConfigError),  # the owner has to act
        (AIBadResponse("malformed"), AIBadResponse("400"), AIBadResponse),
    ],
)
async def test_when_all_fail_the_error_worth_retrying_wins(chutes_error, local_error, raised):
    routed = RoutedSTT(Backend("chutes", chutes_error), Backend("local", local_error))
    with pytest.raises(raised) as exc:
        await routed.transcribe(AUDIO, mode="auto")
    assert exc.value in (chutes_error, local_error)


async def test_close_closes_both_backends():
    chutes, local = Backend("chutes"), Backend("local")
    await RoutedSTT(chutes, local).close()
    assert chutes.closed and local.closed


async def test_routed_over_real_clients(fake, make_settings, sessionmaker):
    """chutes answers 503, the local service stands in; both calls are in ai_usage (only chutes could cost)."""
    settings = make_settings(local_stt_url=fake.local_url)
    fake.stt_error(503, "No instances available (yet)")
    fake.local_ok("Купить хлеб", duration=2.0, model="small")
    routed = RoutedSTT(ChutesWhisperSTT(settings, sessionmaker), LocalWhisperSTT(settings, sessionmaker))
    try:
        assert await routed.transcribe(AUDIO, language="ru", mode="auto") == "Купить хлеб"
    finally:
        await routed.close()
    async with sessionmaker() as s:
        rows = (await s.scalars(select(AiUsage).order_by(AiUsage.id))).all()
    assert [(r.purpose, r.model.startswith("local:"), r.ok, r.cost_usd) for r in rows] == [
        ("stt", False, False, Decimal("0")),
        ("stt", True, True, Decimal("0")),
    ]
