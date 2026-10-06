"""Local fakes for the AI tests: an aiohttp server mimicking chutes.ai (/v1/chat/completions, /v1/models),
the AudioDojo STT chute (/stt/whisper), the local Whisper service (/local/transcribe) and arbitrary web pages
(/pages/{name}) for link titles.

Test files cannot import each other (``--import-mode=importlib``), so helpers live on the ``FakeChutes`` object
and in fixtures.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from dumpbot.ai.client import ChutesClient
from dumpbot.config import Settings
from dumpbot.domain.prefs import Prefs

GEMMA = "google/gemma-4-31B-turbo-TEE"
QWEN = "Qwen/Qwen3-32B-TEE"
QWEN_VL = "Qwen/Qwen3.6-27B-TEE"
DEEPSEEK = "deepseek-ai/DeepSeek-V4-Flash-0731-TEE"

MODELS_PAYLOAD = {
    "object": "list",
    "data": [
        {
            "id": GEMMA,
            "pricing": {"prompt": 0.12, "completion": 0.37, "input_cache_read": 0.012},
            "input_modalities": ["text", "image"],
            "supported_features": ["json_mode", "tools", "structured_outputs", "reasoning"],
            "context_length": 131072,
        },
        {
            "id": QWEN,
            "pricing": {"prompt": 0.104, "completion": 0.416},
            "input_modalities": ["text"],
            "context_length": 40960,
        },
        {"id": QWEN_VL, "pricing": {"prompt": 0.30, "completion": 2.00}, "input_modalities": ["text", "image"]},
        {"id": DEEPSEEK, "pricing": {"prompt": 0.44, "completion": 1.32}, "input_modalities": ["text"]},
        {"id": "broken/no-pricing"},
    ],
}


@dataclass
class Reply:
    status: int = 200
    body: Any = None  # dict/list -> JSON; str/bytes -> raw body
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    content_type: str = "text/plain"
    charset: str | None = None


class FakeChutes:
    """Scriptable stand-in for chutes.ai. Unscripted chat/STT requests get HTTP 418 (-> AIBadResponse)."""

    def __init__(self) -> None:
        self.base = ""
        self.chat_queue: list[Reply] = []
        self.chat_requests: list[dict] = []
        self.chat_auth: list[str | None] = []
        self.models_reply = Reply(body=MODELS_PAYLOAD)
        self.models_hits = 0
        self.models_auth: list[str | None] = []
        self.stt_queue: list[Reply] = []
        self.stt_requests: list[dict] = []
        self.stt_auth: list[str | None] = []
        self.local_queue: list[Reply] = []
        self.local_requests: list[dict] = []
        self.local_auth: list[str | None] = []
        self.pages: dict[str, Reply] = {}
        self.page_hits: list[str] = []

    # -- scripting ----------------------------------------------------------------------------------------------

    def chat_ok(
        self,
        content: str,
        *,
        prompt_tokens: int = 1000,
        completion_tokens: int = 200,
        usage: bool = True,
        cached_tokens: Any = None,
        reasoning: str | None = None,
        finish_reason: str = "stop",
        delay: float = 0.0,
    ) -> None:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning is not None:
            message["reasoning_content"] = reasoning
        body: dict[str, Any] = {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": GEMMA,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        }
        if usage:
            body["usage"] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
            if cached_tokens is not None:
                body["usage"]["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
        self.chat_queue.append(Reply(body=body, delay=delay))

    def chat_json(self, obj: Any, **kwargs: Any) -> None:
        self.chat_ok(json.dumps(obj, ensure_ascii=False), **kwargs)

    def chat_error(self, status: int, body: str = "error", headers: dict[str, str] | None = None) -> None:
        self.chat_queue.append(Reply(status=status, body=body, headers=headers or {}))

    def chat_raw(self, body: Any, status: int = 200) -> None:
        """Queue an arbitrary chat response body (e.g. a malformed 200)."""
        self.chat_queue.append(Reply(status=status, body=body))

    def set_models(self, body: Any = None, status: int = 200) -> None:
        """Replace the GET /v1/models response (default: the price snapshot)."""
        self.models_reply = Reply(status=status, body=MODELS_PAYLOAD if body is None else body)

    def stt_ok(self, text: str, *, delay: float = 0.0) -> None:
        self.stt_queue.append(Reply(body={"text": text}, delay=delay))

    def stt_error(self, status: int, body: str = "error", headers: dict[str, str] | None = None) -> None:
        self.stt_queue.append(Reply(status=status, body=body, headers=headers or {}))

    def stt_raw(self, body: Any, status: int = 200) -> None:
        self.stt_queue.append(Reply(status=status, body=body))

    def local_ok(self, text: str, *, duration: float = 3.5, model: str = "small", delay: float = 0.0) -> None:
        body = {"text": text, "language": "ru", "duration": duration, "processing_ms": 420, "model": model}
        self.local_queue.append(Reply(body=body, delay=delay))

    def local_error(self, status: int, body: Any = "error") -> None:
        self.local_queue.append(Reply(status=status, body=body))

    def local_raw(self, body: Any, status: int = 200) -> None:
        self.local_queue.append(Reply(status=status, body=body))

    @property
    def local_url(self) -> str:
        """``LOCAL_STT_URL`` of the fake local Whisper."""
        return f"{self.base}/local"

    def page(self, name: str, body: str | bytes, **kwargs: Any) -> str:
        """Serve ``body`` at /pages/{name}; returns the absolute URL."""
        kwargs.setdefault("content_type", "text/html")
        self.pages[name] = Reply(body=body, **kwargs)
        return self.url(name)

    def url(self, name: str) -> str:
        return f"{self.base}/pages/{name}"

    @property
    def chat_models(self) -> list[str]:
        return [r["model"] for r in self.chat_requests]

    # -- server -------------------------------------------------------------------------------------------------

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._chat)
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/stt/whisper", self._stt)
        app.router.add_post("/local/transcribe", self._local)
        app.router.add_get("/pages/{name}", self._page)
        return app

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        self.chat_requests.append(await request.json())
        self.chat_auth.append(request.headers.get("Authorization"))
        reply = self.chat_queue.pop(0) if self.chat_queue else Reply(status=418, body="no scripted reply")
        return await _respond(reply)

    async def _models(self, request: web.Request) -> web.StreamResponse:
        self.models_hits += 1
        self.models_auth.append(request.headers.get("Authorization"))
        return await _respond(self.models_reply)

    async def _stt(self, request: web.Request) -> web.StreamResponse:
        self.stt_requests.append(await request.json())
        self.stt_auth.append(request.headers.get("Authorization"))
        reply = self.stt_queue.pop(0) if self.stt_queue else Reply(status=418, body="no scripted reply")
        return await _respond(reply)

    async def _local(self, request: web.Request) -> web.StreamResponse:
        self.local_requests.append(await request.json())
        self.local_auth.append(request.headers.get("Authorization"))
        reply = self.local_queue.pop(0) if self.local_queue else Reply(status=418, body="no scripted reply")
        return await _respond(reply)

    async def _page(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        self.page_hits.append(name)
        reply = self.pages.get(name)
        if reply is None:
            return web.Response(status=404, text="<title>Not found</title>", content_type="text/html")
        return await _respond(reply)


async def _respond(reply: Reply) -> web.StreamResponse:
    if reply.delay:
        await asyncio.sleep(reply.delay)
    if isinstance(reply.body, dict | list):
        return web.json_response(reply.body, status=reply.status, headers=reply.headers)
    body = reply.body if isinstance(reply.body, bytes) else (reply.body or "").encode(reply.charset or "utf-8")
    if any(k.lower() == "content-type" for k in reply.headers):
        return web.Response(body=body, status=reply.status, headers=reply.headers)
    return web.Response(
        body=body, status=reply.status, headers=reply.headers, content_type=reply.content_type, charset=reply.charset
    )


@pytest.fixture
async def fake() -> AsyncIterator[FakeChutes]:
    fc = FakeChutes()
    server = TestServer(fc.app(), host="127.0.0.1")
    await server.start_server()
    fc.base = str(server.make_url("/")).rstrip("/")
    try:
        yield fc
    finally:
        await server.close()


@pytest.fixture
def make_settings(fake: FakeChutes) -> Callable[..., Settings]:
    """Settings pointing at the fake server; keyword overrides win. ``.env`` is never read."""

    def _make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "bot_token": "x",
            "chutes_api_key": "cpk_test",
            "chutes_base_url": f"{fake.base}/v1",
            "stt_url": f"{fake.base}/stt/whisper",
            "llm_model": GEMMA,
            "llm_fallback_model": QWEN,
            "vision_fallback_model": QWEN_VL,
            "llm_timeout_s": 5.0,
            "stt_timeout_s": 5.0,
            "llm_fallback_price_in": 1.0,
            "llm_fallback_price_out": 3.0,
            "stt_convert_wav": False,
            "stt_usd_per_second": 0.0005,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return _make


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[ChutesClient]:
    c = ChutesClient(settings)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
def warsaw() -> Prefs:
    return Prefs(tz="Europe/Warsaw")
