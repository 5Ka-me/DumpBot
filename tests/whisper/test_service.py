"""The local Whisper service (``whisper/app.py``) with a fake model and decoder: no faster-whisper, no download.

The service is not part of the ``dumpbot`` package (it has its own image and requirements), so it is loaded from
its file. Only aiohttp is needed, which the bot depends on too.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import io
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

APP_PATH = Path(__file__).resolve().parents[2] / "whisper" / "app.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dumpbot_whisper_service", APP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module's annotations through sys.modules
    spec.loader.exec_module(module)
    return module


svc = _load()


def audio(seconds: float) -> bytes:
    """Fake "file" bytes the fake decoder understands: its duration in seconds."""
    return f"AUDIO:{seconds}".encode()


def fake_decode(data: bytes, max_samples: int) -> list[float]:
    """Like the real decoder, it stops right after ``max_samples``."""
    if not data.startswith(b"AUDIO:"):
        raise ValueError("Invalid data found when processing input")
    return [0.0] * min(int(float(data[6:]) * svc.SAMPLE_RATE), max_samples + 1)


class FakeModel:
    """``segments`` (all at once), or with ``per_second`` lazily like faster-whisper: one segment per second of audio,
    each taking that long to decode."""

    supported_languages = ("en", "ru", "uk")

    def __init__(
        self,
        segments: tuple[str, ...] = (" Купить ", "хлеб и молоко. ", "  "),
        delay: float = 0.0,
        per_second: float = 0.0,
    ) -> None:
        self.segments = segments
        self.delay = delay
        self.per_second = per_second
        self.calls: list[dict[str, Any]] = []
        self.produced = 0  # lazy segments decoded so far
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def transcribe(self, samples: Any, *, language: str | None, beam_size: int, vad_filter: bool):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            self.calls.append({"samples": len(samples), "language": language, "beam": beam_size, "vad": vad_filter})
        finally:
            if not self.per_second:
                self._done()
        info = SimpleNamespace(language=language or "ru")
        if self.per_second:
            return self._lazy(len(samples) // svc.SAMPLE_RATE), info
        return (SimpleNamespace(text=t) for t in self.segments), info

    def _lazy(self, n: int):
        try:
            for i in range(n):
                time.sleep(self.per_second)
                self.produced += 1
                yield SimpleNamespace(text=f"s{i}")
        finally:
            self._done()

    def _done(self) -> None:
        with self._lock:
            self.active -= 1


class Loader:
    """Counts loads; ``errors`` are raised by the first loads (a failed first download)."""

    def __init__(self, model: FakeModel, *errors: BaseException) -> None:
        self.model = model
        self.errors = list(errors)
        self.configs: list[Any] = []

    def __call__(self, cfg: Any) -> FakeModel:
        self.configs.append(cfg)
        if self.errors:
            raise self.errors.pop(0)
        return self.model


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def loader(model: FakeModel) -> Loader:
    return Loader(model)


@pytest.fixture
async def client(loader: Loader) -> AsyncIterator[TestClient]:
    app = svc.make_app(svc.Config(), loader=loader, decoder=fake_decode)
    async with TestClient(TestServer(app, host="127.0.0.1")) as c:
        yield c


async def _post(client: TestClient, body: Any) -> tuple[int, dict[str, Any]]:
    resp = await client.post("/transcribe", json=body)
    return resp.status, await resp.json()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# --- /health and lazy loading -------------------------------------------------------------------------------------


async def test_health_before_and_after_the_lazy_load(client, loader, model) -> None:
    resp = await client.get("/health")
    assert resp.status == 200 and await resp.json() == {"status": "ok", "model": "small", "loaded": False}
    assert loader.configs == []  # nothing is loaded (or downloaded) at startup

    for _ in range(2):
        status, _ = await _post(client, {"audio_b64": b64(audio(2.5)), "language": "ru"})
        assert status == 200
    assert len(loader.configs) == 1  # loaded once, then kept in memory
    assert (await (await client.get("/health")).json())["loaded"] is True


async def test_transcribe_happy_path(client, model) -> None:
    status, body = await _post(client, {"audio_b64": b64(audio(2.5)), "language": "ru"})
    assert status == 200
    assert body["text"] == "Купить хлеб и молоко."  # segments stripped and joined, empty ones dropped
    assert (body["language"], body["duration"], body["model"]) == ("ru", 2.5, "small")
    assert isinstance(body["processing_ms"], int) and body["processing_ms"] >= 0
    assert model.calls == [{"samples": 40000, "language": "ru", "beam": 1, "vad": True}]


async def test_auto_language_is_detected(client, model) -> None:
    status, body = await _post(client, {"audio_b64": b64(audio(1))})
    assert status == 200 and body["language"] == "ru"
    assert model.calls[0]["language"] is None


async def test_config_reaches_the_model() -> None:
    model = FakeModel()
    loader = Loader(model)
    cfg = svc.Config(model="base", threads=2, beam_size=5, vad=False)
    app = svc.make_app(cfg, loader=loader, decoder=fake_decode)
    async with TestClient(TestServer(app, host="127.0.0.1")) as c:
        status, body = await _post(c, {"audio_b64": b64(audio(1)), "language": "en"})
    assert status == 200 and body["model"] == "base"
    assert loader.configs[0].cpu_threads == 2
    assert model.calls[0] | {"samples": 0} == {"samples": 0, "language": "en", "beam": 5, "vad": False}


async def test_empty_audio_needs_no_model(client, loader) -> None:
    status, body = await _post(client, {"audio_b64": b64(audio(0)), "language": "ru"})
    assert status == 200 and body["text"] == "" and body["duration"] == 0
    assert loader.configs == []


# --- limits and errors --------------------------------------------------------------------------------------------


async def test_audio_over_120_seconds_is_413_without_loading_the_model(client, loader) -> None:
    status, body = await _post(client, {"audio_b64": b64(audio(120.5))})
    assert status == 413 and "120" in body["error"]
    assert loader.configs == []
    status, _ = await _post(client, {"audio_b64": b64(audio(120))})
    assert status == 200


async def test_long_audio_is_never_decoded_whole(loader) -> None:
    """Twelve hours of silence fit into a few MB of FLAC (and a forwarded file's duration is whatever its sender
    said): the decoder is told to stop right after the limit, so memory stays bounded and the answer is 413."""
    asked: list[int] = []

    def decoder(data: bytes, max_samples: int) -> list[float]:
        asked.append(max_samples)
        return fake_decode(data, max_samples)

    app = svc.make_app(svc.Config(max_audio_s=120), loader=loader, decoder=decoder)
    async with TestClient(TestServer(app, host="127.0.0.1")) as c:
        status, body = await _post(c, {"audio_b64": b64(audio(12 * 3600))})
    assert status == 413 and body["error"] == "audio is longer than the limit of 120 s"
    assert asked == [120 * svc.SAMPLE_RATE] and loader.configs == []


async def test_body_over_the_limit_is_413(loader) -> None:
    app = svc.make_app(svc.Config(max_body_mb=1), loader=loader, decoder=fake_decode)
    async with TestClient(TestServer(app, host="127.0.0.1")) as c:
        status, body = await _post(c, {"audio_b64": "A" * (1024 * 1024 + 16)})
    assert status == 413 and "1 MB" in body["error"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ([1, 2], "JSON object"),
        ({}, "audio_b64"),
        ({"audio_b64": ""}, "audio_b64"),
        ({"audio_b64": 5}, "audio_b64"),
        ({"audio_b64": "@@not base64@@"}, "base64"),
        ({"audio_b64": b64(audio(1)), "language": "Russian"}, "language"),
        ({"audio_b64": b64(audio(1)), "language": 7}, "language"),
        ({"audio_b64": b64(b"garbage")}, "cannot decode"),
        ({"audio_b64": b64(audio(1)), "language": "de"}, "not supported"),
    ],
)
async def test_bad_requests_are_400(client, body, message) -> None:
    status, answer = await _post(client, body)
    assert status == 400 and message in answer["error"]


async def test_not_json_is_400(client) -> None:
    resp = await client.post("/transcribe", data=b"audio", headers={"Content-Type": "application/json"})
    assert resp.status == 400 and "JSON" in (await resp.json())["error"]


async def test_failed_load_is_503_and_retried_by_the_next_request(model) -> None:
    loader = Loader(model, OSError("huggingface.co unreachable"))
    app = svc.make_app(svc.Config(), loader=loader, decoder=fake_decode)
    async with TestClient(TestServer(app, host="127.0.0.1")) as c:
        status, body = await _post(c, {"audio_b64": b64(audio(1))})
        assert status == 503 and "try again later" in body["error"]
        assert (await (await c.get("/health")).json())["loaded"] is False
        status, _ = await _post(c, {"audio_b64": b64(audio(1))})
    assert status == 200 and len(loader.configs) == 2


async def test_requests_are_transcribed_one_at_a_time() -> None:
    model = FakeModel(delay=0.2)
    app = svc.make_app(svc.Config(), loader=Loader(model), decoder=fake_decode)
    async with TestClient(TestServer(app, host="127.0.0.1")) as c:
        results = await asyncio.gather(*(_post(c, {"audio_b64": b64(audio(1))}) for _ in range(3)))
    assert [status for status, _ in results] == [200, 200, 200]
    assert model.max_active == 1 and len(model.calls) == 3


async def test_health_answers_during_a_transcription() -> None:
    model = FakeModel(delay=0.5)
    app = svc.make_app(svc.Config(), loader=Loader(model), decoder=fake_decode)
    async with TestClient(TestServer(app, host="127.0.0.1")) as c:
        work = asyncio.create_task(_post(c, {"audio_b64": b64(audio(1))}))
        await asyncio.sleep(0.1)
        started = time.monotonic()
        resp = await c.get("/health")
        assert resp.status == 200 and time.monotonic() - started < 0.3
        assert (await work)[0] == 200


async def _give_up(client: TestClient, data: bytes, when: Callable[[], bool]) -> None:
    """Send a request and disconnect (like the bot's timeout) once ``when()`` holds on the service's side."""
    request = asyncio.create_task(client.post("/transcribe", json={"audio_b64": b64(data)}))
    deadline = time.monotonic() + 5
    while not when():
        assert time.monotonic() < deadline and not request.done(), "the service never got that far"
        await asyncio.sleep(0.01)
    request.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await request


async def test_a_client_that_gives_up_stops_its_transcription() -> None:
    """The bot's timeout ended the wait for 100 s of audio: the service stops after the current segment instead of
    decoding the rest for nobody, and the next voice note does not queue behind that work."""
    model = FakeModel(per_second=0.05)  # 100 s of audio = 5 s of work
    app = svc.make_app(svc.Config(), loader=Loader(model), decoder=fake_decode)
    async with TestClient(TestServer(app, host="127.0.0.1")) as c:
        await _give_up(c, audio(100), lambda: model.produced >= 3)  # the transcription is under way
        started = time.monotonic()
        status, body = await _post(c, {"audio_b64": b64(audio(2))})
        waited = time.monotonic() - started
    assert status == 200 and body["text"] == "s0 s1"
    assert waited < 2.0  # not behind the ~4.5 s left of the abandoned one
    assert model.produced < 40  # a few segments of the first request, then the 2 of the second, not 102
    assert model.max_active == 1  # the slot was given up only once the abandoned thread had stopped


async def test_a_model_load_outlives_the_request_that_started_it(model) -> None:
    """The first download may take longer than the bot waits: the load goes on, and the next request uses it."""
    release = threading.Event()

    class SlowLoader(Loader):
        def __call__(self, cfg: Any) -> FakeModel:
            self.configs.append(cfg)
            release.wait(10)
            return self.model

    loader = SlowLoader(model)
    app = svc.make_app(svc.Config(), loader=loader, decoder=fake_decode)
    async with TestClient(TestServer(app, host="127.0.0.1")) as c:
        await _give_up(c, audio(1), lambda: bool(loader.configs))  # the load has started
        await asyncio.sleep(0.2)  # the service notices the disconnect while still loading
        release.set()
        status, _ = await _post(c, {"audio_b64": b64(audio(1))})
    assert status == 200 and len(loader.configs) == 1  # loaded once, not again for the second request


def test_main_cancels_the_requests_of_clients_that_left(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(svc.web, "run_app", lambda app, **kwargs: seen.update(kwargs))
    svc.main([])
    assert seen["handler_cancellation"] is True  # aiohttp's default (False) lets a handler run on for nobody


# --- the real decoder (PyAV + numpy: in the service's image, not in the bot's environment) -------------------------


def _flac_of_silence(seconds: int, rate: int = 8000) -> bytes:
    import av
    import numpy as np

    buf = io.BytesIO()
    with av.open(buf, mode="w", format="flac") as out:
        stream = out.add_stream("flac", rate=rate, layout="mono", format="s16")
        chunk, pts = 4000, 0
        for _ in range(seconds * rate // chunk):
            frame = av.AudioFrame.from_ndarray(np.zeros((1, chunk), dtype=np.int16), format="s16", layout="mono")
            frame.sample_rate, frame.pts = rate, pts
            pts += chunk
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return buf.getvalue()


def test_the_real_decoder_stops_right_after_the_limit() -> None:
    pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    limit = 120 * svc.SAMPLE_RATE
    hour = _flac_of_silence(3600)
    assert len(hour) < 2_000_000  # an hour of silence is a small file

    samples = svc.decode_audio(hour, limit)
    assert limit < len(samples) <= limit + svc.SAMPLE_RATE  # stopped within a second after the limit
    short = svc.decode_audio(_flac_of_silence(2), limit)
    assert short.dtype == np.float32 and abs(len(short) - 2 * svc.SAMPLE_RATE) <= 400
    with pytest.raises(Exception):  # noqa: B017 - PyAV raises its own error types
        svc.decode_audio(b"not audio at all", limit)


# --- config -------------------------------------------------------------------------------------------------------


def test_config_defaults() -> None:
    cfg = svc.Config.from_env({})
    assert (cfg.model, cfg.threads, cfg.beam_size, cfg.compute_type) == ("small", 0, 1, "int8")
    assert (cfg.max_audio_s, cfg.models_dir, cfg.port, cfg.vad) == (120.0, "/models", 9000, True)
    assert cfg.cpu_threads == svc.available_cpus() >= 1  # 0 = all cores


def test_config_from_env() -> None:
    env = {
        "WHISPER_MODEL": "medium",
        "WHISPER_THREADS": "3",
        "WHISPER_BEAM_SIZE": "5",
        "WHISPER_VAD": "off",
        "WHISPER_MAX_AUDIO_S": "300",
        "WHISPER_MODELS_DIR": "/data",
        "WHISPER_PORT": "9100",
        "WHISPER_COMPUTE_TYPE": " ",  # blank = default
    }
    cfg = svc.Config.from_env(env)
    assert (cfg.model, cfg.cpu_threads, cfg.beam_size, cfg.vad) == ("medium", 3, 5, False)
    assert (cfg.max_audio_s, cfg.models_dir, cfg.port, cfg.compute_type) == (300.0, "/data", 9100, "int8")


@pytest.mark.parametrize(
    "env",
    [{"WHISPER_THREADS": "many"}, {"WHISPER_THREADS": "-1"}, {"WHISPER_MAX_AUDIO_S": "0"}, {"WHISPER_PORT": "x"}],
)
def test_bad_config_fails_at_startup(env) -> None:
    with pytest.raises(ValueError):
        svc.Config.from_env(env)
