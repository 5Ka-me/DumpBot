"""Fixtures for the Mini App backend tests (``dumpbot.web``, docs/features/miniapp-api.md).

The real aiohttp application (``build_web_app``) runs on 127.0.0.1 (``aiohttp.test_utils.TestServer``); requests carry
initData signed with the test bot token, exactly as Telegram signs it (``Api.init_data``). The bot is ``FakeBot``
(records calls, serves file bytes for picture previews); the processor's AI calls are ``FakeAI`` (autouse), so the
inbox runs through the real job handlers without network. DB tests use Postgres from ``tests/conftest.py``
(``TEST_DATABASE_URL``).

Reference time: Tue 2026-10-06 10:00 Europe/Warsaw (CEST, UTC+2), frozen via ``dumpbot.clock.utcnow``.
Test files cannot import each other (``--import-mode=importlib``), so helpers are fixtures.
"""

from __future__ import annotations

import io
import json
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from dumpbot import clock
from dumpbot.ai.extract import ExtractInput, ExtractOutput, SplitInput, SplitOutput
from dumpbot.ai.schemas import ExtractedItem, Extraction, StepSpec
from dumpbot.config import Settings
from dumpbot.context import AppContext
from dumpbot.db.models import Job, RawMessage
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import CreatedItem
from dumpbot.services import items, processor
from dumpbot.web import auth
from dumpbot.web.server import build_web_app
from dumpbot.workers.jobs import JobWorker

WARSAW = ZoneInfo("Europe/Warsaw")
OWNER = 4242
STRANGER = 777
TOKEN = "123456:TEST-token-for-the-mini-app"
T0 = datetime(2026, 10, 6, 10, 0, tzinfo=WARSAW).astimezone(UTC)


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=WARSAW).astimezone(UTC)


# --- clock -------------------------------------------------------------------------------------------------------


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    fc = FrozenClock(T0)
    monkeypatch.setattr(clock, "utcnow", fc)
    return fc


# --- fakes -------------------------------------------------------------------------------------------------------


class FakeBot:
    """Stands in for ``aiogram.Bot``: records calls; ``files`` holds the bytes ``download`` returns; queued exceptions
    are raised by the next calls of a method."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, list[BaseException]] = defaultdict(list)
        self.files: dict[str, bytes] = {}
        self.next_id = 9000

    def fail(self, method: str, *errors: BaseException) -> None:
        self.errors[method].extend(errors)

    def calls_of(self, method: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if name == method]

    @property
    def texts(self) -> list[str]:
        return [kw["text"] for kw in self.calls_of("send_message")]

    def _call(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        if self.errors[method]:
            raise self.errors[method].pop(0)

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        self._call("send_message", kwargs)
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id, chat=SimpleNamespace(id=kwargs["chat_id"]))

    async def edit_message_text(self, **kwargs: Any) -> SimpleNamespace:
        self._call("edit_message_text", kwargs)
        return SimpleNamespace(message_id=kwargs["message_id"])

    async def set_message_reaction(self, **kwargs: Any) -> bool:
        self._call("set_message_reaction", kwargs)
        return True

    async def download(self, file: Any, destination: Any = None, **kwargs: Any) -> io.BytesIO:
        self._call("download", {"file": file})
        return io.BytesIO(self.files.get(str(file), b""))


class FakeAI:
    """Scripted ``extract`` / ``split_steps`` / ``answer_query`` / ``fetch_titles`` of the processor."""

    def __init__(self) -> None:
        self.outputs: list[Extraction | BaseException] = []
        self.inputs: list[ExtractInput] = []
        self.splits: list[list[str] | BaseException] = []
        self.split_inputs: list[SplitInput] = []
        self.answers: list[str | BaseException] = []

    def push(self, *outputs: dict[str, Any] | BaseException) -> None:
        for out in outputs:
            self.outputs.append(Extraction.model_validate(out) if isinstance(out, dict) else out)

    async def extract(self, client: Any, settings: Any, inp: ExtractInput) -> ExtractOutput:
        self.inputs.append(inp)
        out = self.outputs.pop(0) if self.outputs else Extraction()
        if isinstance(out, BaseException):
            raise out
        return ExtractOutput(extraction=out, model="fake-model", prompt_version="v3")

    async def split_steps(self, client: Any, settings: Any, inp: SplitInput) -> SplitOutput:
        self.split_inputs.append(inp)
        result = self.splits.pop(0) if self.splits else []
        if isinstance(result, BaseException):
            raise result
        return SplitOutput(steps=[StepSpec(title=t) for t in result], model="fake-model", prompt_version="v3")

    async def answer_query(self, client: Any, settings: Any, question: str, rows: list, now: Any, prefs: Any) -> str:
        result = self.answers.pop(0) if self.answers else "Завтра <созвон>"
        if isinstance(result, BaseException):
            raise result
        return result

    async def fetch_titles(self, urls: list[str], **kwargs: Any) -> dict[str, str]:
        return {}


@pytest.fixture(autouse=True)
def fake_ai(monkeypatch: pytest.MonkeyPatch) -> FakeAI:
    ai = FakeAI()
    monkeypatch.setattr(processor, "extract", ai.extract)
    monkeypatch.setattr(processor, "split_steps", ai.split_steps)
    monkeypatch.setattr(processor, "answer_query", ai.answer_query)
    monkeypatch.setattr(processor, "fetch_titles", ai.fetch_titles)
    return ai


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


# --- app ---------------------------------------------------------------------------------------------------------


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"bot_token": TOKEN, "chutes_api_key": "test-key", "owner_id": OWNER}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    return make_settings


@pytest.fixture
async def sessionmaker(engine: AsyncEngine, db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Pooled variant of the shared fixture (``engine`` still truncates the tables after the test)."""
    pooled = create_async_engine(db_url, pool_size=5)
    yield async_sessionmaker(pooled, expire_on_commit=False)
    await pooled.dispose()


