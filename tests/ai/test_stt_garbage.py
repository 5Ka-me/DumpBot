"""Speech-to-text that is not speech, and the paused local Whisper (docs/features/iteration-4.md §1).

``RoutedSTT`` with ``screen``: a rejected transcript is tried once more by another path (the next backend, else the
same one as WAV in Russian); the backends account a rejected one with ``ok=false`` / ``error="garbage transcript"``.
A local Whisper that cannot be connected to is not tried for 10 minutes.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from dumpbot.ai import garbage, stt
from dumpbot.ai.client import AIConfigError, AIUnavailable
from dumpbot.ai.stt import (
    GARBAGE_ERROR,
    LOCAL_PAUSE_S,
    ChutesWhisperSTT,
    GarbageTranscript,
    LocalWhisperSTT,
    RoutedSTT,
)
from dumpbot.db.models import AiUsage

AUDIO = b"OggS\x00\x02fake opus voice note"
WAV = b"RIFF fake wav"
NOISE = "ааааааааааааааааааа"
SPEECH = "Купить хлеб"


class Backend:
    """A scripted backend that judges like the real ones: each call pops the next result (text or exception)."""

    def __init__(self, name: str, *results: str | BaseException) -> None:
        self.name = name
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []
        self.judged: list[bool] = []

    async def transcribe(
        self,
        audio: bytes,
        *,
        mime_type: str | None = None,
        language: str | None = None,
        judge: Callable[[str], str | None] | None = None,
    ) -> str:
        self.calls.append({"audio": audio, "mime_type": mime_type, "language": language})
        self.judged.append(judge is not None)
        result = self.results.pop(0) if self.results else f"текст от {self.name}"
        if isinstance(result, BaseException):
            raise result
        reason = judge(result) if judge is not None else None
        if reason is not None:
            raise GarbageTranscript(reason, result)
        return result


class Paid:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def wav(monkeypatch: pytest.MonkeyPatch) -> list[tuple[bytes, str | None]]:
    """ffmpeg stand-in: records what it converted, answers ``WAV``."""
    converted: list[tuple[bytes, str | None]] = []

    async def _convert(audio: bytes, mime_type: str | None = None) -> bytes:
        converted.append((audio, mime_type))
        return WAV

    monkeypatch.setattr(stt, "convert_to_wav", _convert)
    return converted


@pytest.fixture
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _missing(audio: bytes, mime_type: str | None = None) -> bytes:
        raise AIConfigError("ffmpeg не найден")

    monkeypatch.setattr(stt, "convert_to_wav", _missing)


def _refused_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


# --- RoutedSTT: the second try ------------------------------------------------------------------------------------


async def test_without_screen_nothing_is_judged() -> None:
    chutes = Backend("chutes", NOISE)
    assert await RoutedSTT(chutes).transcribe(AUDIO, mode="chutes") == NOISE
    assert chutes.judged == [False]


async def test_speech_is_returned_at_once() -> None:
    chutes, local = Backend("chutes", SPEECH), Backend("local")
    text = await RoutedSTT(chutes, local).transcribe(AUDIO, language="ru", mode="auto", screen=True, duration=3)
    assert text == SPEECH
    assert chutes.judged == [True] and local.calls == []


async def test_garbage_from_chutes_is_tried_on_the_local_whisper() -> None:
    chutes, local = Backend("chutes", NOISE), Backend("local", SPEECH)
    routed = RoutedSTT(chutes, local)
    text = await routed.transcribe(AUDIO, mime_type="audio/ogg", language="ru", mode="auto", screen=True)
    assert text == SPEECH
    assert local.calls == [{"audio": AUDIO, "mime_type": "audio/ogg", "language": "ru"}]  # the same request


async def test_garbage_twice_raises_with_both_tries() -> None:
    chutes, local = Backend("chutes", NOISE), Backend("local", "Продолжение следует...")
    with pytest.raises(GarbageTranscript) as exc:
        await RoutedSTT(chutes, local).transcribe(AUDIO, language="ru", mode="auto", screen=True)
    assert exc.value.tries == [
        {"stt": "chutes", "reason": garbage.REPEATED_LETTER, "text": NOISE},
        {"stt": "local", "reason": garbage.HALLUCINATION, "text": "Продолжение следует..."},
    ]
    assert (exc.value.reason, exc.value.text) == (garbage.HALLUCINATION, "Продолжение следует...")
    assert NOISE not in str(exc.value)  # the transcript never goes into the message (it is logged)


async def test_the_only_backend_tries_again_as_wav_in_russian(wav) -> None:
    chutes = Backend("chutes", NOISE, SPEECH)
    text = await RoutedSTT(chutes).transcribe(AUDIO, mime_type="audio/ogg", language=None, mode="chutes", screen=True)
    assert text == SPEECH
    assert wav == [(AUDIO, "audio/ogg")]
    assert chutes.calls[1] == {"audio": WAV, "mime_type": "audio/wav", "language": "ru"}


async def test_chutes_mode_does_not_use_the_local_whisper_for_the_second_try(wav) -> None:
    chutes, local = Backend("chutes", NOISE, SPEECH), Backend("local")
    assert await RoutedSTT(chutes, local).transcribe(AUDIO, mode="chutes", screen=True) == SPEECH
    assert local.calls == [] and len(chutes.calls) == 2


async def test_after_a_failure_the_backend_that_answered_tries_again(wav) -> None:
    chutes, local = Backend("chutes", AIUnavailable("HTTP 503")), Backend("local", NOISE, SPEECH)
    assert await RoutedSTT(chutes, local).transcribe(AUDIO, language="ru", mode="auto", screen=True) == SPEECH
    assert len(chutes.calls) == 1  # failed: not "available" for the second try
    assert local.calls[1]["audio"] == WAV


async def test_without_ffmpeg_the_second_try_asks_for_russian(no_ffmpeg) -> None:
    chutes = Backend("chutes", NOISE, SPEECH)
    assert await RoutedSTT(chutes).transcribe(AUDIO, mime_type="audio/ogg", language=None, screen=True) == SPEECH
    assert chutes.calls[1] == {"audio": AUDIO, "mime_type": "audio/ogg", "language": "ru"}


async def test_without_ffmpeg_and_russian_already_there_is_nothing_else_to_try(no_ffmpeg) -> None:
    chutes = Backend("chutes", NOISE, SPEECH)
    with pytest.raises(GarbageTranscript) as exc:
        await RoutedSTT(chutes).transcribe(AUDIO, language="ru", screen=True)
    assert len(chutes.calls) == 1 and len(exc.value.tries) == 1


async def test_a_backend_that_sends_wav_anyway_is_not_asked_the_same_again(wav) -> None:
    """chutes with STT_CONVERT_WAV converts every audio itself: "as WAV in Russian" with Russian asked for already
    is the very same (paid) request."""
    chutes = Backend("chutes", NOISE, SPEECH)
    chutes.sends_wav = True  # type: ignore[attr-defined]
    with pytest.raises(GarbageTranscript) as exc:
        await RoutedSTT(chutes).transcribe(AUDIO, mime_type="audio/ogg", language="ru", screen=True)
    assert len(chutes.calls) == 1 and len(exc.value.tries) == 1
    assert wav == []

    chutes = Backend("chutes", NOISE, SPEECH)  # another language asked for first: Russian is the other path
    chutes.sends_wav = True  # type: ignore[attr-defined]
    assert await RoutedSTT(chutes).transcribe(AUDIO, mime_type="audio/ogg", language=None, screen=True) == SPEECH
    assert chutes.calls[1] == {"audio": AUDIO, "mime_type": "audio/ogg", "language": "ru"}  # it converts it itself
    assert wav == []


async def test_production_chutes_is_not_paid_twice_for_the_same_request(fake, make_settings, wav) -> None:
    """The real backend with STT_CONVERT_WAV and Russian (the production setup, no local Whisper): one request."""
    fake.stt_ok(NOISE)
    fake.stt_ok(NOISE)
    chutes = ChutesWhisperSTT(make_settings(stt_convert_wav=True))
    try:
        with pytest.raises(GarbageTranscript):
            await RoutedSTT(chutes).transcribe(AUDIO, mime_type="audio/ogg", language="ru", screen=True, duration=4)
    finally:
        await chutes.close()
    assert len(fake.stt_requests) == 1
    assert fake.stt_requests[0]["language"] == "ru"


async def test_a_failed_second_try_keeps_the_first_rejection() -> None:
    chutes, local = Backend("chutes", NOISE), Backend("local", AIUnavailable("timeout"))
    with pytest.raises(GarbageTranscript) as exc:
        await RoutedSTT(chutes, local).transcribe(AUDIO, language="ru", mode="auto", screen=True)
    assert exc.value.tries[1] == {"stt": "local", "error": "AIUnavailable"}
    assert exc.value.text == NOISE


async def test_a_paid_second_try_checks_the_budget() -> None:
    chutes, local = Backend("chutes", SPEECH), Backend("local", NOISE)
    paid = Paid()
    text = await RoutedSTT(chutes, local).transcribe(AUDIO, language="ru", mode="local", before_paid=paid, screen=True)
    assert text == SPEECH and paid.calls == 1


async def test_the_judge_knows_the_duration_and_the_language_of_each_try(wav) -> None:
    long_text = "Купить хлеб " * 11  # 132 characters
    chutes = Backend("chutes", long_text, "Hello there my good old friend from work", SPEECH)
    with pytest.raises(GarbageTranscript) as exc:
        await RoutedSTT(chutes).transcribe(AUDIO, language=None, screen=True, duration=2)
    # 44 characters a second (up to 3 s); then Latin letters when Russian was asked for
    assert [t["reason"] for t in exc.value.tries] == [garbage.TOO_FAST, garbage.NOT_CYRILLIC]


# --- real backends: the rejected call in ai_usage -----------------------------------------------------------------


async def test_rejected_transcripts_are_accounted_as_failed_calls(fake, make_settings, sessionmaker) -> None:
    settings = make_settings(local_stt_url=fake.local_url)
    fake.stt_ok("Продолжение следует...")
    fake.local_ok(NOISE, duration=2.0)
    routed = RoutedSTT(ChutesWhisperSTT(settings, sessionmaker), LocalWhisperSTT(settings, sessionmaker))
    try:
        with pytest.raises(GarbageTranscript) as exc:
            await routed.transcribe(AUDIO, language="ru", mode="auto", screen=True, duration=2)
    finally:
        await routed.close()
    assert [t["stt"] for t in exc.value.tries] == ["chutes", "local"]
    async with sessionmaker() as s:
        rows = (await s.scalars(select(AiUsage).order_by(AiUsage.id))).all()
    assert [(r.model.startswith("local:"), r.ok, r.error) for r in rows] == [
        (False, False, GARBAGE_ERROR),
        (True, False, GARBAGE_ERROR),
    ]
    assert rows[0].cost_usd > 0  # the GPU time was spent all the same
    assert rows[1].audio_seconds == Decimal("2.00")


async def test_speech_after_garbage_is_a_normal_call(fake, make_settings, sessionmaker) -> None:
    settings = make_settings(local_stt_url=fake.local_url)
    fake.stt_ok(NOISE)
    fake.local_ok(SPEECH)
    routed = RoutedSTT(ChutesWhisperSTT(settings, sessionmaker), LocalWhisperSTT(settings, sessionmaker))
    try:
        assert await routed.transcribe(AUDIO, language="ru", mode="auto", screen=True) == SPEECH
    finally:
        await routed.close()
    async with sessionmaker() as s:
        rows = (await s.scalars(select(AiUsage).order_by(AiUsage.id))).all()
    assert [(r.ok, r.error) for r in rows] == [(False, GARBAGE_ERROR), (True, None)]


async def test_chutes_does_not_convert_wav_again(fake, make_settings, monkeypatch) -> None:
    async def _convert(audio: bytes, mime_type: str | None = None) -> bytes:
        raise AssertionError("already WAV")

    monkeypatch.setattr(stt, "convert_to_wav", _convert)
    fake.stt_ok(SPEECH)
    chutes = ChutesWhisperSTT(make_settings(stt_convert_wav=True))
    try:
        assert await chutes.transcribe(WAV, mime_type="audio/wav") == SPEECH
    finally:
        await chutes.close()


# --- the paused local Whisper -------------------------------------------------------------------------------------


async def test_an_unreachable_local_whisper_is_paused_for_ten_minutes(make_settings, sessionmaker, caplog) -> None:
    clock = Clock()
    local = LocalWhisperSTT(make_settings(local_stt_url=_refused_url()), sessionmaker, monotonic=clock)
    chutes = Backend("chutes")
    routed = RoutedSTT(chutes, local)
    caplog.set_level(logging.WARNING, logger="dumpbot.ai.stt")
    try:
        assert await routed.transcribe(AUDIO, mode="local") == "текст от chutes"
        assert local.pause_left() == LOCAL_PAUSE_S == 600
        assert routed.local_paused
        assert routed.order("local") == routed.order("auto") == ["chutes"]

        clock.now += 300  # still paused: chutes at once, the local service is not even tried
        assert await routed.transcribe(AUDIO, mode="local") == "текст от chutes"
        async with sessionmaker() as s:
            local_rows = (await s.scalars(select(AiUsage).where(AiUsage.model.startswith("local:")))).all()
        assert len(local_rows) == 1

        clock.now += 301  # the pause is over: tried again, fails again, paused again
        assert not routed.local_paused and routed.order("local") == ["local", "chutes"]
        await routed.transcribe(AUDIO, mode="local")
        assert routed.local_paused
    finally:
        await local.close()
    warnings = [r for r in caplog.records if "cannot be reached" in r.getMessage()]
    assert len(warnings) == 2  # once per pause


async def test_an_error_status_or_a_slow_answer_is_no_pause(fake, make_settings) -> None:
    fake.local_error(503, {"error": "the model is loading"})
    fake.local_ok("late", delay=1.0)
    local = LocalWhisperSTT(make_settings(local_stt_url=fake.local_url, local_stt_timeout_s=0.2))
    try:
        for _ in range(2):
            with pytest.raises(AIUnavailable):
                await local.transcribe(AUDIO)
            assert local.pause_left() == 0.0
    finally:
        await local.close()


async def test_an_answer_ends_the_pause(fake, make_settings) -> None:
    clock = Clock()
    local = LocalWhisperSTT(make_settings(local_stt_url=fake.local_url), monotonic=clock)
    local._paused_until = clock.now + 100  # as after a refused connection
    assert local.pause_left() == 100
    fake.local_ok(SPEECH)
    try:
        assert await local.transcribe(AUDIO) == SPEECH  # called directly (RoutedSTT would skip it)
    finally:
        await local.close()
    assert local.pause_left() == 0.0


def test_without_a_local_whisper_nothing_is_paused() -> None:
    assert not RoutedSTT(Backend("chutes")).local_paused


async def test_nothing_to_try_is_unavailable() -> None:
    class Paused(Backend):
        def pause_left(self) -> float:
            return 60.0

    with pytest.raises(AIUnavailable):
        await RoutedSTT(Paused("chutes")).transcribe(AUDIO)
