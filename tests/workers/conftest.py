"""Fixtures for the background-processing tests (queue, ingest, processor, digests, scheduler).

Test files cannot import each other (``--import-mode=importlib``), so fakes and helpers live here as fixtures.
DB tests use Postgres from ``tests/conftest.py`` (``TEST_DATABASE_URL``). Nothing talks to the network:
``extract`` / ``answer_query`` / ``fetch_titles`` in ``dumpbot.services.processor`` are replaced by ``FakeAI``
(autouse), the bot is ``FakeBot`` and speech-to-text is the real ``RoutedSTT`` over ``FakeSTT`` backends: ``stt`` stands
for chutes; ``local_stt`` for the local Whisper, wired in by ``with_local_stt()``.

Reference time: Tue 2026-10-06 10:00 Europe/Warsaw (CEST, UTC+2), frozen via ``dumpbot.clock.utcnow``.
"""

from __future__ import annotations

import io
import itertools
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import Chat, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from dumpbot import clock
from dumpbot.ai.extract import ExtractInput, ExtractOutput
from dumpbot.ai.schemas import ExtractedItem, Extraction
from dumpbot.ai.stt import RoutedSTT
from dumpbot.config import Settings
from dumpbot.context import AppContext
from dumpbot.db.models import BotMessage, Item, Job, Occurrence, RawMessage, Reminder
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import CreatedItem
from dumpbot.services import items, processor
from dumpbot.services.settings_repo import load_prefs, save_prefs

WARSAW = ZoneInfo("Europe/Warsaw")
OWNER = 4242


