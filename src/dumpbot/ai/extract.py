"""Message -> ``Extraction`` (structured output) and short answers to questions about saved data.

Fallback ladder of ``extract`` (architecture §4.7):
primary model -> on invalid output one retry with the validation error -> fallback model (one attempt).
Output cut at ``max_tokens`` (``finish_reason == "length"``) is first asked again once with ``LONG_MAX_TOKENS``.
``AIUnavailable`` or ``AIModelNotFound`` (404) from the primary skips straight to the fallback; if that is unavailable
too -> ``AIUnavailable``. A 404 from the fallback never turns a paid primary answer into a config error: the primary's
``AIUnavailable`` is raised if there was one, ``AIModelNotFound`` only when both models are missing, else
``AIBadResponse``. Images route to vision-capable models (``llm_model`` / ``vision_fallback_model``).

``split_steps`` (docs/features/iteration-2.md §C2) breaks one task into checklist steps by the same ladder.

After validation the code drops an AM/PM question about a time the message already pins ("в 17" is never 05:00):
``_drop_pointless_ask``.
"""

from __future__ import annotations

import base64
import functools
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from dumpbot.ai import prompts
from dumpbot.ai.client import AIBadResponse, AIModelNotFound, AIUnavailable, ChutesClient, parse_json_object
from dumpbot.ai.schemas import (
    MAX_STEPS,
    EditPatch,
    ExtractedItem,
    Extraction,
    QuerySpec,
    SplitResult,
    StepSpec,
    When,
    extraction_json_schema,
    split_json_schema,
)
from dumpbot.config import Settings
from dumpbot.domain.kinds import SourceKind
from dumpbot.domain.prefs import Prefs

__all__ = [
    "PROMPT_VERSION",
    "ExtractInput",
    "ExtractOutput",
    "SplitInput",
    "SplitOutput",
    "answer_query",
    "extract",
    "split_steps",
]

log = logging.getLogger(__name__)

PROMPT_VERSION = prompts.PROMPT_VERSION
MAX_IMAGES = 4
MAX_ANSWER_ROWS = 50
MAX_ANSWER_TOKENS = 600
# One more try when the JSON was cut at max_tokens (a long OCR text): only generated tokens are billed.
LONG_MAX_TOKENS = 4096
_MD_RE = re.compile(r"\*\*|__|```[a-zA-Z0-9_-]*")


@dataclass
class ExtractInput:
    text: str  # text / caption / transcript (may be empty for a picture)
    source: SourceKind
    now: datetime  # aware; when the message was sent
    prefs: Prefs
    images: list[tuple[bytes, str]] = field(default_factory=list)  # (data, mime)
    forward_from: str | None = None  # "канал «Хабр»"
    link_titles: dict[str, str] = field(default_factory=dict)
    force_query: bool = False  # the message started with "?"
    reply_item: dict | None = None  # snapshot of the item whose card was replied to -> intent=edit
    clarification: str | None = None  # the chosen option of a previous Ask
    # Earlier items with similar words, {id, kind, title, date} (the "RELATED CANDIDATES" line, §C1).
    related: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ExtractOutput:
    extraction: Extraction
    model: str
    prompt_version: str
    missing_model: str | None = None  # the primary model answered 404: ``model`` is the fallback


@dataclass
class SplitInput:
    task: dict[str, Any]  # the item as the prompt sees it (``items.item_snapshot`` without its id)
    now: datetime  # aware; for CAL and the steps' times
    prefs: Prefs
    note: str | None = None  # the owner's words ("разбей на шаги", "на 3 шага, начни с документов")


@dataclass
class SplitOutput:
    steps: list[StepSpec]
    model: str
    prompt_version: str
    missing_model: str | None = None


@functools.cache
def _schema() -> dict[str, Any]:
    return extraction_json_schema()


@functools.cache
def _split_schema() -> dict[str, Any]:
    return split_json_schema()


async def extract(client: ChutesClient, settings: Settings, inp: ExtractInput) -> ExtractOutput:
    """Run the fallback ladder and apply the ``force_query`` / ``reply_item`` overrides to the result."""
    has_images = bool(inp.images)
    fallback = settings.vision_fallback_model if has_images else settings.llm_fallback_model
    ext, model, missing = await _ladder(
        client,
        settings,
        _messages(inp),
        purpose="vision" if has_images else "extract",
        fallback=fallback,
        out_cls=Extraction,
        schema=_schema(),
        schema_name="extraction",
    )
    return _output(ext, inp, model, missing_model=missing)


