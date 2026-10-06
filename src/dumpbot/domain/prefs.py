"""User preferences, stored as JSONB in ``user_settings.prefs`` and edited from the in-bot settings menu.

Adding a field with a default needs no DB migration: old rows simply get the default on load.
"""

from __future__ import annotations

from datetime import time
from typing import Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

SlotName = Literal["morning", "lunch", "afternoon", "evening", "after_work", "night"]
SttMode = Literal["auto", "local", "chutes"]
STT_MODES: tuple[str, ...] = get_args(SttMode)
SLOT_NAMES: tuple[str, ...] = ("morning", "lunch", "afternoon", "evening", "after_work", "night")

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
    stt_language: Literal["ru", "en"] | None = None  # None = auto-detect
    # Speech-to-text: auto = chutes, the local Whisper when chutes fails; local = the reverse; chutes = chutes only.
    # Without LOCAL_STT_URL every mode works as "chutes".
    stt_mode: SttMode = "auto"

    missed_grace_minutes: int = Field(360, ge=1)  # older due reminders are marked missed, not sent

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone: {v}") from e
        return v

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)  # ZoneInfo caches instances itself

    def slot(self, name: str) -> time:
        """Clock time for a slot name (see SLOT_NAMES). Raises KeyError for unknown names."""
        if name not in SLOT_NAMES:
            raise KeyError(name)
        return getattr(self, f"slot_{name}")
