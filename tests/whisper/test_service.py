"""The local Whisper service (``whisper/app.py``) with a fake model and decoder: no faster-whisper, no download.

The service is not part of the ``dumpbot`` package (it has its own image and requirements), so it is loaded from
its file. Only aiohttp is needed, which the bot depends on too.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
import threading
import time
from collections.abc import AsyncIterator
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


def fake_decode(data: bytes) -> list[float]:
    if not data.startswith(b"AUDIO:"):
        raise ValueError("Invalid data found when processing input")
    return [0.0] * int(float(data[6:]) * svc.SAMPLE_RATE)


class FakeModel:
    supported_languages = ("en", "ru", "uk")

    def __init__(self, segments: tuple[str, ...] = (" Купить ", "хлеб и молоко. ", "  "), delay: float = 0.0) -> None:
        self.segments = segments
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
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
            with self._lock:
                self.active -= 1
        info = SimpleNamespace(language=language or "ru")
        return (SimpleNamespace(text=t) for t in self.segments), info


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