async def split_steps(client: ChutesClient, settings: Settings, inp: SplitInput) -> SplitOutput:
    """The "split" call (🪄 Разбить на шаги, "разбей на шаги"): the steps of one task, by the same fallback ladder
    as ``extract`` (text models, ``ai_usage.purpose = "split"``). An empty list means "nothing sensible to split"."""
    messages = [
        {"role": "system", "content": prompts.SPLIT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": prompts.build_split_text(task=inp.task, now=inp.now, prefs=inp.prefs, note=inp.note),
        },
    ]
    result, model, missing = await _ladder(
        client,
        settings,
        messages,
        purpose="split",
        fallback=settings.llm_fallback_model,
        out_cls=SplitResult,
        schema=_split_schema(),
        schema_name="steps",
    )
    steps = [s for s in result.steps if s.title.strip()][:MAX_STEPS]
    log.info("split with %s: %d step(s)", model, len(steps))
    return SplitOutput(steps=steps, model=model, prompt_version=PROMPT_VERSION, missing_model=missing)


async def _ladder[M: BaseModel](
    client: ChutesClient,
    settings: Settings,
    messages: list[dict],
    *,
    purpose: str,
    fallback: str | None,
    out_cls: type[M],
    schema: dict[str, Any],
    schema_name: str,
) -> tuple[M, str, str | None]:
    """Primary model (one repair retry) -> fallback model; returns (result, model, missing primary model or None).

    ``AIUnavailable`` or ``AIModelNotFound`` (404) of the primary skips to the fallback. A 404 of the fallback never
    turns a paid primary answer into a config error: the primary's ``AIUnavailable`` is raised if there was one,
    ``AIModelNotFound`` only when both models are missing, else ``AIBadResponse``.
    """
    primary = settings.llm_model
    call = functools.partial(_attempt, client, settings, out_cls=out_cls, schema=schema, schema_name=schema_name)
    unavailable: AIUnavailable | None = None
    missing: AIModelNotFound | None = None
    try:
        out = await call(primary, messages, purpose, retries=1)
        if out is not None:
            return out, primary, None
    except AIUnavailable as e:
        unavailable = e
        log.warning("primary model %s unavailable (%s), trying %s", primary, e, fallback)
    except AIModelNotFound as e:
        missing = e
        log.error("primary model %s not found (%s), trying %s", primary, e, fallback)

    if fallback and fallback != primary:
        try:
            out = await call(fallback, messages, purpose, retries=0)
        except AIModelNotFound as e:
            log.error("fallback model %s not found (%s)", fallback, e)
            if unavailable is not None:
                raise unavailable from e  # the primary may come back: let the queue retry
            if missing is not None:
                raise missing from e  # both models are missing: the owner has to fix the model IDs
            # The primary answered (and was paid for) with unusable output: a retry would pay for the same again.
            raise AIBadResponse(f"no valid {schema_name} from the primary model, the fallback model is missing") from e
        if out is not None:
            return out, fallback, primary if missing is not None else None
    elif missing is not None:
        raise missing  # no fallback model configured: the owner has to fix the model ID
    if unavailable is not None:
        raise unavailable  # the primary may come back: let the queue retry
    raise AIBadResponse(f"no valid {schema_name} from the primary and fallback models")


async def _attempt[M: BaseModel](
    client: ChutesClient,
    settings: Settings,
    model: str,
    messages: list[dict],
    purpose: str,
    *,
    retries: int,
    out_cls: type[M],
    schema: dict[str, Any],
    schema_name: str,
) -> M | None:
    """Call ``model`` and validate; on invalid output retry up to ``retries`` times. None = unusable output.

    Output cut at ``max_tokens`` is not "invalid": the same request is sent once more with ``LONG_MAX_TOKENS``
    (not counted as a retry), since asking to fix the JSON would be cut at the same place.
    """
    current = messages
    max_tokens = settings.llm_max_tokens
    attempt = 0
    while True:
        try:
            res = await client.chat(
                purpose=purpose,
                model=model,
                messages=current,
                json_schema=schema,
                schema_name=schema_name,
                max_tokens=max_tokens,
            )
        except AIBadResponse as e:
            log.warning("model %s gave an unusable response: %s", model, e)
            return None
        try:
            return out_cls.model_validate(parse_json_object(res.content))
        except (ValueError, TypeError) as e:
            # JSONDecodeError and pydantic ValidationError are ValueErrors; pydantic passes through a TypeError of a
            # validator that met a value of an unexpected type: invalid output too, not a failed message.
            if res.finish_reason == "length" and max_tokens < LONG_MAX_TOKENS:
                log.warning(
                    "model %s output was cut at %d tokens, asking again with %d", model, max_tokens, LONG_MAX_TOKENS
                )
                max_tokens = LONG_MAX_TOKENS
                continue
            attempt += 1
            summary = _error_summary(e)
            log.warning("model %s returned an invalid %s (attempt %d): %s", model, schema_name, attempt, summary)
            if attempt > retries:
                return None
            current = [
                *messages,
                {"role": "assistant", "content": res.content[:4000] or "(empty)"},
                {
                    "role": "user",
                    "content": f"Your JSON is invalid: {summary}. Reply with ONE corrected JSON object only.",
                },
            ]


