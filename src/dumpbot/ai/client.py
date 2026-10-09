"""chutes.ai client: OpenAI-compatible ``/chat/completions`` over aiohttp, model prices, usage accounting.

Error mapping (chutes answers with these in the wild):
- 401 "Invalid token", 402 (no balance), 403 -> ``AIConfigError``
- 404 "model not found" / "No matching cord found!" -> ``AIModelNotFound`` (an ``AIConfigError``: only this model is
  gone, so ``extract`` / ``answer_query`` still try the fallback model)
- 408, 429 (nginx HTML, maybe ``Retry-After``), 5xx incl. 503 "No instances available (yet)",
  500 "exhausted all available targets", network errors, timeouts -> ``AIUnavailable``
- 400 -> response_format ladder (strict json_schema -> json_object -> plain), then ``AIBadResponse``
- anything else -> ``AIBadResponse``

Pause (docs/features/iteration-3.md §1): a 408 / 429 / 5xx or a timeout puts the model on a pause of
``llm_cooldown_s`` (in process memory, per model; any 200 lifts it). The client only keeps the pause, ``extract``
decides what to skip (``cooldown_left``).

A call cancelled by its caller (``HEDGE_LOST``: the parallel request of the other model answered first, or a
shutdown) is recorded with ``ok=False`` and ``error="cancelled: <reason>"``; its tokens and cost only when the answer
had already arrived (else 0: nothing is known to be billed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dumpbot import clock
from dumpbot.config import Settings
from dumpbot.services import usage

log = logging.getLogger(__name__)

PRICE_TTL = timedelta(hours=6)
PRICE_RETRY = timedelta(minutes=10)  # after a failed /models fetch: use fallback prices, retry later
PRICE_FETCH_TIMEOUT_S = 10.0
SEED = 7
# Task.cancel() message for the call whose parallel twin gave a valid answer first (docs/features/iteration-3.md §1).
HEDGE_LOST = "hedge lost"

# response_format levels tried in order when the API rejects a request with HTTP 400.
LEVEL_SCHEMA = 0  # json_schema (strict) + chat_template_kwargs
LEVEL_JSON_OBJECT = 1  # {"type": "json_object"} + chat_template_kwargs
LEVEL_PLAIN = 2  # neither response_format nor chat_template_kwargs

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*")
_TAG_RE = re.compile(r"<[^>]+>")


class AIError(Exception):
    """Base class for AI provider errors."""


class AIUnavailable(AIError):
    """Temporary: network, timeout, 429, 5xx, 503 'No instances available'. Retry later."""

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AIConfigError(AIError):
    """401 (API key), 402 (no balance), 403, 404 (model / chute not found). Needs the owner."""


class AIModelNotFound(AIConfigError):
    """404: this model / chute does not exist (renamed, retired, a typo). Other models may still work."""


class AIBadResponse(AIError):
    """The model answered, but the result is unusable (after all retries)."""


@dataclass
class ChatResult:
    content: str  # without <think>…</think>
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    finish_reason: str | None = None  # "stop", "length" (cut at max_tokens), … as reported by the API


class _Rejected(Exception):
    """HTTP 400 from the API: try the next response_format level."""


# --- helpers shared with stt.py / extract.py -------------------------------------------------------------------


def strip_think(text: str) -> str:
    """Remove ``<think>…</think>`` blocks (and a dangling opening/closing tag) from model output."""
    text = _THINK_RE.sub("", text)
    low = text.lower()
    end = low.rfind("</think>")
    if end != -1:  # the opening tag was part of the prompt template
        text = text[end + len("</think>") :]
        low = text.lower()
    start = low.find("<think>")
    if start != -1:  # unterminated thinking: nothing after it is the answer
        text = text[:start]
    return text.strip()


def parse_json_object(text: str) -> dict[str, Any]:
    """Strip ``<think>`` and markdown code fences, then decode the first JSON object. ``ValueError`` if none."""
    text = _FENCE_RE.sub("", strip_think(text)).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in the model output")
    # Only the first "{" is tried: retrying at inner braces could return a nested object that still validates.
    obj, _ = json.JSONDecoder().raw_decode(text, start)
    if not isinstance(obj, dict):
        raise ValueError("model output is not a JSON object")
    return obj


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` header (seconds or HTTP date) -> seconds from now, or None."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - clock.utcnow()).total_seconds())


def error_snippet(body: bytes | str, limit: int = 200) -> str:
    """Short single-line version of an error body (HTML tags dropped) for logs and exception messages."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    return " ".join(_TAG_RE.sub(" ", body).split())[:limit]


