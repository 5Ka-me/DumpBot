"""Local speech-to-text for Dump Bot: faster-whisper (CTranslate2, CPU, int8) behind a small aiohttp API.

``POST /transcribe`` ``{"audio_b64": str, "language": "ru" | "en" | null}``
    -> 200 ``{"text", "language", "duration", "processing_ms", "model"}``;
    400 malformed request or audio that cannot be decoded; 413 audio longer than ``WHISPER_MAX_AUDIO_S`` (or a body
    over ``WHISPER_MAX_BODY_MB``); 503 the model could not be loaded (the first download failed: try again later).
``GET /health`` -> ``{"status": "ok", "model": "small", "loaded": false}``.

Any format FFmpeg reads (OGG/Opus voice notes, MP3, M4A, WAV…) is decoded by PyAV, which faster-whisper bundles.
The model is loaded on the first request: downloaded from Hugging Face into ``WHISPER_MODELS_DIR`` once, read from
there without any network afterwards, then kept in memory. Requests are transcribed one at a time.
Audio and transcripts are never logged.

Environment: ``WHISPER_MODEL`` (small; tiny | base | small | medium), ``WHISPER_THREADS`` (0 = all cores),
``WHISPER_BEAM_SIZE`` (1), ``WHISPER_COMPUTE_TYPE`` (int8), ``WHISPER_VAD`` (1: skip silence),
``WHISPER_MAX_AUDIO_S`` (120), ``WHISPER_MAX_BODY_MB`` (32), ``WHISPER_MODELS_DIR`` (/models), ``WHISPER_PORT`` (9000).

    python app.py              # serve on 0.0.0.0:9000
    python app.py --download   # only download the model into WHISPER_MODELS_DIR, then exit
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aiohttp import web

log = logging.getLogger("whisper")

SAMPLE_RATE = 16000
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}$")

Loader = Callable[["Config"], Any]  # blocking: Config -> a WhisperModel-like object
Decoder = Callable[[bytes], Sequence[float]]  # blocking: file bytes -> 16 kHz mono samples


@dataclass(frozen=True)
class Config:
    model: str = "small"
    threads: int = 0  # 0 = all available cores
    beam_size: int = 1
    compute_type: str = "int8"
    vad: bool = True
    max_audio_s: float = 120.0
    max_body_mb: int = 32
    models_dir: str = "/models"
    host: str = "0.0.0.0"
    port: int = 9000

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Config:
        """Settings from ``WHISPER_*`` variables; ``ValueError`` for a value that does not parse."""

        def get(name: str, default: object) -> str:
            value = env.get(f"WHISPER_{name}", "").strip()
            return value or str(default)

        d = cls()
        return cls(
            model=get("MODEL", d.model),
            threads=_at_least(0, int(get("THREADS", d.threads)), "WHISPER_THREADS"),
            beam_size=max(1, int(get("BEAM_SIZE", d.beam_size))),
            compute_type=get("COMPUTE_TYPE", d.compute_type),
            vad=get("VAD", "1").lower() not in ("0", "false", "no", "off"),
            max_audio_s=_at_least(1, float(get("MAX_AUDIO_S", d.max_audio_s)), "WHISPER_MAX_AUDIO_S"),
            max_body_mb=max(1, int(get("MAX_BODY_MB", d.max_body_mb))),
            models_dir=get("MODELS_DIR", d.models_dir),
            host=get("HOST", d.host),
            port=int(get("PORT", d.port)),
        )

    @property
    def cpu_threads(self) -> int:
        return self.threads or available_cpus()


def _at_least[N: (int, float)](low: int, value: N, name: str) -> N:
    if value < low:
        raise ValueError(f"{name} must be >= {low}")
    return value


def available_cpus() -> int:
    """Cores this process may run on (a container's CPU limit included where the OS reports it)."""
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # not Linux
        return os.cpu_count() or 1


# --- faster-whisper (imported lazily: the tests use fakes and do not install it) ----------------------------------


def load_model(cfg: Config) -> Any:
    """The model from ``models_dir`` without network; downloaded from Hugging Face first if it is not there yet."""
    from faster_whisper import WhisperModel

    kwargs: dict[str, Any] = {
        "device": "cpu",
        "compute_type": cfg.compute_type,
        "cpu_threads": cfg.cpu_threads,
        "download_root": cfg.models_dir,
    }
    try:
        return WhisperModel(cfg.model, local_files_only=True, **kwargs)
    except Exception as e:
        log.info("model %s not in %s yet (%s): downloading it once", cfg.model, cfg.models_dir, type(e).__name__)
    return WhisperModel(cfg.model, **kwargs)


def decode_audio(data: bytes) -> Sequence[float]:
    """16 kHz mono float32 samples of any audio file FFmpeg can read."""
    from faster_whisper import decode_audio as fw_decode

    return fw_decode(io.BytesIO(data), sampling_rate=SAMPLE_RATE)


def download(cfg: Config) -> str:
    from faster_whisper import download_model

    return download_model(cfg.model, cache_dir=cfg.models_dir)


# --- service ------------------------------------------------------------------------------------------------------


class BadRequest(Exception):
    """The request cannot be served as sent (-> 400)."""


class Service:
    """Lazily loaded model + the HTTP handlers. ``loader`` / ``decoder`` are replaceable for tests."""

    def __init__(self, cfg: Config, *, loader: Loader = load_model, decoder: Decoder = decode_audio) -> None:
        self.cfg = cfg
        self._loader = loader
        self._decoder = decoder
        self._model: Any = None
        self._load_lock = asyncio.Lock()
        self._busy = asyncio.Semaphore(1)  # one transcription at a time: bounded memory, no CPU oversubscription

    @property
    def loaded(self) -> bool:
        return self._model is not None

    async def model(self) -> Any:
        """The model, loaded on first use (in a thread: the event loop keeps answering /health)."""
        async with self._load_lock:
            if self._model is None:
                started, cfg = time.monotonic(), self.cfg
                log.info("loading model %s (%d threads, %s)", cfg.model, cfg.cpu_threads, cfg.compute_type)
                self._model = await asyncio.to_thread(self._loader, self.cfg)
                log.info("model %s loaded in %.1f s", self.cfg.model, time.monotonic() - started)
        return self._model

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "model": self.cfg.model, "loaded": self.loaded})

    async def transcribe(self, request: web.Request) -> web.Response:
        try:
            audio, language = await self._parse(request)
        except web.HTTPRequestEntityTooLarge:
            return _error(413, f"request body is over {self.cfg.max_body_mb} MB")
        except BadRequest as e:
            return _error(400, str(e))
        async with self._busy:
            return await self._transcribe(audio, language)

    async def _parse(self, request: web.Request) -> tuple[bytes, str | None]:
        try:
            body = await request.json()
        except ValueError as e:
            raise BadRequest("body must be JSON") from e
        if not isinstance(body, dict):
            raise BadRequest("body must be a JSON object")
        audio_b64, language = body.get("audio_b64"), body.get("language")
        if not isinstance(audio_b64, str) or not audio_b64:
            raise BadRequest("audio_b64 must be a non-empty base64 string")
        if language is not None and (not isinstance(language, str) or not LANGUAGE_RE.match(language)):
            raise BadRequest("language must be a language code such as 'ru' or 'en', or null")
        try:
            audio = base64.b64decode(audio_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise BadRequest("audio_b64 is not valid base64") from e
        return audio, language

    async def _transcribe(self, audio: bytes, language: str | None) -> web.Response:
        started = time.monotonic()
        try:
            samples = await asyncio.to_thread(self._decoder, audio)
        except Exception as e:
            log.info("cannot decode %d bytes of audio: %s", len(audio), type(e).__name__)
            return _error(400, "cannot decode the audio")
        duration = len(samples) / SAMPLE_RATE
        if duration > self.cfg.max_audio_s:
            log.info("%.1f s of audio is over the %.0f s limit", duration, self.cfg.max_audio_s)
            return _error(413, f"audio is {duration:.0f} s long, the limit is {self.cfg.max_audio_s:.0f} s")
        text, detected = "", language or ""
        if duration > 0:
            try:
                model = await self.model()
            except Exception as e:
                log.error("model %s cannot be loaded: %s: %s", self.cfg.model, type(e).__name__, e)
                return _error(503, "the model is not available yet, try again later")
            if language is not None and language not in getattr(model, "supported_languages", (language,)):
                return _error(400, f"language {language!r} is not supported by this model")
            text, detected = await asyncio.to_thread(self._run, model, samples, language)
        ms = int((time.monotonic() - started) * 1000)
        log.info("transcribed %.1f s of audio in %d ms (language %s, %d chars)", duration, ms, detected, len(text))
        return web.json_response(
            {
                "text": text,
                "language": detected,
                "duration": round(duration, 2),
                "processing_ms": ms,
                "model": self.cfg.model,
            }
        )

    def _run(self, model: Any, samples: Sequence[float], language: str | None) -> tuple[str, str]:
        segments, info = model.transcribe(
            samples, language=language, beam_size=self.cfg.beam_size, vad_filter=self.cfg.vad
        )
        text = " ".join(part for part in (s.text.strip() for s in segments) if part)  # runs the decoding
        return text, str(info.language or language or "")


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def make_app(cfg: Config, *, loader: Loader = load_model, decoder: Decoder = decode_audio) -> web.Application:
    service = Service(cfg, loader=loader, decoder=decoder)
    app = web.Application(client_max_size=cfg.max_body_mb * 1024 * 1024)
    app.router.add_get("/health", service.health)
    app.router.add_post("/transcribe", service.transcribe)
    return app


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per downloaded file otherwise
    cfg = Config.from_env()
    if "--download" in (sys.argv[1:] if argv is None else argv):
        log.info("downloading model %s into %s", cfg.model, cfg.models_dir)
        log.info("model files: %s", download(cfg))
        return
    log.info(
        "whisper service on %s:%d, model %s (loaded on the first request), %d threads, beam %d, max %.0f s",
        cfg.host,
        cfg.port,
        cfg.model,
        cfg.cpu_threads,
        cfg.beam_size,
        cfg.max_audio_s,
    )
    web.run_app(make_app(cfg), host=cfg.host, port=cfg.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