def _error_summary(e: ValueError | TypeError) -> str:
    """Short, value-free description of what failed (no message text ends up in logs)."""
    if isinstance(e, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in err['loc']) or 'root'}: {err['msg']}"
            for err in e.errors(include_url=False, include_input=False)[:6]
        ]
        return "; ".join(parts)
    return str(e)[:300]


def _messages(inp: ExtractInput) -> list[dict]:
    user_text = prompts.build_user_text(
        text=inp.text,
        source=inp.source,
        now=inp.now,
        prefs=inp.prefs,
        forward_from=inp.forward_from,
        link_titles=inp.link_titles,
        reply_item=inp.reply_item,
        clarification=inp.clarification,
        force_query=inp.force_query,
        related=inp.related,
    )
    content: str | list[dict] = user_text
    if inp.images:
        content = [{"type": "text", "text": user_text}, *(_image_part(d, m) for d, m in inp.images[:MAX_IMAGES])]
    return [{"role": "system", "content": prompts.SYSTEM_PROMPT}, {"role": "user", "content": content}]


def _image_part(data: bytes, mime: str) -> dict:
    url = f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(data).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": url}}


def _output(ext: Extraction, inp: ExtractInput, model: str, *, missing_model: str | None = None) -> ExtractOutput:
    ext = _drop_pointless_ask(_apply_overrides(ext, inp))
    log.info("extracted with %s: intent=%s items=%d", model, ext.intent, len(ext.items))
    return ExtractOutput(extraction=ext, model=model, prompt_version=PROMPT_VERSION, missing_model=missing_model)


def _apply_overrides(ext: Extraction, inp: ExtractInput) -> Extraction:
    """``force_query`` -> intent query; ``reply_item`` -> intent edit with ``edit.target=None`` (the item is known)."""
    if inp.force_query:
        if ext.intent == "query":
            return ext
        # No QuerySpec from the model: an unfiltered query answered in prose is the safest guess.
        return Extraction(intent="query", query=ext.query or QuerySpec(answer="summary"), image=ext.image)
    if inp.reply_item is not None:
        edit = ext.edit or _edit_from_items(ext)
        return Extraction(intent="edit", edit=edit.model_copy(update={"target": None}), image=ext.image)
    return ext


def _edit_from_items(ext: Extraction) -> EditPatch:
    """The model saved a new item instead of editing: reuse its time / repetition / lead as the change (its steps
    as "split": the item replied to is the one to break down)."""
    if not ext.items:
        return EditPatch()
    item = ext.items[0]
    if item.steps:
        return EditPatch(action="split")
    return EditPatch(when=item.when, rrule=item.rrule, lead=item.lead)


def _drop_pointless_ask(ext: Extraction) -> Extraction:
    """No "morning or evening?" question about a time the owner already pinned: an AM/PM question (every option is a
    clock time, two of them 12 hours apart: "17:00" / "05:00") is dropped when the asked item's time phrase names
    that hour one way only — a 24-hour hour 13..23 or 0 ("в 17", "17:30", "в 00:15", "в семнадцать"), or one with
    утра / вечера / am / pm — and no bare hour on that dial position: "в 8", "в 9 или в 21", "до 20 в 8" stay
    questions. Without a time phrase the question stays (``when.time`` is then only the model's guess). An item whose
    time has the pinned hour keeps it ("low" confidence becomes "med": no "проверь время" hint); a time of the other
    half of the day (or none) becomes the pinned hour, confidence as it was."""
    ask = ext.ask
    if ask is None or ask.item >= len(ext.items):
        return ext
    options = [_option_time(o) for o in ask.options]
    times = [o for o in options if o is not None]
    if len(times) != len(options):
        return ext
    asked = _am_pm_hours([h for h, _ in times])
    item = ext.items[ask.item]
    if len(asked) != 1 or item.when is None:
        return ext
    hour = _pinned_hour(item.when.text, next(iter(asked)))
    update = None if hour is None else _pinned_update(item, item.when, hour, times)
    if update is None:
        return ext
    log.info("dropped an AM/PM question about an unambiguous time (item %d)", ask.item)
    items = list(ext.items)
    items[ask.item] = item.model_copy(update=update)
    return ext.model_copy(update={"ask": None, "items": items})