def http_error(status: int, body: bytes | str, headers: Mapping[str, str]) -> AIError:
    """Map a non-200 HTTP status to the AI error hierarchy (400 is handled by the caller)."""
    message = f"HTTP {status}: {error_snippet(body)}"
    if status == 404:
        return AIModelNotFound(message)
    if status in (401, 402, 403):
        return AIConfigError(message)
    if status in (408, 429) or status >= 500:
        return AIUnavailable(message, retry_after=parse_retry_after(headers.get("Retry-After")))
    return AIBadResponse(message)


def _content_text(content: Any) -> str:
    """``message.content`` may be a string, null, or a list of parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content)


def _prompt_chars(messages: list[dict]) -> int:
    return sum(len(_content_text(m.get("content"))) for m in messages)


def _estimate_tokens(chars: int) -> int:
    return max(1, round(chars / 3)) if chars else 0


def _usage_int(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _cached_tokens(usage_data: Mapping[str, Any], prompt_tokens: int) -> int:
    """``usage.prompt_tokens_details.cached_tokens`` (part of ``prompt_tokens``), clamped to [0, prompt_tokens]."""
    details = usage_data.get("prompt_tokens_details")
    cached = _usage_int(details, "cached_tokens") if isinstance(details, Mapping) else None
    return min(max(cached or 0, 0), prompt_tokens)


# --- client ----------------------------------------------------------------------------------------------------


class ChutesClient:
    """Chat completions on chutes.ai with cost accounting into ``ai_usage``."""

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        http: aiohttp.ClientSession | None = None,
    ) -> None:
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._http = http
        self._own_http = http is None
        self._base = settings.chutes_base_url.rstrip("/")
        # (model, uses json_schema) -> response_format level that worked last time
        self._levels: dict[tuple[str, bool], int] = {}
        self._prices: dict[str, tuple[float, float, float]] = {}  # USD per 1M (input, output, cached input)
        self._prices_valid_until: datetime | None = None
        self._prices_lock = asyncio.Lock()
        self._paused_until: dict[str, float] = {}  # model -> time.monotonic() its pause ends

    async def close(self) -> None:
        if self._own_http and self._http is not None and not self._http.closed:
            await self._http.close()

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or (self._own_http and self._http.closed):
            self._http = aiohttp.ClientSession()
        return self._http

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.chutes_api_key.get_secret_value()}"}

    # -- pause after errors -------------------------------------------------------------------------------------

    def cooldown_left(self, model: str) -> float:
        """Seconds the model's pause after a 408 / 429 / 5xx / timeout still lasts; 0.0 when there is none."""
        until = self._paused_until.get(model)
        if until is None:
            return 0.0
        left = until - time.monotonic()
        if left <= 0:
            del self._paused_until[model]
            return 0.0
        return left

    def _pause(self, model: str, reason: str) -> None:
        seconds = self._settings.llm_cooldown_s
        if seconds <= 0:
            return
        if not self.cooldown_left(model):
            log.warning("model %s paused for %g s after %s", model, seconds, reason)
        self._paused_until[model] = time.monotonic() + seconds

    def _unpause(self, model: str) -> None:
        if self._paused_until.pop(model, None) is not None:
            log.info("model %s answers again, pause lifted", model)

    # -- chat ---------------------------------------------------------------------------------------------------

    async def chat(
        self,
        *,
        purpose: str,
        model: str,
        messages: list[dict],
        json_schema: dict | None = None,
        schema_name: str = "out",
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> ChatResult:
        """One chat completion. On HTTP 400 walks the response_format ladder (remembered per model).

        Records ``ai_usage`` for every HTTP attempt (success and failure) when a sessionmaker is set.
        """
        key = (model, json_schema is not None)
        level = self._levels.get(key, LEVEL_SCHEMA)
        while True:
            body = self._body(model, messages, json_schema, schema_name, max_tokens, temperature, level)
            try:
                result = await self._post(purpose, model, messages, body)
            except _Rejected as e:
                # 400 bodies may echo the request (message text): keep them out of exception messages / WARNING logs.
                log.debug("chutes 400 from %s: %s", model, e)
                nxt = _next_level(level, has_schema=json_schema is not None)
                if nxt is None:
                    raise AIBadResponse(f"request rejected by {model} (HTTP 400)") from None
                log.info("chutes rejected request to %s at format level %d, trying level %d", model, level, nxt)
                level = nxt
                continue
            self._levels[key] = level
            return result

    def _body(
        self,
        model: str,
        messages: list[dict],
        json_schema: dict | None,
        schema_name: str,
        max_tokens: int | None,
        temperature: float,
        level: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or self._settings.llm_max_tokens,
            "seed": SEED,
        }
        if level < LEVEL_PLAIN:
            body["chat_template_kwargs"] = {"enable_thinking": False}
            if json_schema is not None:
                body["response_format"] = (
                    {"type": "json_schema", "json_schema": {"name": schema_name, "schema": json_schema, "strict": True}}
                    if level == LEVEL_SCHEMA
                    else {"type": "json_object"}
                )
        return body

    async def _post(self, purpose: str, model: str, messages: list[dict], body: dict[str, Any]) -> ChatResult:
        started = time.monotonic()
        try:
            async with self._session().post(
                f"{self._base}/chat/completions",
                json=body,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=self._settings.llm_timeout_s),
            ) as resp:
                status, headers = resp.status, resp.headers
                raw = await resp.read()
        except asyncio.CancelledError as e:
            # No answer yet, so nothing is known to be billed; aiohttp has dropped the connection already.
            await self._record(purpose, model, ok=False, latency_ms=_ms_since(started), error=cancel_reason(e))
            raise
        except (TimeoutError, aiohttp.ClientError) as e:
            latency = _ms_since(started)
            # A connect timeout or a refused connection says nothing about the model (the network is down).
            if isinstance(e, TimeoutError) and not isinstance(e, aiohttp.ConnectionTimeoutError):
                self._pause(model, "a timeout")
            await self._record(purpose, model, ok=False, latency_ms=latency, error=f"network: {type(e).__name__}")
            raise AIUnavailable(f"chutes unreachable ({type(e).__name__})") from e
        latency = _ms_since(started)

        if status != 200:
            snippet = error_snippet(raw)
            await self._record(purpose, model, ok=False, latency_ms=latency, error=f"HTTP {status}: {snippet}")
            if status == 400:
                raise _Rejected(snippet)
            err = http_error(status, raw, headers)
            log.warning("chutes %s for %s (%s): %s", status, model, purpose, snippet)
            if isinstance(err, AIUnavailable):
                self._pause(model, f"HTTP {status}")
            raise err
        self._unpause(model)

        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError("message is not an object")
        except (ValueError, KeyError, IndexError, TypeError) as e:
            await self._record(purpose, model, ok=False, latency_ms=latency, error="malformed 200 response")
            log.debug("malformed 200 from %s: %s", model, error_snippet(raw))
            raise AIBadResponse(f"malformed response from {model} ({type(e).__name__})") from e

        raw_content = _content_text(message.get("content"))
        reasoning = message.get("reasoning_content")
        reasoning = reasoning if isinstance(reasoning, str) else ""
        if reasoning:
            log.debug("%s reasoning_content (%d chars): %s", model, len(reasoning), reasoning[:500])
        content = strip_think(raw_content)

        usage_data = data.get("usage")
        if not isinstance(usage_data, dict):
            usage_data = {}
        in_tok = _usage_int(usage_data, "prompt_tokens")
        out_tok = _usage_int(usage_data, "completion_tokens")
        if in_tok is None:
            in_tok = _estimate_tokens(_prompt_chars(messages))
        if out_tok is None:
            out_tok = _estimate_tokens(len(raw_content) + len(reasoning))
        # prompt_tokens includes the cached prefix, which is billed at the (much lower) cache-read price.
        cached = _cached_tokens(usage_data, in_tok)
        cancelled: asyncio.CancelledError | None = None
        try:
            prices = await self._price(model)
        except asyncio.CancelledError as e:  # the answer is here (and billed), but the caller no longer wants it
            prices, cancelled = self._known_price(model), e
        price_in, price_out, price_cached = prices
        cost = ((in_tok - cached) * price_in + cached * price_cached + out_tok * price_out) / 1e6
        if cancelled is not None:
            await self._record(
                purpose,
                model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                ok=False,
                latency_ms=latency,
                error=cancel_reason(cancelled),
            )
            raise cancelled
        finish_reason = choice.get("finish_reason")

        await self._record(
            purpose, model, input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost, latency_ms=latency
        )
        log.info("chutes %s %s: in=%d out=%d cost=$%.6f %dms", purpose, model, in_tok, out_tok, cost, latency)
        return ChatResult(
            content=content,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            latency_ms=latency,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        )

    async def _record(self, purpose: str, model: str, **fields: Any) -> None:
        if self._sessionmaker is not None:
            # A write cut off half-way (the call cancelled meanwhile) would lose a paid call or leave a DB connection
            # mid-transaction: the row is written first, then the cancellation goes on.
            await _run_to_end(usage.record_usage(self._sessionmaker, purpose=purpose, model=model, **fields))

    # -- prices -------------------------------------------------------------------------------------------------

    async def price(self, model: str) -> tuple[float, float]:
        """USD per 1M (input, output) tokens from ``GET {base}/models`` (cached 6 h), else the fallback prices."""
        price_in, price_out, _ = await self._price(model)
        return price_in, price_out

    async def _price(self, model: str) -> tuple[float, float, float]:
        """USD per 1M (input, output, cached input) tokens; cached input costs as much as input when not listed."""
        await self._refresh_prices()
        return self._known_price(model)

    def _known_price(self, model: str) -> tuple[float, float, float]:
        """The price from the list fetched last (no refresh), else the fallback prices."""
        found = self._prices.get(model)
        if found is None:
            fallback_in = self._settings.llm_fallback_price_in
            return fallback_in, self._settings.llm_fallback_price_out, fallback_in
        return found

    async def _refresh_prices(self) -> None:
        if self._prices_valid_until is not None and clock.utcnow() < self._prices_valid_until:
            return
        async with self._prices_lock:
            if self._prices_valid_until is not None and clock.utcnow() < self._prices_valid_until:
                return  # another task refreshed while we waited
            try:
                prices = await self._fetch_prices()
            except Exception as e:
                # Prices only affect accounting: never fail a paid call because the price list is down.
                log.warning("chutes price list unavailable (%s), using fallback prices", type(e).__name__)
                self._prices_valid_until = clock.utcnow() + PRICE_RETRY
                return
            self._prices = prices
            self._prices_valid_until = clock.utcnow() + PRICE_TTL

    async def _fetch_prices(self) -> dict[str, tuple[float, float, float]]:
        # The list is public. No key is sent: with a bad key it would answer 401 and break price lookup too.
        async with self._session().get(
            f"{self._base}/models",
            timeout=aiohttp.ClientTimeout(total=PRICE_FETCH_TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"GET /models -> HTTP {resp.status}")
            data = await resp.json(content_type=None)
        prices: dict[str, tuple[float, float, float]] = {}
        for entry in data.get("data") or []:
            pricing = entry.get("pricing") if isinstance(entry, dict) else None
            if not isinstance(pricing, dict) or not entry.get("id"):
                continue
            p_in = pricing.get("prompt", pricing.get("input"))
            p_out = pricing.get("completion", pricing.get("output"))
            try:
                price_in, price_out = float(p_in), float(p_out)
            except (TypeError, ValueError):
                continue
            try:
                price_cached = float(pricing["input_cache_read"])
            except (KeyError, TypeError, ValueError):
                price_cached = price_in  # not listed: no discount assumed
            prices[str(entry["id"])] = (price_in, price_out, price_cached)
        return prices


def _next_level(level: int, *, has_schema: bool) -> int | None:
    if level >= LEVEL_PLAIN:
        return None
    if not has_schema:
        return LEVEL_PLAIN  # without a schema levels 0 and 1 send the same body
    return level + 1


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def cancel_reason(e: asyncio.CancelledError) -> str:
    """``ai_usage.error`` of a cancelled call: "cancelled: <the Task.cancel message>" ("cancelled: hedge lost"),
    else "cancelled"."""
    reason = str(e.args[0]) if e.args and e.args[0] else ""
    return f"cancelled: {reason}" if reason else "cancelled"


async def _run_to_end[T](aw: Coroutine[Any, Any, T]) -> T:
    """Await ``aw`` to its end even when the calling task is cancelled meanwhile, then let the cancellation go on.

    A second cancellation while waiting cancels ``aw`` too, so nothing is left running.
    """
    task = asyncio.ensure_future(aw)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
