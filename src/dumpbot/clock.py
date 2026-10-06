"""Single source of "now" so tests can freeze time by monkeypatching ``dumpbot.clock.utcnow``."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)