def _local(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    """Warsaw wall time -> aware UTC."""
    return datetime(year, month, day, hour, minute, second, tzinfo=WARSAW).astimezone(UTC)


T0 = _local(2026, 10, 6, 10, 0)


@pytest.fixture
def local() -> Callable[..., datetime]:
    return _local


@pytest.fixture
def t0() -> datetime:
    return T0


# --- DB engine ---------------------------------------------------------------------------------------------------


@pytest.fixture
async def sessionmaker(engine: AsyncEngine, db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Pooled variant of the shared fixture: the code under test opens many short sessions and a new connection
    costs ~0.1 s (SCRAM auth). ``engine`` is still requested so its teardown truncates the tables."""
    pooled = create_async_engine(db_url, pool_size=5)
    yield async_sessionmaker(pooled, expire_on_commit=False)
    await pooled.dispose()


# --- frozen clock ------------------------------------------------------------------------------------------------


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> datetime:
        self.now = now
        return now

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
    """Stands in for ``aiogram.Bot``: records calls; queued exceptions are raised by the next calls of a method."""

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
    def sent(self) -> list[dict[str, Any]]:
        return self.calls_of("send_message")

    @property
    def texts(self) -> list[str]:
        return [kw["text"] for kw in self.sent]

    @property
    def reactions(self) -> list[tuple[int, str]]:
        return [(kw["message_id"], kw["reaction"][0].emoji) for kw in self.calls_of("set_message_reaction")]

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
        return io.BytesIO(self.files.get(str(file), f"bytes-of-{file}".encode()))


class FakeSTT:
    def __init__(self, default: str = "голосовой транскрипт") -> None:
        self.calls: list[dict[str, Any]] = []
        self.results: list[str | BaseException] = []
        self.default = default

    async def transcribe(self, audio: bytes, *, mime_type: str | None = None, language: str | None = None) -> str:
        self.calls.append({"audio": audio, "mime_type": mime_type, "language": language})
        result = self.results.pop(0) if self.results else self.default
        if isinstance(result, BaseException):
            raise result
        return result


class FakeAI:
    """Scripted ``extract`` / ``answer_query`` / ``fetch_titles`` (patched into the processor module)."""

    def __init__(self) -> None:
        self.outputs: list[Extraction | ExtractOutput | BaseException] = []
        self.inputs: list[ExtractInput] = []
        self.answers: list[str | BaseException] = []
        self.answer_calls: list[dict[str, Any]] = []
        self.titles: dict[str, str] = {}
        self.title_calls: list[list[str]] = []

    def push(self, *outputs: Extraction | ExtractOutput | dict[str, Any] | BaseException) -> None:
        for out in outputs:
            self.outputs.append(Extraction.model_validate(out) if isinstance(out, dict) else out)

    async def extract(self, client: Any, settings: Any, inp: ExtractInput) -> ExtractOutput:
        self.inputs.append(inp)
        out = self.outputs.pop(0) if self.outputs else Extraction()
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, ExtractOutput):
            return out
        return ExtractOutput(extraction=out, model="fake-model", prompt_version="v1")

    async def answer_query(
        self, client: Any, settings: Any, question: str, rows: list[dict], now: datetime, prefs: Prefs
    ) -> str:
        self.answer_calls.append({"question": question, "rows": rows, "now": now})
        result = self.answers.pop(0) if self.answers else "Завтра <созвон> & спорт"
        if isinstance(result, BaseException):
            raise result
        return result

    async def fetch_titles(self, urls: list[str], **kwargs: Any) -> dict[str, str]:
        self.title_calls.append(list(urls))
        return {u: t for u, t in self.titles.items() if u in urls}


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def stt() -> FakeSTT:
    """The chutes Whisper (paid)."""
    return FakeSTT()


@pytest.fixture
def local_stt() -> FakeSTT:
    """The local Whisper (free); only used after ``with_local_stt()``."""
    return FakeSTT("локальный транскрипт")


@pytest.fixture
def with_local_stt(ctx: AppContext, stt: FakeSTT, local_stt: FakeSTT) -> Callable[[], None]:
    """Configure the local Whisper (as LOCAL_STT_URL does in production)."""

    def _wire() -> None:
        ctx.stt = RoutedSTT(stt, local_stt)  # type: ignore[arg-type]

    return _wire


@pytest.fixture(autouse=True)
def fake_ai(monkeypatch: pytest.MonkeyPatch) -> FakeAI:
    ai = FakeAI()
    monkeypatch.setattr(processor, "extract", ai.extract)
    monkeypatch.setattr(processor, "answer_query", ai.answer_query)
    monkeypatch.setattr(processor, "fetch_titles", ai.fetch_titles)
    return ai


@pytest.fixture(autouse=True)
def no_flood_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    from dumpbot.bot import sender

    async def _no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(sender, "_sleep", _no_sleep)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="123:TEST",
        chutes_api_key="test-key",
        owner_id=OWNER,
        heartbeat_file=str(tmp_path / "hb" / "heartbeat"),
        scheduler_interval_s=30,
    )


@pytest.fixture
def ctx(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, bot: FakeBot, stt: FakeSTT, frozen: FrozenClock
) -> AppContext:
    routed = RoutedSTT(stt)  # type: ignore[arg-type]  # no LOCAL_STT_URL: every mode is "chutes"
    return AppContext(settings=settings, sessionmaker=sessionmaker, bot=bot, ai=None, stt=routed)  # type: ignore[arg-type]


# --- DB helpers --------------------------------------------------------------------------------------------------


@pytest.fixture
def set_prefs(sessionmaker: async_sessionmaker[AsyncSession]) -> Callable[..., Awaitable[Prefs]]:
    """``await set_prefs(budget_usd=1.0, ...)`` -> stored owner prefs (defaults + overrides)."""

    async def _set(**changes: Any) -> Prefs:
        async with sessionmaker() as session:
            prefs = (await load_prefs(session, OWNER)).model_copy(update=changes)
            prefs = Prefs.model_validate(prefs.model_dump())
            await save_prefs(session, OWNER, prefs)
            await session.commit()
        return prefs

    return _set


@pytest.fixture
def add_raw(
    sessionmaker: async_sessionmaker[AsyncSession], frozen: FrozenClock
) -> Callable[..., Awaitable[RawMessage]]:
    """``await add_raw("купить молоко", content_type="text", ...)`` -> committed RawMessage."""
    message_ids = itertools.count(100)

    async def _add(text: str | None = "", **kw: Any) -> RawMessage:
        values: dict[str, Any] = {
            "chat_id": OWNER,
            "message_id": next(message_ids),
            "content_type": "text",
            "text": text,
            "urls": [],
            "tg_date": frozen.now,
            "status": "new",
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
    """``await db(select(Item).where(...))`` -> list of scalars (fresh session)."""

    async def _all(stmt: Any) -> list[Any]:
        async with sessionmaker() as session:
            return list((await session.scalars(stmt)).all())

    return _all


@pytest.fixture
def get(sessionmaker: async_sessionmaker[AsyncSession]) -> Callable[..., Awaitable[Any]]:
    async def _get(model: Any, ident: Any) -> Any:
        async with sessionmaker() as session:
            return await session.get(model, ident)

    return _get


@pytest.fixture
def make_items(sessionmaker: async_sessionmaker[AsyncSession]) -> Callable[..., Awaitable[list[CreatedItem]]]:
    """``await make_items({"kind": "task", "title": "…", "when": {...}}, now=…)`` -> committed CreatedItems."""

    async def _make(*specs: dict[str, Any], now: datetime, prefs: Prefs | None = None) -> list[CreatedItem]:
        async with sessionmaker() as session:
            created = await items.create_items(
                session,
                raw=None,
                extraction=Extraction(items=[ExtractedItem.model_validate(s) for s in specs]),
                source="text",
                source_text=None,
                source_meta=None,
                urls=[],
                prefs=prefs or Prefs(),
                now=now,
                model="test",
                prompt_version="v1",
            )
            await session.commit()
        return created

    return _make


@pytest.fixture
def items_of_raw(db: Callable[..., Awaitable[list[Any]]]) -> Callable[..., Awaitable[list[Item]]]:
    async def _items(raw_id: int, *, include_deleted: bool = False) -> list[Item]:
        stmt = select(Item).where(Item.raw_message_id == raw_id)
        if not include_deleted:
            stmt = stmt.where(Item.status != "deleted")
        return await db(stmt.order_by(Item.id))

    return _items


@pytest.fixture
def jobs_all(db: Callable[..., Awaitable[list[Any]]]) -> Callable[[], Awaitable[list[Job]]]:
    async def _jobs() -> list[Job]:
        return await db(select(Job).order_by(Job.id))

    return _jobs


@pytest.fixture
def bot_messages(db: Callable[..., Awaitable[list[Any]]]) -> Callable[[], Awaitable[list[BotMessage]]]:
    async def _msgs() -> list[BotMessage]:
        return await db(select(BotMessage).order_by(BotMessage.message_id))

    return _msgs


@pytest.fixture
def reminders_of(db: Callable[..., Awaitable[list[Any]]]) -> Callable[..., Awaitable[list[Reminder]]]:
    async def _rem(occurrence_id: int) -> list[Reminder]:
        return await db(select(Reminder).where(Reminder.occurrence_id == occurrence_id).order_by(Reminder.fire_at))

    return _rem


@pytest.fixture
def occurrences_of(db: Callable[..., Awaitable[list[Any]]]) -> Callable[..., Awaitable[list[Occurrence]]]:
    async def _occ(item_id: int) -> list[Occurrence]:
        return await db(select(Occurrence).where(Occurrence.item_id == item_id).order_by(Occurrence.due_at))

    return _occ


# --- aiogram objects ---------------------------------------------------------------------------------------------


@pytest.fixture
def make_message() -> Callable[..., Message]:
    """``make_message(text="…", entities=[…])`` -> aiogram Message in the owner's private chat."""

    def _make(message_id: int = 1, **kw: Any) -> Message:
        values: dict[str, Any] = {
            "message_id": message_id,
            "date": T0,
            "chat": Chat(id=OWNER, type="private"),
            "from_user": User(id=OWNER, is_bot=False, first_name="Дима"),
        }
        values.update(kw)
        return Message(**values)

    return _make


def utf16_offset(text: str, sub: str) -> tuple[int, int]:
    """(offset, length) of ``sub`` in ``text`` in UTF-16 code units, as Telegram reports entities."""
    idx = text.index(sub)
    return len(text[:idx].encode("utf-16-le")) // 2, len(sub.encode("utf-16-le")) // 2


@pytest.fixture
def u16() -> Callable[[str, str], tuple[int, int]]:
    return utf16_offset
