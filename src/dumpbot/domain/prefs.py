"""User preferences, stored as JSONB in ``user_settings.prefs`` and edited from the in-bot settings menu.

Adding a field with a default needs no DB migration: old rows simply get the default on load.
"""

from __future__ import annotations

import logging
from datetime import time
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

SlotName = Literal["morning", "lunch", "afternoon", "evening", "after_work", "night"]
SttMode = Literal["auto", "local", "chutes"]
STT_MODES: tuple[str, ...] = get_args(SttMode)
SLOT_NAMES: tuple[str, ...] = ("morning", "lunch", "afternoon", "evening", "after_work", "night")
MISSED_GRACE_MAX = 1_000_000  # minutes (~2 years), as the Mini App's settings form allows

SLOT_LABELS: dict[str, str] = {
    "morning": "утро",
    "lunch": "обед",
    "afternoon": "день",
    "evening": "вечер",
    "after_work": "после работы",
    "night": "ночь",
}


class Prefs(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    tz: str = "Europe/Warsaw"

    # Named times of day the LLM may return instead of HH:MM ("в обед" -> lunch).
    slot_morning: time = time(9, 0)
    slot_lunch: time = time(13, 0)
    slot_afternoon: time = time(15, 0)
    slot_evening: time = time(19, 0)
    slot_after_work: time = time(18, 30)
    slot_night: time = time(22, 0)
    weekend_time: time = time(11, 0)  # "на выходных" = Saturday at this time
    bare_date_time: time = time(10, 0)  # a date without a time ("завтра")
    task_default_time: time = time(19, 0)  # actionable item without any time

    lead_minutes: int = Field(5, ge=0, le=10080)  # default "remind N minutes before the event"
    soon_days: int = Field(2, ge=1, le=14)  # "на днях" = in this many days (at task_default_time)

    quiet_enabled: bool = True
    quiet_start: time = time(23, 0)
    quiet_end: time = time(8, 0)
    # Before this local time "завтра" still means the calendar date that has just started.
    night_cutoff: time = time(4, 0)

    digest_morning_enabled: bool = True
    digest_morning_time: time = time(9, 0)
    digest_evening_enabled: bool = True
    digest_evening_time: time = time(21, 0)

    budget_usd: float = Field(5.0, ge=0)
    stt_language: Literal["ru", "en"] | None = "ru"  # None = auto-detect (unreliable on short clips: Russian
    # speech came back as English gibberish), so Russian by default
    # Speech-to-text: auto = chutes, the local Whisper when chutes fails; local = the reverse; chutes = chutes only.
    # Without LOCAL_STT_URL every mode works as "chutes".
    stt_mode: SttMode = "auto"

    # Older due reminders are marked missed, not sent. Bounded: the scheduler adds it to every due time as a timedelta.
    missed_grace_minutes: int = Field(360, ge=1, le=MISSED_GRACE_MAX)

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone: {v}") from e
        return v

    @classmethod
    def from_stored(cls, data: dict[str, Any] | None) -> Prefs:
        """Prefs from their stored JSON; a value that no longer validates (saved before a bound existed) falls back to
        the field's default instead of breaking every load."""
        values = dict(data or {})
        try:
            return cls.model_validate(values)
        except ValidationError as e:
            bad = {str(err["loc"][0]) for err in e.errors() if err.get("loc")} & values.keys()
            if not bad:
                raise
        log.warning("stored prefs: invalid %s, using the default", ", ".join(sorted(bad)))
        return cls.model_validate({name: value for name, value in values.items() if name not in bad})

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)  # ZoneInfo caches instances itself

    def slot(self, name: str) -> time:
        """Clock time for a slot name (see SLOT_NAMES). Raises KeyError for unknown names."""
        if name not in SLOT_NAMES:
            raise KeyError(name)
        return getattr(self, f"slot_{name}")
