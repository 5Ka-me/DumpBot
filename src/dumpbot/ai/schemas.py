"""Structured output the LLM must return (sent as ``response_format: json_schema``).

The LLM only *describes* time ("day" + "time" slot/HH:MM + offsets); exact datetimes are computed
deterministically in ``domain.timeresolve`` using the user's preferences and IANA timezone.

Validation is lenient on input (unknown keys ignored, tags normalized, over-long strings and lists clipped to their
``max_length``) but the generated JSON schema is strict (``additionalProperties: false``, ``maxLength`` /
``maxItems``) to steer constrained decoding. One long link title or an extra tag must never fail the whole message.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.fields import FieldInfo

from dumpbot.domain.kinds import Kind

HHMM = r"([01]\d|2[0-3]):[0-5]\d"
SLOT_RE = r"morning|lunch|afternoon|evening|after_work|night"
TIME_RE = rf"^({HHMM}|{SLOT_RE})$"
# +N days | +Nw | +Nmo | +Ny | MO..SU (nearest upcoming) | "next FR" (weekday of next week)
# | dN (N-th day of month, d-1 = last day) | MM-DD | YYYY-MM-DD | weekend
DAY_RE = (
    r"^(\+\d{1,3}(d|w|mo|y)?"
    r"|(next )?(MO|TU|WE|TH|FR|SA|SU)"
    r"|d(-1|[1-9]|[12]\d|3[01])"
    r"|(\d{4}-)?\d{2}-\d{2}"
    r"|weekend)$"
)
AT_RE = r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$"
# Fuzzy spans: calendar week / month (YYYY-MM: the code picks the year) / season, "на днях", "когда-нибудь".
PERIOD_RE = r"^(this_week|next_week|this_month|next_month|\d{4}-\d{2}|spring|summer|autumn|winter|soon|someday)$"
RRULE_RE = r"^FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(;[A-Z]+=[-+,0-9A-Z]+)*$"
RANGE_RE = (
    r"^(today|tomorrow|yesterday|this_week|next_week|last_week|this_month|last_month"
    r"|past:\d{1,3}|next:\d{1,3}"
    r"|\d{4}-\d{2}-\d{2}(\.\.\d{4}-\d{2}-\d{2})?)$"
)

MAX_LEAD_MINUTES = 43200  # 30 days
MAX_ITEMS = 20  # the prompt asks to merge long shopping lists into one item well before this
MAX_STEPS = 8  # steps of one checklist (docs/features/iteration-2.md §C2)
MAX_RELATED = 3  # related earlier items per item (§C1)


class _S(BaseModel):
    model_config = ConfigDict(extra="ignore", json_schema_extra={"additionalProperties": False})

    @model_validator(mode="before")
    @classmethod
    def _clip_overlong(cls, data: Any) -> Any:
        """Strings over a field's ``max_length`` end with "…", lists keep their first ``max_length`` elements."""
        if not isinstance(data, dict):
            return data
        clipped = dict(data)
        for name, info in cls.model_fields.items():
            value, limit = clipped.get(name), _max_length(info)
            if limit is None or not isinstance(value, str | list) or len(value) <= limit:
                continue
            clipped[name] = value[: limit - 1].rstrip() + "…" if isinstance(value, str) else value[:limit]
        return clipped


def _max_length(info: FieldInfo) -> int | None:
    """The ``max_length`` constraint of a string / list field, if any."""
    return next((m.max_length for m in info.metadata if isinstance(getattr(m, "max_length", None), int)), None)


def _norm_tags(v: Any) -> list[str]:
    if v is None:
        return []
    out: list[str] = []
    for t in v:
        t = re.sub(r"\s+", "_", str(t).strip().lstrip("#").lower())[:30]
        if t and t not in out:
            out.append(t)
    return out


class When(_S):
    """How the message describes the time. At most one of ``at`` / ``in_min`` / ``period`` / (``day`` and/or
    ``time``); when the model sends more, the first in that order wins."""

    text: str = Field("", max_length=80, description="verbatim time phrase from the message")
    day: str | None = Field(None, pattern=DAY_RE)
    time: str | None = Field(None, pattern=TIME_RE)
    in_min: int | None = Field(None, ge=1, le=525600, description="'через N минут/часов' as minutes")
    at: str | None = Field(None, pattern=AT_RE, description="local wall time, only if inexpressible")
    period: str | None = Field(None, pattern=PERIOD_RE, description="fuzzy span instead of a day")
    role: Literal["event", "remind", "deadline"] = "event"


class StepSpec(_S):
    """One step of a checklist, in order: ``ExtractedItem.steps`` and the answer of the "split" call."""

    title: str = Field(min_length=1, max_length=120)
    when: When | None = Field(None, description="only if the message gives this step a time")


