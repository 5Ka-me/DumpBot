"""Speech-to-text: the community Whisper chute "AudioDojo" on chutes.ai (paid) and the local Whisper service (free).

- ``ChutesWhisperSTT``: ``POST {stt_url}`` with JSON ``{"audio_b64", "language"?, "return_timestamps": false}`` ->
  ``{"text"}`` (see docs/research/chutes-stt.md). Billed per started second of GPU time, so cost =
  ``stt_usd_per_second * max(1, ceil(latency_s))``; a timed-out request is counted the same way (the chute keeps
  running after we stop waiting).
- ``LocalWhisperSTT``: ``POST {local_stt_url}/transcribe`` with ``{"audio_b64", "language"}`` -> ``{"text", "language",
  "duration", "processing_ms", "model"}`` (``whisper/app.py``, faster-whisper on the CPU). ``ai_usage`` rows get
  ``model="local:<model>"`` and cost 0.
- ``RoutedSTT``: the order by ``Prefs.stt_mode`` (docs/features/iteration-2.md, B) with a fallback to the next one.

Iteration 4 (docs/features/iteration-4.md §1): with ``screen`` a transcript that is not speech (``ai.garbage``) is
rejected by the backend that made it — its ``ai_usage`` row gets ``ok=false``, ``error="garbage transcript"`` — and
tried once more by another path: the next backend of the order, else the same one with the audio converted to WAV and
``language=ru``; garbage again -> ``GarbageTranscript`` with both tries. A local Whisper that cannot be connected to
(DNS, refused, connect timeout) is not tried for ``LOCAL_PAUSE_S`` (10 min): chutes stands in at once, one warning in
the log per pause, and /settings marks the local mode «(недоступен)».
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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlsplit

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dumpbot.ai.client import AIBadResponse, AIConfigError, AIError, AIUnavailable, error_snippet, http_error
from dumpbot.ai.garbage import garbage_reason
from dumpbot.config import Settings
from dumpbot.services import usage

log = logging.getLogger(__name__)

FFMPEG_TIMEOUT_S = 30.0
GARBAGE_ERROR: Final = "garbage transcript"  # ai_usage.error of a call whose transcript was not speech
LOCAL_PAUSE_S: Final = 600.0  # an unreachable local Whisper is not tried for this long
LOCAL_CONNECT_TIMEOUT_S: Final = 10.0  # a host that is down (a sleeping PC behind a VPN) drops the SYN: give up soon
WAV_MIME: Final = "audio/wav"
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


# Why a transcript is not speech (``ai.garbage.garbage_reason`` with the duration and language bound), or None.
Judge = Callable[[str], str | None]


class GarbageTranscript(AIError):
    """Speech-to-text answered, but not with speech (a Whisper hallucination on noise, one letter in a loop…).

    ``tries``: every attempt of this audio, in order — ``{"stt": backend, "reason": why}`` for a rejected transcript
    (with its ``text``), ``{"stt": backend, "error": exception type}`` for a second try that failed; ``text`` is the
    last rejected transcript. The transcripts are kept for analysis but never put into the message (it is logged).
    """

    def __init__(self, reason: str, text: str, *, stt: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.text = text
        self.tries: list[dict[str, str]] = [{"stt": stt or "", "reason": reason, "text": text}]


class SpeechToText(Protocol):
    """A speech-to-text backend. ``judge`` (passed only by ``RoutedSTT`` with ``screen``) rejects a transcript that is
    not speech: the backend accounts the call with ``ok=false`` and raises ``GarbageTranscript``."""

    async def transcribe(
        self, audio: bytes, *, mime_type: str | None = None, language: str | None = None, judge: Judge | None = None
    ) -> str: ...


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

    async def transcribe(
        self, audio: bytes, *, mime_type: str | None = None, language: str | None = None, judge: Judge | None = None
    ) -> str:
        """Transcript of ``audio`` (stripped; may be empty for silence). ``judge`` rejects one that is not speech:
        ``GarbageTranscript``, the call is accounted (and billed) with ``ok=false``."""
        if self._settings.stt_convert_wav and not _is_wav(mime_type):
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

        text = text.strip()
        reason = judge(text) if judge is not None else None
        if reason is not None:
            await self._record(ok=False, latency_s=latency_s, billed_s=billed_s, cost=cost, error=GARBAGE_ERROR)
            log.info("STT: %d chars rejected (%s), billed %ds, cost=$%.4f", len(text), reason, billed_s, cost)
            raise GarbageTranscript(reason, text)
        await self._record(ok=True, latency_s=latency_s, billed_s=billed_s, cost=cost)
        log.info("STT ok: %d chars, billed %ds, cost=$%.4f", len(text), billed_s, cost)
        return text

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

    Network errors, timeouts and 5xx (503 = the model could not be loaded yet) -> ``AIUnavailable``; any other status
    -> ``AIBadResponse``: 413 (audio over the service's limit), 400 (cannot decode), 404 (``LOCAL_STT_URL`` points at
    something else). Never ``AIConfigError``: that one is about chutes (the owner's key, balance and model IDs).
    ``LOCAL_STT_URL`` is the service's base URL; one given with the ``/transcribe`` path works too.
    The audio is sent as it is: the service decodes OGG/Opus, MP3, M4A, WAV itself.

    A service that cannot be connected to at all (DNS, refused, no answer to the connect within
    ``LOCAL_CONNECT_TIMEOUT_S``: the PC is off) is paused for ``pause_s`` (in process memory): ``pause_left()`` > 0,
    ``RoutedSTT`` does not try it meanwhile, and the warning is logged once per pause. A slow answer or an error
    status is no pause: the service is there.
    """

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        http: aiohttp.ClientSession | None = None,
        *,
        pause_s: float = LOCAL_PAUSE_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(settings, sessionmaker, http)
        base = settings.local_stt_url.strip().rstrip("/").removesuffix("/transcribe").rstrip("/")
        self._url = f"{base}/transcribe"
        self._model = "whisper"  # replaced by the model name the service reports
        self._pause_s = pause_s
        self._monotonic = monotonic
        self._paused_until: float | None = None

    def pause_left(self) -> float:
        """Seconds the service is still not tried after it could not be connected to (0.0: no pause)."""
        if self._paused_until is None:
            return 0.0
        left = self._paused_until - self._monotonic()
        if left <= 0:
            self._paused_until = None
            return 0.0
        return left

    async def transcribe(
        self, audio: bytes, *, mime_type: str | None = None, language: str | None = None, judge: Judge | None = None
    ) -> str:
        """Transcript of ``audio`` (stripped; may be empty for silence). ``judge`` rejects one that is not speech:
        ``GarbageTranscript``, its ``ai_usage`` row has ``ok=false``."""
        body = {"audio_b64": base64.b64encode(audio).decode("ascii"), "language": language or None}
        timeout = aiohttp.ClientTimeout(total=self._settings.local_stt_timeout_s, sock_connect=LOCAL_CONNECT_TIMEOUT_S)
        started = time.monotonic()
        try:
            async with self._session().post(self._url, json=body, timeout=timeout) as resp:
                status, headers = resp.status, resp.headers
                raw = await resp.read()
        except (TimeoutError, aiohttp.ClientError) as e:
            await self._record(ok=False, latency_s=time.monotonic() - started, error=f"network: {type(e).__name__}")
            if isinstance(e, aiohttp.ClientConnectorError | aiohttp.ConnectionTimeoutError):
                self._pause(e)
            raise AIUnavailable(f"local STT unreachable ({type(e).__name__})") from e
        latency_s = time.monotonic() - started
        self._paused_until = None  # it answered

        if status != 200:
            snippet = error_snippet(raw)
            await self._record(ok=False, latency_s=latency_s, error=f"HTTP {status}: {snippet}")
            hint = " (check LOCAL_STT_URL: it should be the service's base URL, e.g. http://whisper:9000)"
            log.warning("local STT HTTP %s: %s%s", status, snippet, hint if status == 404 else "")
            raise _local_error(status, raw, headers)
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

        text = text.strip()
        reason = judge(text) if judge is not None else None
        if reason is not None:
            await self._record(ok=False, latency_s=latency_s, audio_s=duration, error=GARBAGE_ERROR)
            log.info("local STT: %.1f s of audio, %d chars rejected (%s)", duration, len(text), reason)
            raise GarbageTranscript(reason, text)
        await self._record(ok=True, latency_s=latency_s, audio_s=duration)
        log.info("local STT ok: %.1f s of audio, %d chars, %d ms", duration, len(text), int(latency_s * 1000))
        return text

    def _pause(self, error: BaseException) -> None:
        """Not tried for ``pause_s`` from now on; the warning only when a pause starts."""
        if self._pause_s <= 0:
            return
        starts = not self.pause_left()
        self._paused_until = self._monotonic() + self._pause_s
        if starts:
            log.warning(
                "local STT cannot be reached (%s): not tried for %.0f min, chutes stands in",
                type(error).__name__,
                self._pause_s / 60,
            )

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