@pytest.fixture
def ctx(sessionmaker: async_sessionmaker[AsyncSession], bot: FakeBot, frozen: FrozenClock) -> AppContext:
    return AppContext(settings=make_settings(), sessionmaker=sessionmaker, bot=bot, ai=None, stt=None)  # type: ignore[arg-type]


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    """A frontend build: index.html, a hashed asset, a favicon."""
    root = tmp_path / "webapp"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Свалка</title><div id=root></div>", encoding="utf-8")
    (root / "assets" / "app-1a2b.js").write_text("console.log('app')", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the build", encoding="utf-8")
    return root


@dataclass
class Reply:
    status: int
    data: Any
    headers: Any


class Api:
    """An HTTP client of the running app; requests carry the owner's initData unless ``auth`` says otherwise."""

    def __init__(self, client: TestClient, frozen: FrozenClock) -> None:
        self.client = client
        self.frozen = frozen

    def init_data(self, user_id: int = OWNER, *, age: timedelta = timedelta(minutes=1), **extra: str) -> str:
        fields = {
            "query_id": "AAH0dGVzdA",
            "user": json.dumps({"id": user_id, "first_name": "Дима", "username": "dima"}, ensure_ascii=False),
            "auth_date": str(int((self.frozen.now - age).timestamp())),
            "signature": "c2lnbmF0dXJl",
            **extra,
        }
        return urlencode({**fields, "hash": auth.sign(fields, TOKEN)})

    async def call(
        self, method: str, path: str, body: Any = None, *, auth_header: str | None = None, raw: bool = False
    ) -> Reply:
        headers = {"Authorization": auth_header if auth_header is not None else f"tma {self.init_data()}"}
        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        async with self.client.request(method, path, **kwargs) as response:
            if raw or not response.content_type.endswith("json"):
                data: Any = await response.read()
            else:
                data = await response.json()
            return Reply(response.status, data, response.headers)

    async def get(self, path: str, **kw: Any) -> Reply:
        return await self.call("GET", path, **kw)

    async def post(self, path: str, body: Any = None, **kw: Any) -> Reply:
        return await self.call("POST", path, body if body is not None else {}, **kw)

    async def patch(self, path: str, body: Any, **kw: Any) -> Reply:
        return await self.call("PATCH", path, body, **kw)

    async def delete(self, path: str, **kw: Any) -> Reply:
        return await self.call("DELETE", path, **kw)


@pytest.fixture
async def api(ctx: AppContext, static_dir: Path, frozen: FrozenClock) -> AsyncIterator[Api]:
    server = TestServer(build_web_app(ctx, static_dir=static_dir), host="127.0.0.1")
    client = TestClient(server)
    await client.start_server()
    try:
        yield Api(client, frozen)
    finally:
        await client.close()


# --- data helpers ------------------------------------------------------------------------------------------------


@pytest.fixture
def make_items(sessionmaker: async_sessionmaker[AsyncSession]) -> Callable[..., Awaitable[list[CreatedItem]]]:
    """``await make_items({"kind": "task", "title": "…", "when": {...}}, raw_id=…)`` -> committed CreatedItems."""

    async def _make(*specs: dict[str, Any], now: datetime = T0, raw_id: int | None = None) -> list[CreatedItem]:
        async with sessionmaker() as session:
            raw = await session.get(RawMessage, raw_id) if raw_id is not None else None
            created = await items.create_items(
                session,
                raw=raw,
                extraction=Extraction(items=[ExtractedItem.model_validate(s) for s in specs]),
                source="text",
                source_text=None,
                source_meta=None,
                urls=[],
                prefs=Prefs(),
                now=now,
                model="test",
                prompt_version="v3",
            )
            await session.commit()
        return created

    return _make


@pytest.fixture
def add_raw(sessionmaker: async_sessionmaker[AsyncSession]) -> Callable[..., Awaitable[RawMessage]]:
    """``await add_raw(message_id=…, content_type="photo", file_id=…)`` -> committed Telegram RawMessage."""

    async def _add(**kw: Any) -> RawMessage:
        values: dict[str, Any] = {
            "chat_id": OWNER,
            "message_id": 100,
            "content_type": "text",
            "text": "",
            "tg_date": T0,
            "status": "done",
            "raw_json": {},
        }
        values.update(kw)
        async with sessionmaker() as session:
            raw = RawMessage(**values)
            session.add(raw)
            await session.commit()
        return raw

    return _add


@pytest.fixture
def db(sessionmaker: async_sessionmaker[AsyncSession]) -> Callable[..., Awaitable[list[Any]]]:
    async def _all(stmt: Any) -> list[Any]:
        async with sessionmaker() as session:
            return list((await session.scalars(stmt)).all())

    return _all


@pytest.fixture
def run_jobs(ctx: AppContext) -> Callable[[], Awaitable[int]]:
    """Run every due job with the real handlers (``processor.HANDLERS``)."""

    async def _run() -> int:
        worker = JobWorker(ctx, processor.HANDLERS)
        total = 0
        while ran := await worker.run_once():
            total += ran
        return total

    return _run


@pytest.fixture
def jobs_all(db: Callable[..., Awaitable[list[Any]]]) -> Callable[[], Awaitable[list[Job]]]:
    async def _jobs() -> list[Job]:
        return await db(select(Job).order_by(Job.id))

    return _jobs
