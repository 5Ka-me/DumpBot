"""HTTP plumbing of the Mini App API: the application key of ``AppContext``, errors as JSON, request parsing.

Every error is ``{"error": {"code": str, "message": str, "field"?: str}}`` with a Russian message the app may show.
Unexpected exceptions are logged by type and code location only (their text may carry message content or SQL
parameters) and answered with 500.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiohttp import web

from dumpbot.context import AppContext
from dumpbot.services import events, items
from dumpbot.workers.jobs import describe_error, safe_traceback

log = logging.getLogger(__name__)

CTX: Final = web.AppKey("ctx", AppContext)
API_PREFIX: Final = "/api/"
# What a handler adds to the ``miniapp`` journal event of its call (``journal_note``) and further events of its own
# (``journal_also``); written by ``web.server.journal_middleware`` after the answer went out.
JOURNAL_DATA: Final = web.RequestKey("journal_data", dict)
JOURNAL_EXTRA: Final = web.RequestKey("journal_extra", list)

NOT_FOUND: Final = "Запись не найдена"
INTERNAL: Final = "😢 Что-то пошло не так — попробуй ещё раз."
NUL_TEXT: Final = "В тексте недопустимый символ (NUL)"
_HTTP_CODES: Final[dict[int, tuple[str, str]]] = {
    404: ("not_found", "Нет такого адреса"),
    405: ("method_not_allowed", "Этот метод здесь не поддерживается"),
    413: ("too_large", "Слишком большой запрос"),
}

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class ApiError(Exception):
    """An answer with an error status: ``code`` for the app's logic, ``message`` for the owner."""

    def __init__(self, status: int, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.field = field


def bad_request(message: str, field: str | None = None) -> ApiError:
    return ApiError(400, "bad_request", message, field=field)


def conflict(code: str, message: str) -> ApiError:
    return ApiError(409, code, message)


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=_dumps)


def error_response(status: int, code: str, message: str, field: str | None = None) -> web.Response:
    error: dict[str, Any] = {"code": code, "message": message}
    if field is not None:
        error["field"] = field
    return json_response({"error": error}, status=status)


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def ctx_of(request: web.Request) -> AppContext:
    return request.app[CTX]


def journal_note(request: web.Request, **data: Any) -> None:
    """Codes for the ``miniapp`` journal event of this call, e.g. ``move=True`` (docs/features/iteration-4.md §4)."""
    request.setdefault(JOURNAL_DATA, {}).update(data)


def journal_also(request: web.Request, *entries: events.Entry) -> None:
    """More journal events of this call (one per item it changed in bulk), written with its ``miniapp`` event."""
    request.setdefault(JOURNAL_EXTRA, []).extend(entries)


async def read_json(request: web.Request) -> dict[str, Any]:
    """The JSON object of the request body ({} without a body); 400 for anything else, and for a NUL character in
    any of its strings (the field: the top-level key)."""
    if not request.can_read_body:
        return {}
    try:
        data = json.loads(await request.text())
    except (ValueError, UnicodeDecodeError):
        raise bad_request("Тело запроса — не JSON") from None
    if not isinstance(data, dict):
        raise bad_request("Тело запроса должно быть JSON-объектом")
    for key, value in data.items():
        if has_nul(key) or has_nul(value):
            raise bad_request(NUL_TEXT, key)
    return data


def has_nul(value: Any) -> bool:
    """A string (or one inside a list / object) with U+0000, which Postgres text columns refuse."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, list):
        return any(has_nul(v) for v in value)
    if isinstance(value, dict):
        return any(has_nul(k) or has_nul(v) for k, v in value.items())
    return False


def path_id(request: web.Request, name: str = "id") -> int:
    """A positive integer from the path (404 otherwise)."""
    value = request.match_info.get(name, "")
    if not value.isdecimal() or int(value) <= 0 or len(value) > 18:
        raise ApiError(404, "not_found", NOT_FOUND)
    return int(value)


@web.middleware
async def errors_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """``ApiError`` / ``items.NotFoundError`` / aiohttp's 404, 405, 413 under /api -> JSON; anything else -> 500."""
    try:
        return await handler(request)
    except ApiError as e:
        return error_response(e.status, e.code, e.message, e.field)
    except items.NotFoundError:
        return error_response(404, "not_found", NOT_FOUND)
    except web.HTTPException as e:
        if not request.path.startswith(API_PREFIX) or e.status not in _HTTP_CODES:
            raise
        code, message = _HTTP_CODES[e.status]
        return error_response(e.status, code, message)
    except Exception as e:
        log.error("%s %s failed: %s\n%s", request.method, request.path, describe_error(e), safe_traceback(e))
        return error_response(500, "internal", INTERNAL)