_DAYPART = r"(утра|ночи|дня|вечера|am|pm)"
# An option of the question as a clock time: "17:00", "05:00", "5", "в 8", "8 вечера", "20 ч".
_OPTION_RE = re.compile(rf"(?:в|at)?\s*(\d{{1,2}})(?:[:.]([0-5]\d))?\s*(?:ч|h|час|часа|часов)?\s*{_DAYPART}?", re.I)
_PM_PARTS = frozenset({"дня", "вечера", "pm"})
# Hours as written in a time phrase: "17:00", "в 17", "к 21", "17 часов"; not a day of month ("13 октября", "20 окт",
# "к 15-му", "к 20му", "в 17.10") or another unit ("в 15 минутах", "до 3 дней"; "в 3 дня" is 15:00).
_CLOCK_RE = re.compile(r"(?<![\d:.])(\d{1,2}):[0-5]\d(?!\d)")
_AT_HOUR_RE = re.compile(
    r"\b(?:в|к|до|после|с|около|at|by|around)\s+(\d{1,2})(?!\d|[:.,]\d|\s*[-–])"
    r"(?!\s*(?:(?:го|е|й|ое|ого|м|му|ом)\b|числ|янв|фев|мар|апр|ма[йяе]|июн|июл|авг|сен|окт|ноя|дек|мин|сек"
    r"|дн(?!я\b)|день|недел|месяц|год|лет|раз|%))",
    re.I,
)
_HOURS_RE = re.compile(r"(?<![\d:.])(\d{1,2})\s*(?:ч\b|час\w*)", re.I)
# Spoken hours (a voice transcript) in hour position, after a preposition: "в семнадцать", "к двадцати двум",
# "в восемь". Not minutes ("в восемь двадцать": 8:20) or a date ("двадцать пятого", "к двадцать первому").
_WORD_HOUR_RE = re.compile(r"\b(?:в|во|к|до|после|около|с|со)\s+([а-яё]+)(?=(?:\s+([а-яё]+))?)", re.I)
_WORD_HOURS = {
    word: hour
    for hour, words in enumerate(
        (
            "",
            "час часу",
            "два двух двум",
            "три трех трем",
            "четыре четырех четырем",
            "пять пяти",
            "шесть шести",
            "семь семи",
            "восемь восьми",
            "девять девяти",
            "десять десяти",
            "одиннадцать одиннадцати",
            "двенадцать двенадцати",
            "тринадцать тринадцати",
            "четырнадцать четырнадцати",
            "пятнадцать пятнадцати",
            "шестнадцать шестнадцати",
            "семнадцать семнадцати",
            "восемнадцать восемнадцати",
            "девятнадцать девятнадцати",
            "двадцать двадцати",
        )
    )
    for word in words.split()
}
_WORD_UNITS = {
    word: n
    for n, words in enumerate(("", "один одна одному", "два две двух двум", "три трех трем"))
    for word in words.split()
}
_ORDINAL_END_RE = re.compile(r"(?:ого|его|ому|ему)$")
_DAYPART_AFTER_RE = re.compile(rf"\s*(?:ч\b|час\w*)?\s*{_DAYPART}\b", re.I)
_HHMM_RE = re.compile(r"(\d{2}):(\d{2})")
_AT_TIME_RE = re.compile(r"T(\d{2}):\d{2}$")


def _option_time(option: str) -> tuple[int, int | None] | None:
    """(hour 0..23, minute or None) of an option that is only a clock time, else None ("Другое", "в пятницу")."""
    text = " ".join(re.sub(r"[()]", " ", option).split()).strip(" .?!").lower()
    m = _OPTION_RE.fullmatch(text)
    if m is None or int(m.group(1)) > 23:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else None
    return (hour + 12 if m.group(3) in _PM_PARTS and hour < 12 else hour), minute


def _am_pm_hours(hours: list[int]) -> set[int]:
    """Hours on the 12-hour dial (0..11) offered both ways: {5} for 17:00 / 05:00; empty when no two options are
    12 hours apart (17:00 / 18:00 is another question)."""
    return {h % 12 for h in hours for other in hours if other != h and other % 12 == h % 12}


def _pinned_hour(text: str, position: int) -> int | None:
    """The one hour (0..23) the time phrase pins on this dial position (0..11), else None: no hour there, a bare one
    ("в 8"), or two different ones."""
    written = [(h, pinned) for h, pinned in _written_hours(text) if h % 12 == position]
    if not written or not all(pinned for _, pinned in written):
        return None
    hours = {h for h, _ in written}
    return hours.pop() if len(hours) == 1 else None


