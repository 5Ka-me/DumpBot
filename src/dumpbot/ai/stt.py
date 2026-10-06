"""Speech-to-text: the community Whisper chute "AudioDojo" on chutes.ai (paid) and the local Whisper service (free).

- ``ChutesWhisperSTT``: ``POST {stt_url}`` with JSON ``{"audio_b64", "language"?, "return_timestamps": false}`` ->
  ``{"text"}`` (see docs/research/chutes-stt.md). Billed per started second of GPU time, so cost =
  ``stt_usd_per_second * max(1, ceil(latency_s))``; a timed-out request is counted the same way (the chute keeps
  running after we stop waiting).
- ``LocalWhisperSTT``: ``POST {local_stt_url}/transcribe`` with ``{"audio_b64", "language"}`` -> ``{"text", "language",
  "duration", "processing_ms", "model"}`` (``whisper/app.py``, faster-whisper on the CPU). ``ai_usage`` rows get
  ``model="local:<model>"`` and cost 0.
- ``RoutedSTT``: the order by ``Prefs.stt_mode`` (docs/features/iteration-2.md, B) with a fallback to the next one.
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
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlsplit

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dumpbot.ai.client import AIBadResponse, AIConfigError, AIError, AIUnavailable, error_snippet, http_error
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


class _HttpSTT:
    """Settings, ``ai_usage`` sessionmaker and an aiohttp session (shared, or own and closed by ``close``)."""

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

    async def close(self) -> None:
        if self._own_http and self._http is not None and not self._http.closed:
            await self._http.close()

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or (self._own_http and self._http.closed):
            self._http = aiohttp.ClientSession()
        return self._http


class ChutesWhisperSTT(_HttpSTT):
    """Whisper large-v3-turbo on chutes. Raises ``AIUnavailable`` / ``AIConfigError`` / ``AIBadResponse``."""

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        http: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(settings, sessionmaker, http)
        parts = urlsplit(settings.stt_url)
        self._model = f"{parts.netloc}{parts.path}"[:128] or "whisper"

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
            latency_s = time.monotonic() - started
            # A timed-out request was most likely running on the GPU, which is billed whether or not we waited:
            # count the time waited, so the budget guard sees it (a connect timeout never reached the chute).
            sent = isinstance(e, TimeoutError) and not isinstance(e, aiohttp.ConnectionTimeoutError)
            billed_s = max(1, math.ceil(latency_s)) if sent else 0
            await self._record(
                ok=False,
                latency_s=latency_s,
                billed_s=billed_s,
                cost=self._settings.stt_usd_per_second * billed_s,
                error=f"network: {type(e).__name__}",
            )
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


class LocalWhisperSTT(_HttpSTT):
    """The local faster-whisper service (``whisper/app.py``). Free: ``ai_usage`` rows cost 0.

    Network errors, timeouts and 5xx (503 = the model could not be loaded yet) -> ``AIUnavailable``; 413 (audio over
    the service's limit) and 400 (cannot decode) -> ``AIBadResponse``; 404 (wrong ``LOCAL_STT_URL``) ->
    ``AIModelNotFound``. The audio is sent as it is: the service decodes OGG/Opus, MP3, M4A, WAV itself.
    """

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        http: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(settings, sessionmaker, http)
        self._url = settings.local_stt_url.strip().rstrip("/") + "/transcribe"
        self._model = "whisper"  # replaced by the model name the service reports

    async def transcribe(self, audio: bytes, *, mime_type: str | None = None, language: str | None = None) -> str:
        """Transcript of ``audio`` (stripped; may be empty for silence)."""
        body = {"audio_b64": base64.b64encode(audio).decode("ascii"), "language": language or None}
        started = time.monotonic()
        try:
            async with self._session().post(
                self._url, json=body, timeout=aiohttp.ClientTimeout(total=self._settings.local_stt_timeout_s)
            ) as resp:
                status, headers = resp.status, resp.headers
                raw = await resp.read()
        except (TimeoutError, aiohttp.ClientError) as e:
            await self._record(ok=False, latency_s=time.monotonic() - started, error=f"network: {type(e).__name__}")
            raise AIUnavailable(f"local STT unreachable ({type(e).__name__})") from e
        latency_s = time.monotonic() - started

        if status != 200:
            snippet = error_snippet(raw)
            await self._record(ok=False, latency_s=latency_s, error=f"HTTP {status}: {snippet}")
            log.warning("local STT HTTP %s: %s", status, snippet)
            raise http_error(status, raw, headers)
        try:
            data = json.loads(raw)
            text = data["text"]
            if not isinstance(text, str):
                raise TypeError("'text' is not a string")
            duration = float(data.get("duration") or 0.0)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            await self._record(ok=False, latency_s=latency_s, error="malformed response")
            raise AIBadResponse(f"malformed local STT response ({type(e).__name__})") from e
        if isinstance(data.get("model"), str) and data["model"]:
            self._model = data["model"]

        await self._record(ok=True, latency_s=latency_s, audio_s=duration)
        log.info("local STT ok: %.1f s of audio, %d chars, %d ms", duration, len(text), int(latency_s * 1000))
        return text.strip()

    async def _record(self, *, ok: bool, latency_s: float, audio_s: float = 0.0, error: str | None = None) -> None:
        if self._sessionmaker is None:
            return
        await usage.record_usage(
            self._sessionmaker,
            purpose="stt",
            model=f"local:{self._model}",
            audio_seconds=audio_s,
            cost_usd=0.0,
            ok=ok,
            latency_ms=int(latency_s * 1000),
            error=error,
        )


# --- routing ----------------------------------------------------------------------------------------------------

CHUTES: Final = "chutes"
LOCAL: Final = "local"
# Prefs.stt_mode -> backends in the order they are tried; LOCAL is skipped while LOCAL_STT_URL is empty.
ORDER_BY_MODE: Final[dict[str, tuple[str, ...]]] = {
    "auto": (CHUTES, LOCAL),
    "local": (LOCAL, CHUTES),
    "chutes": (CHUTES,),
}
PAID: Final = frozenset({CHUTES})

BeforePaid = Callable[[], Awaitable[object]]


class RoutedSTT:
    """chutes and the local Whisper in the order of ``Prefs.stt_mode``; when one fails, the next one is tried.

    Any ``AIError`` moves on to the next backend, not only ``AIUnavailable``: a gone chute (404), a rejected key or an
    audio the local service refuses (413 over its length limit) may still work on the other one, and the local
    attempt costs nothing. When all fail, the error worth retrying wins: ``AIUnavailable``, then ``AIConfigError``,
    then the last one. ``before_paid`` is awaited right before a paid backend (the budget check: it may raise to stop).
    """

    def __init__(self, chutes: SpeechToText, local: SpeechToText | None = None) -> None:
        self._backends: dict[str, SpeechToText] = {CHUTES: chutes}
        if local is not None:
            self._backends[LOCAL] = local

    @property
    def local_configured(self) -> bool:
        return LOCAL in self._backends

    def order(self, mode: str) -> list[str]:
        """Backend names to try for ``mode`` (unknown modes act as ``auto``)."""
        names = ORDER_BY_MODE.get(mode, ORDER_BY_MODE["auto"])
        return [name for name in names if name in self._backends]

    async def transcribe(
        self,
        audio: bytes,
        *,
        mime_type: str | None = None,
        language: str | None = None,
        mode: str = "auto",
        before_paid: BeforePaid | None = None,
    ) -> str:
        errors: list[AIError] = []
        for name in self.order(mode):
            if name in PAID and before_paid is not None:
                await before_paid()
            try:
                text = await self._backends[name].transcribe(audio, mime_type=mime_type, language=language)
            except AIError as e:
                log.warning("STT %s failed: %s: %s", name, type(e).__name__, e)
                errors.append(e)
                continue
            if errors:
                log.info("STT: %s stood in after %d failure(s)", name, len(errors))
            return text
        raise _worth_retrying(errors)

    async def close(self) -> None:
        for name, backend in self._backends.items():
            close = getattr(backend, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception as e:
                log.warning("closing STT %s failed: %s", name, type(e).__name__)


def _worth_retrying(errors: list[AIError]) -> AIError:
    for kind in (AIUnavailable, AIConfigError):
        found = next((e for e in errors if isinstance(e, kind)), None)
        if found is not None:
            return found
    return errors[-1]


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
