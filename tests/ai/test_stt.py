from __future__ import annotations

import base64
import socket
import sys
from decimal import Decimal

import pytest
from sqlalchemy import select

from dumpbot.ai import stt as stt_mod
from dumpbot.ai.client import AIBadResponse, AIConfigError, AIUnavailable
from dumpbot.ai.stt import ChutesWhisperSTT, SpeechToText
from dumpbot.db.models import AiUsage

AUDIO = b"OggS\x00\x02fake opus voice note"


async def test_transcribe_happy_path(fake, settings):
    fake.stt_ok("  Купить молоко после работы \n")
    stt = ChutesWhisperSTT(settings)
    try:
        text = await stt.transcribe(AUDIO, mime_type="audio/ogg", language="ru")
    finally:
        await stt.close()

    assert text == "Купить молоко после работы"
    req = fake.stt_requests[0]
    assert base64.b64decode(req["audio_b64"]) == AUDIO
    assert req["language"] == "ru"
    assert req["return_timestamps"] is False
    assert fake.stt_auth[0] == "Bearer cpk_test"


async def test_transcribe_auto_language_omits_the_field(fake, settings):
    fake.stt_ok("hello")
    stt = ChutesWhisperSTT(settings)
    try:
        assert await stt.transcribe(AUDIO) == "hello"
    finally:
        await stt.close()
    assert "language" not in fake.stt_requests[0]


async def test_stt_usage_and_cost_recorded(fake, make_settings, sessionmaker):
    fake.stt_ok("текст")
    fake.stt_error(503, "No instances available (yet) for chute_id=d6c66e0a")
    stt = ChutesWhisperSTT(make_settings(stt_usd_per_second=0.0005), sessionmaker)
    try:
        await stt.transcribe(AUDIO)
        with pytest.raises(AIUnavailable):
            await stt.transcribe(AUDIO)
    finally:
        await stt.close()

    async with sessionmaker() as s:
        ok, err = (await s.scalars(select(AiUsage).order_by(AiUsage.id))).all()
    assert (ok.purpose, ok.ok, ok.audio_seconds, ok.cost_usd) == ("stt", True, Decimal("1.00"), Decimal("0.000500"))
    assert ok.model.endswith("/stt/whisper")
    assert (err.purpose, err.ok, err.cost_usd) == ("stt", False, Decimal("0"))
    assert "503" in err.error


async def test_stt_cost_rounds_up_started_seconds(fake, make_settings, sessionmaker):
    fake.stt_ok("текст", delay=1.2)
    stt = ChutesWhisperSTT(make_settings(stt_usd_per_second=0.001), sessionmaker)
    try:
        await stt.transcribe(AUDIO)
    finally:
        await stt.close()
    async with sessionmaker() as s:
        row = await s.scalar(select(AiUsage))
    assert row.audio_seconds == Decimal("2.00")
    assert row.cost_usd == Decimal("0.002000")


@pytest.mark.parametrize(
    ("status", "body", "exc"),
    [
        (503, "No instances available (yet) for chute_id=d6c66e0a", AIUnavailable),
        (500, "internal", AIUnavailable),
        (401, '{"detail":"Invalid token"}', AIConfigError),
        (402, "no balance", AIConfigError),
        (404, '{"detail":"No matching cord found!"}', AIConfigError),
        (400, "bad audio", AIBadResponse),
    ],
)
async def test_stt_errors(fake, settings, status, body, exc):
    fake.stt_error(status, body)
    stt = ChutesWhisperSTT(settings)
    try:
        with pytest.raises(exc):
            await stt.transcribe(AUDIO)
    finally:
        await stt.close()


async def test_stt_429_retry_after(fake, settings):
    fake.stt_error(429, "<html>Too Many Requests</html>", headers={"Retry-After": "15"})
    stt = ChutesWhisperSTT(settings)
    try:
        with pytest.raises(AIUnavailable) as ei:
            await stt.transcribe(AUDIO)
    finally:
        await stt.close()
    assert ei.value.retry_after == 15.0


@pytest.mark.parametrize("body", [{"chunks": []}, {"text": None}, "not json"])
async def test_stt_malformed_response(fake, settings, body):
    fake.stt_raw(body)
    stt = ChutesWhisperSTT(settings)
    try:
        with pytest.raises(AIBadResponse):
            await stt.transcribe(AUDIO)
    finally:
        await stt.close()


