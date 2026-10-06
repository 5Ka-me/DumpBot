"""Fixtures for the presentation-layer tests (views, callback data, sender).

Test files cannot import each other (``--import-mode=importlib``), so fakes and helpers are exposed as fixtures:
``fake_bot`` (records calls, can be told to fail), ``make_ctx``, ORM object factories (transient, no DB),
``check_view`` (HTML + keyboard validator) and ``retry_after`` / ``bad_request`` exception factories.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup

from dumpbot.bot.cbdata import ALL_CALLBACKS
from dumpbot.config import Settings
from dumpbot.context import AppContext
from dumpbot.db.models import Item, Occurrence, Reminder
from dumpbot.dto import Row

WARSAW = ZoneInfo("Europe/Warsaw")
OWNER = 777000111
BIG_ID = 10**12

# --- fake Telegram bot -------------------------------------------------------------------------------------------


class FakeBot:
    """Stands in for ``aiogram.Bot``: records keyword calls; queued exceptions are raised by the next calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, list[BaseException]] = defaultdict(list)
        self.next_id = 500

    def fail(self, method: str, *errors: BaseException) -> None:
        self.errors[method].extend(errors)

    def calls_of(self, method: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if name == method]

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


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def make_ctx(fake_bot: FakeBot) -> Callable[..., AppContext]:
    def build(sessionmaker: Any = None) -> AppContext:
        settings = Settings(_env_file=None, bot_token="123:TEST", chutes_api_key="test-key", owner_id=OWNER)
        return AppContext(settings=settings, sessionmaker=sessionmaker, bot=fake_bot, ai=None, stt=None)  # type: ignore[arg-type]

    return build


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the sender's flood-control sleep; returns the list of requested waits."""
    from dumpbot.bot import sender

    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(sender, "_sleep", fake_sleep)
    return waits


@pytest.fixture
def retry_after() -> Callable[[int], TelegramRetryAfter]:
    def build(seconds: int = 1) -> TelegramRetryAfter:
        method = SendMessage(chat_id=OWNER, text="x")
        return TelegramRetryAfter(method=method, message="Too Many Requests: retry after", retry_after=seconds)

    return build


@pytest.fixture
def bad_request() -> Callable[[str], TelegramBadRequest]:
    def build(message: str) -> TelegramBadRequest:
        return TelegramBadRequest(method=EditMessageText(text="x", chat_id=OWNER, message_id=1), message=message)

    return build


# --- time and ORM object factories (transient objects, nothing touches the DB) -----------------------------------


@pytest.fixture
def now() -> datetime:
    """Tue 2026-10-06 14:05 Europe/Warsaw (UTC+2), as aware UTC."""
    return datetime(2026, 10, 6, 14, 5, tzinfo=WARSAW).astimezone(UTC)


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=WARSAW).astimezone(UTC)


@pytest.fixture
def at() -> Callable[..., datetime]:
    """``at(2026, 10, 6, 15, 0)`` -> aware UTC datetime for that Warsaw wall time."""
    return local


@pytest.fixture
def make_item() -> Callable[..., Item]:
    def build(**kw: Any) -> Item:
        values: dict[str, Any] = {
            "id": 1,
            "kind": "task",
            "title": "Купить наушники",
            "details": None,
            "tags": [],
            "urls": [],
            "source": None,
            "source_kind": "text",
            "status": "open",
            "rrule": None,
            "rec_active": False,
            "needs_confirm": False,
            "lead_minutes": None,
        }
        values.update(kw)
        return Item(**values)

    return build


@pytest.fixture
def make_occ() -> Callable[..., Occurrence]:
    def build(due_at: datetime, **kw: Any) -> Occurrence:
        values: dict[str, Any] = {"id": 10, "item_id": 1, "due_at": due_at, "precision": "exact", "status": "open"}
        values.update(kw)
        return Occurrence(**values)

    return build