class ExtractedItem(_S):
    kind: Kind
    title: str = Field(min_length=1, max_length=120)
    details: str | None = Field(None, max_length=600)
    tags: list[str] = Field(default_factory=list, max_length=6)
    when: When | None = None
    rrule: str | None = Field(None, pattern=RRULE_RE)
    lead: list[int] | None = Field(None, max_length=3, description="minutes before the event")
    conf: Literal["high", "med", "low"] = "high"
    related: list[int] = Field(
        default_factory=list, max_length=MAX_RELATED, description="ids from RELATED CANDIDATES really about it"
    )
    steps: list[StepSpec] = Field(default_factory=list, max_length=MAX_STEPS, description="checklist, only if asked")

    _tags = field_validator("tags", mode="before")(_norm_tags)

    @field_validator("related", mode="before")
    @classmethod
    def _related_ids(cls, v: Any) -> list[int]:
        """Positive integer ids, unique; anything else (a title, null) is dropped instead of failing the message."""
        out: list[int] = []
        for x in v or []:
            try:
                n = int(x)
            except (TypeError, ValueError):
                continue
            if n > 0 and n not in out and not isinstance(x, bool):
                out.append(n)
        return out

    @field_validator("lead")
    @classmethod
    def _lead_range(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return None
        v = sorted({int(x) for x in v if 0 <= int(x) <= MAX_LEAD_MINUTES}, reverse=True)
        return v or None


class Ask(_S):
    """A clarifying question with quick-reply options (e.g. "В 8 утра или вечера?")."""

    item: int = Field(0, ge=0, le=MAX_ITEMS - 1, description="index in items the question is about")
    q: str = Field(min_length=1, max_length=160)
    options: list[str] = Field(min_length=2, max_length=4)


class QuerySpec(_S):
    """A question about the user's own saved data, as filters."""

    text: str | None = Field(None, max_length=120, description="keywords for full-text search")
    kinds: list[Kind] = Field(default_factory=list)
    status: Literal["open", "done", "any"] = "any"
    date_field: Literal["due", "created"] = "due"
    range: str | None = Field(None, pattern=RANGE_RE)
    tags: list[str] = Field(default_factory=list, max_length=6)
    answer: Literal["list", "summary"] = "list"
    limit: int = Field(20, ge=1, le=50)

    _tags = field_validator("tags", mode="before")(_norm_tags)


class EditPatch(_S):
    """A change to an existing item (reply to a bot card, or "перенеси встречу с Олегом на 16")."""

    target: str | None = Field(None, max_length=120, description="which item, when not replying to it")
    action: Literal["update", "done", "undone", "delete", "no_reminder", "stop_recurrence", "split"] = "update"
    title: str | None = Field(None, max_length=120)
    kind: Kind | None = None
    tags_add: list[str] = Field(default_factory=list, max_length=6)
    tags_remove: list[str] = Field(default_factory=list, max_length=6)
    when: When | None = None
    rrule: str | None = Field(None, pattern=RRULE_RE)
    lead: list[int] | None = Field(None, max_length=3)

    _tags_add = field_validator("tags_add", mode="before")(_norm_tags)
    _tags_remove = field_validator("tags_remove", mode="before")(_norm_tags)

    @field_validator("lead")
    @classmethod
    def _lead_range(cls, v: list[int] | None) -> list[int] | None:
        return ExtractedItem._lead_range(v)


class SplitResult(_S):
    """Answer of the "split" call (🪄 Разбить на шаги, "разбей на шаги"): the steps of one task, in order."""

    steps: list[StepSpec] = Field(default_factory=list, max_length=MAX_STEPS)


class ImageInfo(_S):
    text: str | None = Field(None, max_length=4000, description="all readable text in the image, verbatim")
    description: str | None = Field(None, max_length=300, description="one short sentence, in Russian")


class Extraction(_S):
    intent: Literal["save", "query", "edit"] = "save"
    items: list[ExtractedItem] = Field(default_factory=list, max_length=MAX_ITEMS)
    query: QuerySpec | None = None
    edit: EditPatch | None = None
    image: ImageInfo | None = None
    ask: Ask | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Extraction:
        if self.intent == "query" and self.query is None:
            self.query = QuerySpec()
        if self.intent == "edit" and self.edit is None:
            raise ValueError("intent=edit requires 'edit'")
        if self.ask is not None and self.ask.item >= max(len(self.items), 1):
            self.ask = None
        return self


def extraction_json_schema() -> dict[str, Any]:
    """JSON schema for ``response_format={"type": "json_schema", ...}``."""
    return Extraction.model_json_schema()


def split_json_schema() -> dict[str, Any]:
    """JSON schema of the "split" call's answer."""
    return SplitResult.model_json_schema()