async def test_stt_timeout_is_unavailable(fake, make_settings):
    fake.stt_ok("late", delay=1.0)
    stt = ChutesWhisperSTT(make_settings(stt_timeout_s=0.2))
    try:
        with pytest.raises(AIUnavailable):
            await stt.transcribe(AUDIO)
    finally:
        await stt.close()


async def test_stt_timeout_is_billed_for_the_time_waited(fake, make_settings, sessionmaker):
    # The chute keeps transcribing (and billing) after we stop waiting: the budget guard must see that spend.
    fake.stt_ok("late", delay=1.0)
    stt = ChutesWhisperSTT(make_settings(stt_timeout_s=0.2, stt_usd_per_second=0.001), sessionmaker)
    try:
        with pytest.raises(AIUnavailable):
            await stt.transcribe(AUDIO)
    finally:
        await stt.close()
    async with sessionmaker() as s:
        row = await s.scalar(select(AiUsage))
    assert (row.ok, row.audio_seconds, row.cost_usd) == (False, Decimal("1.00"), Decimal("0.001000"))
    assert "Timeout" in row.error


async def test_stt_connection_error_is_not_billed(make_settings, sessionmaker):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    stt = ChutesWhisperSTT(make_settings(stt_url=f"http://127.0.0.1:{port}/stt/whisper"), sessionmaker)
    try:
        with pytest.raises(AIUnavailable):
            await stt.transcribe(AUDIO)
    finally:
        await stt.close()
    async with sessionmaker() as s:
        row = await s.scalar(select(AiUsage))
    assert (row.ok, row.cost_usd) == (False, Decimal("0"))


async def test_convert_flag_without_ffmpeg_is_config_error(fake, make_settings, monkeypatch):
    monkeypatch.setattr(stt_mod.shutil, "which", lambda name: None)
    stt = ChutesWhisperSTT(make_settings(stt_convert_wav=True))
    try:
        with pytest.raises(AIConfigError, match="ffmpeg"):
            await stt.transcribe(AUDIO, mime_type="audio/ogg")
    finally:
        await stt.close()
    assert fake.stt_requests == []


async def test_convert_flag_runs_ffmpeg_on_temp_files(fake, make_settings, monkeypatch):
    # A stand-in "ffmpeg": the current Python copies the input file to the output path.
    monkeypatch.setattr(stt_mod.shutil, "which", lambda name: sys.executable)
    seen: list[tuple[str, str]] = []

    def argv(ffmpeg: str, src: str, dst: str) -> list[str]:
        seen.append((src, dst))
        return [ffmpeg, "-c", "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2])", src, dst]

    monkeypatch.setattr(stt_mod, "_ffmpeg_argv", argv)
    fake.stt_ok("ok")
    stt = ChutesWhisperSTT(make_settings(stt_convert_wav=True))
    try:
        assert await stt.transcribe(AUDIO, mime_type="audio/ogg; codecs=opus") == "ok"
    finally:
        await stt.close()

    assert base64.b64decode(fake.stt_requests[0]["audio_b64"]) == AUDIO
    src, dst = seen[0]
    assert src.endswith("in.ogg") and dst.endswith("out.wav")


async def test_ffmpeg_failure_is_bad_response(fake, make_settings, monkeypatch):
    monkeypatch.setattr(stt_mod.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(stt_mod, "_ffmpeg_argv", lambda ffmpeg, src, dst: [ffmpeg, "-c", "import sys; sys.exit(3)"])
    stt = ChutesWhisperSTT(make_settings(stt_convert_wav=True))
    try:
        with pytest.raises(AIBadResponse):
            await stt.transcribe(AUDIO)
    finally:
        await stt.close()
    assert fake.stt_requests == []


def test_ffmpeg_argv_targets_16k_mono_wav():
    argv = stt_mod._ffmpeg_argv("ffmpeg", "in.ogg", "out.wav")
    assert argv[0] == "ffmpeg" and argv[-1] == "out.wav"
    assert argv[argv.index("-ar") + 1] == "16000" and argv[argv.index("-ac") + 1] == "1"


def test_chutes_whisper_satisfies_the_protocol(settings):
    stt: SpeechToText = ChutesWhisperSTT(settings)
    assert callable(stt.transcribe)