@pytest.fixture
def make_reminder() -> Callable[..., Reminder]:
    def build(occ: Occurrence, lead: int = 5, **kw: Any) -> Reminder:
        values: dict[str, Any] = {
            "id": 100 + lead,
            "occurrence_id": occ.id,
            "fire_at": occ.due_at - timedelta(minutes=lead),
            "lead_minutes": lead,
            "auto": False,
            "status": "pending",
        }
        values.update(kw)
        return Reminder(**values)

    return build


@pytest.fixture
def make_row(now: datetime) -> Callable[..., Row]:
    def build(**kw: Any) -> Row:
        values: dict[str, Any] = {
            "item_id": 1,
            "kind": "task",
            "title": "Задача",
            "tags": [],
            "status": "open",
            "due_at": None,
            "occurrence_id": None,
            "occ_status": None,
            "rrule": None,
            "created_at": now,
            "snippet": None,
        }
        values.update(kw)
        return Row(**values)

    return build


# --- validators --------------------------------------------------------------------------------------------------

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "blockquote"}
_TOKEN_RE = re.compile(
    r"<(?P<close>/?)(?P<tag>[a-z-]+)(?P<attrs>\s[^<>]*)?>"
    r"|&(?:lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);"
    r"|(?P<stray>[<>&])"
)


def html_problems(text: str) -> list[str]:
    """Problems that would make Telegram reject the text in HTML parse mode (or exceed the length limit)."""
    problems: list[str] = []
    stack: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        if m.group("stray"):
            context = text[max(0, m.start() - 20) : m.end() + 20]
            problems.append(f"unescaped {m.group('stray')!r} at {m.start()}: {context!r}")
        elif m.group("tag"):
            tag = m.group("tag")
            if tag not in ALLOWED_TAGS:
                problems.append(f"tag not allowed: {tag}")
            elif m.group("close"):
                if not stack or stack[-1] != tag:
                    problems.append(f"unbalanced </{tag}> (open: {stack})")
                else:
                    stack.pop()
            else:
                stack.append(tag)
    if stack:
        problems.append(f"unclosed tags: {stack}")
    if len(text.encode("utf-16-le")) // 2 > 4096:
        problems.append("longer than 4096 UTF-16 units")
    if not text.strip():
        problems.append("empty text")
    return problems


def callback_problems(markup: InlineKeyboardMarkup | None) -> list[str]:
    problems: list[str] = []
    if markup is None:
        return problems
    by_prefix = {cls.__prefix__: cls for cls in ALL_CALLBACKS}
    for row in markup.inline_keyboard:
        if not row:
            problems.append("empty keyboard row")
        for b in row:
            data = b.callback_data
            if not b.text or not data:
                problems.append(f"button without text/callback: {b!r}")
                continue
            if len(data.encode()) > 64:
                problems.append(f"callback_data longer than 64 bytes: {data!r}")
            cls = by_prefix.get(data.split(":", 1)[0])
            if cls is None:
                problems.append(f"unknown callback prefix: {data!r}")
                continue
            try:
                if cls.unpack(data).pack() != data:
                    problems.append(f"callback does not round-trip: {data!r}")
            except (TypeError, ValueError) as e:
                problems.append(f"callback does not unpack: {data!r}: {e}")
    return problems


@pytest.fixture
def check_view() -> Callable[[Any], tuple[str, InlineKeyboardMarkup | None]]:
    """Assert a view (``(text, markup)`` or plain text) is valid HTML within limits with valid callbacks."""

    def check(view: Any) -> tuple[str, InlineKeyboardMarkup | None]:
        text, kb = view if isinstance(view, tuple) else (view, None)
        assert isinstance(text, str)
        assert kb is None or isinstance(kb, InlineKeyboardMarkup)
        problems = html_problems(text) + callback_problems(kb)
        assert not problems, problems
        return text, kb

    return check


@pytest.fixture
def buttons() -> Callable[[InlineKeyboardMarkup | None], list[tuple[str, str]]]:
    """Flatten a keyboard to ``[(text, callback_data), ...]``."""

    def flat(kb: InlineKeyboardMarkup | None) -> list[tuple[str, str]]:
        if kb is None:
            return []
        return [(b.text, b.callback_data or "") for row in kb.inline_keyboard for b in row]

    return flat
