"""Message -> ``Extraction`` (structured output) and short answers to questions about saved data.

Fallback ladder of ``extract`` (architecture §4.7):
primary model -> on invalid output one retry with the validation error -> fallback model (one attempt).
Output cut at ``max_tokens`` (``finish_reason == "length"``) is first asked again once with ``LONG_MAX_TOKENS``.
``AIUnavailable`` or ``AIModelNotFound`` (404) from the primary skips straight to the fallback; if that is unavailable
too -> ``AIUnavailable``. A 404 from the fallback never turns a paid primary answer into a config error: the primary's
``AIUnavailable`` is raised if there was one, ``AIModelNotFound`` only when both models are missing, else
``AIBadResponse``. Images route to vision-capable models (``llm_model`` / ``vision_fallback_model``).

``split_steps`` (docs/features/iteration-2.md §C2) breaks one task into checklist steps by the same ladder.
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
    Extraction,
    QuerySpec,
    SplitResult,
    StepSpec,
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
        except ValueError as e:  # JSONDecodeError and pydantic ValidationError are both ValueErrors
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


def _error_summary(e: ValueError) -> str:
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
    ext = _apply_overrides(ext, inp)
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