def _pinned_update(
    item: ExtractedItem, when: When, hour: int, options: list[tuple[int, int | None]]
) -> dict[str, Any] | None:
    """The item's change when the question about ``hour`` is dropped: none but "low" -> "med" when its time has that
    hour; the pinned hour (minutes of that option, else of its time) when its time is the other half of the day or
    missing; None (keep the question) when it is another hour or the time is not a plain day / time."""
    m = _HHMM_RE.fullmatch(when.time or "") or _AT_TIME_RE.search(when.at or "")
    current = int(m.group(1)) if m else None
    if current == hour:
        return {"conf": "med"} if item.conf == "low" else {}
    if (current is not None and current % 12 != hour % 12) or when.at or when.in_min or when.period:
        return None
    minute = next((mm for h, mm in options if h == hour and mm is not None), None)
    if minute is None:
        clock = _HHMM_RE.fullmatch(when.time or "")
        minute = int(clock.group(2)) if clock else 0
    return {"when": when.model_copy(update={"time": f"{hour:02d}:{minute:02d}"})}


def _written_hours(text: str) -> list[tuple[int, bool]]:
    """Hours named in a time phrase, each as (hour 0..23, pinned): pinned when it is 13..23 or 0, or утра / вечера /
    am / pm follows it (then the hour is read with it: "8 вечера" -> 20); a bare "8" is (8, False)."""
    out: list[tuple[int, bool]] = []
    for rx in (_CLOCK_RE, _AT_HOUR_RE, _HOURS_RE):
        for m in rx.finditer(text):
            if int(m.group(1)) <= 23:
                out.append(_with_daypart(int(m.group(1)), text, m.end()))
    for m in _WORD_HOUR_RE.finditer(text):
        word, after = m.group(1).lower().replace("ё", "е"), (m.group(2) or "").lower().replace("ё", "е")
        hour, end = _WORD_HOURS.get(word), m.end()
        if hour is None or _ORDINAL_END_RE.search(after):
            continue
        if hour == 20 and after in _WORD_UNITS:
            hour, end = 20 + _WORD_UNITS[after], m.end(2)
        out.append(_with_daypart(hour, text, end))
    return out


def _with_daypart(hour: int, text: str, end: int) -> tuple[int, bool]:
    """(hour, pinned) of an hour written up to ``end`` of ``text``, read with the day part that follows it."""
    m = _DAYPART_AFTER_RE.match(text, end)
    if m is None:
        return hour, hour == 0 or hour >= 13
    part = m.group(1).lower()
    if part in _PM_PARTS and hour < 12:
        hour += 12
    elif part == "ночи" and 9 <= hour <= 11:
        hour += 12
    elif part in ("утра", "ночи", "am") and hour == 12:
        hour = 0
    return hour, True


async def answer_query(
    client: ChutesClient, settings: Settings, question: str, rows: list[dict], now: datetime, prefs: Prefs
) -> str:
    """Short Russian plain-text answer (no HTML) based only on ``rows``.

    The primary model, then (unavailable, unusable answer or 404) the fallback; when the fallback is missing too,
    the primary's error is raised.
    """
    if not rows:
        return "Ничего не нашёл 🤷"
    messages = [
        {"role": "system", "content": prompts.ANSWER_SYSTEM_PROMPT},
        {"role": "user", "content": prompts.build_answer_text(question, rows[:MAX_ANSWER_ROWS], now, prefs)},
    ]
    max_tokens = min(settings.llm_max_tokens, MAX_ANSWER_TOKENS)
    try:
        answer = await _answer(client, settings.llm_model, messages, max_tokens)
    except (AIUnavailable, AIBadResponse, AIModelNotFound) as e:
        if not settings.llm_fallback_model or settings.llm_fallback_model == settings.llm_model:
            raise
        log.warning("answer with %s failed (%s), trying %s", settings.llm_model, e, settings.llm_fallback_model)
        try:
            answer = await _answer(client, settings.llm_fallback_model, messages, max_tokens)
        except AIModelNotFound as missing:
            log.error("fallback model %s not found (%s)", settings.llm_fallback_model, missing)
            raise e from missing
    return answer


async def _answer(client: ChutesClient, model: str, messages: list[dict], max_tokens: int) -> str:
    res = await client.chat(purpose="answer", model=model, messages=messages, max_tokens=max_tokens, temperature=0.2)
    text = _MD_RE.sub("", res.content).strip()
    if not text:
        raise AIBadResponse(f"empty answer from {model}")
    return text