def _local_error(status: int, raw: bytes, headers: Mapping[str, str]) -> AIError:
    """A non-200 answer of the local service: temporary (``AIUnavailable``) or not usable for this audio
    (``AIBadResponse``), never a chutes configuration problem (``AIConfigError`` → "check the chutes key")."""
    error = http_error(status, raw, headers)
    return error if isinstance(error, AIUnavailable) else AIBadResponse(f"local STT {error}")


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
    A backend on pause (``pause_left()``: the local Whisper could not be connected to) is left out of the order.

    ``screen`` (docs/features/iteration-4.md §1): a transcript that is not speech (``ai.garbage``, by ``duration`` and
    the language asked for) is rejected by its backend and tried once more by another path — the next backend of the
    order, else the same one with the audio converted to WAV and ``language="ru"``. A usable second transcript is
    returned; garbage again (or a second try that fails) -> ``GarbageTranscript`` with both tries.
    """

    def __init__(self, chutes: SpeechToText, local: SpeechToText | None = None) -> None:
        self._backends: dict[str, SpeechToText] = {CHUTES: chutes}
        if local is not None:
            self._backends[LOCAL] = local

    @property
    def local_configured(self) -> bool:
        return LOCAL in self._backends

    @property
    def local_paused(self) -> bool:
        """The local Whisper is configured but paused: it could not be connected to (/settings: «(недоступен)»)."""
        local = self._backends.get(LOCAL)
        return local is not None and _pause_left(local) > 0

    def order(self, mode: str) -> list[str]:
        """Backend names to try for ``mode`` (unknown modes act as ``auto``), without the paused ones."""
        names = ORDER_BY_MODE.get(mode, ORDER_BY_MODE["auto"])
        return [name for name in names if name in self._backends and _pause_left(self._backends[name]) <= 0]

    async def transcribe(
        self,
        audio: bytes,
        *,
        mime_type: str | None = None,
        language: str | None = None,
        mode: str = "auto",
        before_paid: BeforePaid | None = None,
        screen: bool = False,
        duration: float | None = None,
    ) -> str:
        names = self.order(mode)
        errors: list[AIError] = []
        for n, name in enumerate(names):
            call = _Call(audio, mime_type, language)
            try:
                text = await self._call(name, call, before_paid, screen=screen, duration=duration)
            except GarbageTranscript as garbage:
                garbage.tries[-1]["stt"] = name
                return await self._second_try(garbage, names[n + 1 :], call, before_paid, duration)
            except AIError as e:
                log.warning("STT %s failed: %s: %s", name, type(e).__name__, e)
                errors.append(e)
                continue
            if errors:
                log.info("STT: %s stood in after %d failure(s)", name, len(errors))
            return text
        if not errors:  # every backend paused (chutes never is, so only with a test double)
            raise AIUnavailable("no speech-to-text backend to try")
        raise _worth_retrying(errors)

    async def _call(
        self, name: str, call: _Call, before_paid: BeforePaid | None, *, screen: bool, duration: float | None
    ) -> str:
        if name in PAID and before_paid is not None:
            await before_paid()
        backend = self._backends[name]
        if not screen:
            return await backend.transcribe(call.audio, mime_type=call.mime_type, language=call.language)
        language = call.language

        def judge(text: str) -> str | None:
            return garbage_reason(text, duration=duration, language=language)

        return await backend.transcribe(call.audio, mime_type=call.mime_type, language=language, judge=judge)

    async def _second_try(
        self,
        first: GarbageTranscript,
        untried: list[str],
        call: _Call,
        before_paid: BeforePaid | None,
        duration: float | None,
    ) -> str:
        """One more transcript of an audio whose first one was not speech: the next backend of the order
        (``untried``), else the same one with the audio as WAV and ``language="ru"``. Garbage again, or an error ->
        ``first`` with this try added."""
        tried = first.tries[0]["stt"]
        if untried:
            name, again = untried[0], call
        else:
            name, again = tried, await _as_wav_ru(call)
            if again == call:  # the very same request again would give the same answer
                log.info("STT %s: transcript rejected (%s), nothing else to try", tried, first.reason)
                raise first
        log.info("STT %s: transcript rejected (%s), one more try: %s", tried, first.reason, name)
        try:
            text = await self._call(name, again, before_paid, screen=True, duration=duration)
        except GarbageTranscript as garbage:
            first.tries.append({"stt": name, "reason": garbage.reason, "text": garbage.text})
            first.reason, first.text = garbage.reason, garbage.text
            log.info("STT %s: transcript rejected again (%s)", name, garbage.reason)
            raise first from None
        except AIError as e:
            first.tries.append({"stt": name, "error": type(e).__name__})
            log.warning("STT %s: the second try failed: %s: %s", name, type(e).__name__, e)
            raise first from None
        log.info("STT %s: the second try is usable", name)
        return text

    async def close(self) -> None:
        for name, backend in self._backends.items():
            close = getattr(backend, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception as e:
                log.warning("closing STT %s failed: %s", name, type(e).__name__)


@dataclass(frozen=True)
class _Call:
    """What is sent to a backend."""

    audio: bytes
    mime_type: str | None
    language: str | None


async def _as_wav_ru(call: _Call) -> _Call:
    """The second try with the same backend: the audio as 16 kHz WAV and Russian asked for; without ffmpeg (or when
    it fails) the audio as it is."""
    try:
        return _Call(await convert_to_wav(call.audio, call.mime_type), WAV_MIME, "ru")
    except AIError as e:
        log.warning("STT: no WAV for the second try (%s)", type(e).__name__)
        return _Call(call.audio, call.mime_type, "ru")


def _pause_left(backend: SpeechToText) -> float:
    """``backend.pause_left()`` when it has one (the local Whisper), else 0.0."""
    pause_left = getattr(backend, "pause_left", None)
    return float(pause_left()) if callable(pause_left) else 0.0


def _is_wav(mime_type: str | None) -> bool:
    return (mime_type or "").split(";")[0].strip().lower() in ("audio/wav", "audio/x-wav", "audio/wave")


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
