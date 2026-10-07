"""Outgoing messages are silent inside quiet hours unless the caller decided (bot.quiet_requests)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.methods import EditMessageText, SendMessage

from dumpbot import clock
from dumpbot.bot.quiet_requests import QuietHoursRequests
from dumpbot.domain.prefs import Prefs


class _Ctx:
    def __init__(self, prefs: Prefs | None = None, *, broken: bool = False) -> None:
        self._prefs = prefs or Prefs(tz="Europe/Warsaw")
        self._broken = broken

    @asynccontextmanager
    async def _session(self):
        if self._broken:
            raise ConnectionError("db down")
        yield object()

    def sessionmaker(self):
        return self._session()

    async def prefs(self, session: Any) -> Prefs:
        return self._prefs


async def _send(ctx: _Ctx, method: Any, now: datetime, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(clock, "utcnow", lambda: now)
    seen: list[Any] = []

    async def make_request(bot: Any, m: Any) -> str:
        seen.append(m)
        return "ok"

    assert await QuietHoursRequests(ctx)(make_request, None, method) == "ok"  # type: ignore[arg-type]
    return seen[0]


NIGHT = datetime(2026, 10, 6, 22, 50, tzinfo=UTC)  # 00:50 in Warsaw: inside the default 23:00-08:00
DAY = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


async def test_message_at_night_is_silent(monkeypatch):
    m = await _send(_Ctx(), SendMessage(chat_id=1, text="x"), NIGHT, monkeypatch)
    assert m.disable_notification is True


async def test_message_during_the_day_untouched(monkeypatch):
    m = await _send(_Ctx(), SendMessage(chat_id=1, text="x"), DAY, monkeypatch)
    assert m.disable_notification is None


async def test_explicit_choice_wins(monkeypatch):
    m = await _send(_Ctx(), SendMessage(chat_id=1, text="x", disable_notification=False), NIGHT, monkeypatch)
    assert m.disable_notification is False


async def test_quiet_hours_off(monkeypatch):
    m = await _send(_Ctx(Prefs(quiet_enabled=False)), SendMessage(chat_id=1, text="x"), NIGHT, monkeypatch)
    assert m.disable_notification is None


async def test_methods_without_notifications_pass_through(monkeypatch):
    m = await _send(_Ctx(), EditMessageText(chat_id=1, message_id=2, text="x"), NIGHT, monkeypatch)
    assert not hasattr(m, "disable_notification") or m.disable_notification is None


async def test_db_error_does_not_block_sending(monkeypatch):
    m = await _send(_Ctx(broken=True), SendMessage(chat_id=1, text="x"), NIGHT, monkeypatch)
    assert m.disable_notification is None


def test_voice_language_defaults_to_russian():
    assert Prefs().stt_language == "ru"
