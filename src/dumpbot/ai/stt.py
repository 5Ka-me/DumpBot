"""Speech-to-text via the community Whisper chute "AudioDojo" on chutes.ai (see docs/research/chutes-stt.md).

``POST {stt_url}`` with JSON ``{"audio_b64", "language"?, "return_timestamps": false}`` -> ``{"text"}``.
Billed per started second of GPU time, so cost = ``stt_usd_per_second * max(1, ceil(latency_s))``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import shutil
import tempfile
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dumpbot.ai.client import AIBadResponse, AIConfigError, AIUnavailable, error_snippet, http_error
from dumpbot.config import Settings
from dumpbot.services import usage

log = logging.getLogger(__name__)

FFMPEG_TIMEOUT_S = 30.0
_EXT_BY_MIME = {
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
}


class SpeechToText(Protocol):
    async def transcribe(self, audio: bytes, *, mime_type: str | None = None, language: str | None = None) -> str: ...


class ChutesWhisperSTT:
    """Whisper large-v3-turbo on chutes. Raises ``AIUnavailable`` / ``AIConfigError`` / ``AIBadResponse``."""

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        http: aiohttp.ClientSession | None = None,
    ) -> None:
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._http = http
        self._own_http = http is None
        parts = urlsplit(settings.stt_url)
        self._model = f"{parts.netloc}{parts.path}"[:128] or "whisper"

    async def close(self) -> None:
        if self._own_http and self._http is not None and not self._http.closed:
            await self._http.close()

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or (self._own_http and self._http.closed):
            self._http = aiohttp.ClientSession()
        return self._http

    async def transcribe(self, audio: bytes, *, mime_type: str | None = None, language: str | None = None) -> str:
        """Transcript of ``audio`` (stripped; may be empty for silence)."""
        if self._settings.stt_convert_wav:
            audio = await convert_to_wav(audio, mime_type)
        body: dict[str, object] = {"audio_b64": base64.b64encode(audio).decode("ascii"), "return_timestamps": False}
        if language:
            body["language"] = language

        started = time.monotonic()
        try:
            async with self._session().post(
                self._settings.stt_url,
                json=body,
                headers={"Authorization": f"Bearer {self._settings.chutes_api_key.get_secret_value()}"},
                timeout=aiohttp.ClientTimeout(total=self._settings.stt_timeout_s),
            ) as resp:
                status, headers = resp.status, resp.headers
                raw = await resp.read()
        except (TimeoutError, aiohttp.ClientError) as e:
            await self._record(ok=False, latency_s=time.monotonic() - started, error=f"network: {type(e).__name__}")
            raise AIUnavailable(f"STT unreachable ({type(e).__name__})") from e
        latency_s = time.monotonic() - started

        if status != 200:
            snippet = error_snippet(raw)
            await self._record(ok=False, latency_s=latency_s, error=f"HTTP {status}: {snippet}")
            log.warning("STT HTTP %s: %s", status, snippet)
            raise http_error(status, raw, headers)

        # From here on the GPU time was spent, so the call is billed even if the body is unusable.
        billed_s = max(1, math.ceil(latency_s))
        cost = self._settings.stt_usd_per_second * billed_s
        try:
            text = json.loads(raw)["text"]
            if not isinstance(text, str):
                raise TypeError("'text' is not a string")
        except (ValueError, KeyError, TypeError) as e:
            await self._record(ok=False, latency_s=latency_s, billed_s=billed_s, cost=cost, error="malformed response")
            # The body may hold transcript text: never put it into the exception (it ends up in logs).
            raise AIBadResponse(f"malformed STT response ({type(e).__name__})") from e

        await self._record(ok=True, latency_s=latency_s, billed_s=billed_s, cost=cost)
        log.info("STT ok: %d chars, billed %ds, cost=$%.4f", len(text), billed_s, cost)
        return text.strip()

    async def _record(
        self, *, ok: bool, latency_s: float, billed_s: int = 0, cost: float = 0.0, error: str | None = None
    ) -> None:
        if self._sessionmaker is None:
            return
        # audio_seconds holds the billed GPU seconds: the audio duration is not known here.
        await usage.record_usage(
            self._sessionmaker,
            purpose="stt",
            model=self._model,
            audio_seconds=float(billed_s),
            cost_usd=cost,
            ok=ok,
            latency_ms=int(latency_s * 1000),
            error=error,
        )


def _ffmpeg_argv(ffmpeg: str, src: str, dst: str) -> list[str]:
    """ffmpeg command: any input -> 16 kHz mono PCM WAV (to a file: WAV headers need a seekable output)."""
    inp = ["-y", "-nostdin", "-loglevel", "error", "-i", src]
    out = ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", dst]
    return [ffmpeg, *inp, *out]


async def convert_to_wav(audio: bytes, mime_type: str | None = None) -> bytes:
    """Convert audio to 16 kHz mono WAV with ffmpeg. ``AIConfigError`` if ffmpeg is not installed."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AIConfigError(
            "STT_CONVERT_WAV включён, но ffmpeg не найден: установите ffmpeg или выключите STT_CONVERT_WAV"
        )
    ext = _EXT_BY_MIME.get((mime_type or "").split(";")[0].strip().lower(), ".ogg")
    with tempfile.TemporaryDirectory(prefix="dumpbot-stt-") as tmp:
        src, dst = Path(tmp) / f"in{ext}", Path(tmp) / "out.wav"
        await asyncio.to_thread(src.write_bytes, audio)
        proc = await asyncio.create_subprocess_exec(
            *_ffmpeg_argv(ffmpeg, str(src), str(dst)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise AIBadResponse("ffmpeg: превышено время конвертации аудио") from None
        if proc.returncode != 0:
            log.warning("ffmpeg failed with code %s: %s", proc.returncode, error_snippet(err or b"", 300))
            raise AIBadResponse("ffmpeg не смог сконвертировать аудио")
        return await asyncio.to_thread(dst.read_bytes)
