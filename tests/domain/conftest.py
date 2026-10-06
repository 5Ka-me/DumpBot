"""Fixtures for the pure domain tests (no DB). ``prefs`` (Europe/Warsaw defaults) comes from tests/conftest.py."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

WARSAW = ZoneInfo("Europe/Warsaw")


@pytest.fixture
def tz() -> ZoneInfo:
    return WARSAW


@pytest.fixture
def now() -> datetime:
    """Tue 2026-10-06 14:05 Europe/Warsaw (CEST, UTC+2) — 19 days before the autumn DST switch."""
    return datetime(2026, 10, 6, 14, 5, tzinfo=WARSAW)
