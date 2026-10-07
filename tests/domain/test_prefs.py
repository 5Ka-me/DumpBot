"""Prefs bounds and loading of stored prefs."""

from __future__ import annotations

import logging
from datetime import time

import pytest
from pydantic import ValidationError

from dumpbot.domain.prefs import MISSED_GRACE_MAX, Prefs


def test_missed_grace_minutes_is_bounded() -> None:
    assert Prefs(missed_grace_minutes=MISSED_GRACE_MAX).missed_grace_minutes == MISSED_GRACE_MAX
    for value in (0, MISSED_GRACE_MAX + 1, 10**15):
        with pytest.raises(ValidationError):
            Prefs(missed_grace_minutes=value)


def test_stored_prefs_keep_valid_values_and_drop_invalid_ones(caplog: pytest.LogCaptureFixture) -> None:
    """A value saved before its bound existed (10**15 minutes broke every reminder) falls back to the default instead
    of making every load of the prefs fail."""
    stored = {"tz": "Europe/Warsaw", "lead_minutes": 15, "missed_grace_minutes": 10**15, "slot_lunch": "12:30:00"}
    with caplog.at_level(logging.WARNING):
        prefs = Prefs.from_stored(stored)
    assert prefs.missed_grace_minutes == 360 and prefs.lead_minutes == 15 and prefs.slot_lunch == time(12, 30)
    assert "missed_grace_minutes" in caplog.text


def test_stored_prefs_without_problems_load_as_they_are() -> None:
    assert Prefs.from_stored(None) == Prefs()
    assert Prefs.from_stored({"soon_days": 3, "unknown": 1}).soon_days == 3
