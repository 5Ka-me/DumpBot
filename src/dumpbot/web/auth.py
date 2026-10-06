"""Authentication of Mini App requests by Telegram's ``initData`` (core.telegram.org/bots/webapps, "Validating data
received via the Mini App").

The app sends ``Authorization: tma <initData>`` with every ``/api`` request. initData is a query string signed by
Telegram: ``hash`` = hex HMAC-SHA256 of the data-check-string (every other field as ``key=value``, sorted by key,
joined by "\\n") with the key HMAC-SHA256("WebAppData", bot token). It is accepted when the signature matches,
``auth_date`` is at most 24 h old (and not from the future) and ``user.id`` is ``OWNER_ID``: no signature, a bad one or
an old one -> 401, someone else -> 403. Neither the answers nor the logs contain initData or the token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import parse_qsl

from aiohttp import web
from aiohttp.typedefs import Middleware

from dumpbot import clock
from dumpbot.web.http import API_PREFIX, Handler, error_response

log = logging.getLogger(__name__)

MAX_AGE: Final = timedelta(hours=24)
FUTURE_SKEW: Final = timedelta(minutes=5)  # a phone's clock may be a little ahead
MAX_INIT_DATA: Final = 8192  # bytes of the header value; real initData is well under 2 KB
SCHEME: Final = "tma"

UNAUTHORIZED: Final = "Открой приложение кнопкой «📱 Свалка» в чате с ботом."
EXPIRED: Final = "Сессия устарела — закрой приложение и открой его снова."
FORBIDDEN: Final = "Это приложение только для владельца бота."


class InitDataError(Exception):
    """initData is missing, malformed, not signed for this bot, or too old (``expired``). The message names the
    reason only, never the data."""

    def __init__(self, reason: str, *, expired: bool = False) -> None:
        super().__init__(reason)
        self.expired = expired


@dataclass(frozen=True)
class WebAppUser:
    """``user`` of the initData (WebAppUser)."""

    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None


USER: Final = web.RequestKey("tg_user", WebAppUser)  # set on every /api request that got through


def secret_key(bot_token: str) -> bytes:
    """HMAC-SHA256 of the bot token with the key "WebAppData"."""
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def sign(fields: dict[str, str], bot_token: str) -> str:
    """The ``hash`` Telegram puts into initData with these fields (also used by the tests to make initData)."""
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    return hmac.new(secret_key(bot_token), check.encode(), hashlib.sha256).hexdigest()


def validate_init_data(init_data: str, bot_token: str, *, now: datetime, max_age: timedelta = MAX_AGE) -> WebAppUser:
    """The user of signed, fresh initData; ``InitDataError`` otherwise (the user id is not checked here)."""
    if not init_data or len(init_data) > MAX_INIT_DATA:
        raise InitDataError("missing or too long")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise InitDataError("malformed") from None
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise InitDataError("duplicate field")
        fields[key] = value
    received = fields.pop("hash", "")
    if not received or not hmac.compare_digest(sign(fields, bot_token), received.lower()):
        raise InitDataError("bad signature")
    _check_age(fields.get("auth_date", ""), now, max_age)
    return _user(fields.get("user", ""))


def _check_age(auth_date: str, now: datetime, max_age: timedelta) -> None:
    if not auth_date.isdecimal():
        raise InitDataError("no auth_date")
    signed_at = datetime.fromtimestamp(int(auth_date), UTC)
    if now - signed_at > max_age:
        raise InitDataError("expired", expired=True)
    if signed_at - now > FUTURE_SKEW:
        raise InitDataError("auth_date in the future")


def _user(raw: str) -> WebAppUser:
    try:
        data = json.loads(raw)
    except ValueError:
        raise InitDataError("bad user") from None
    user_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise InitDataError("no user id")

    def text(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) else None

    return WebAppUser(
        id=user_id,
        first_name=text("first_name") or "",
        last_name=text("last_name"),
        username=text("username"),
        language_code=text("language_code"),
    )


def auth_middleware(bot_token: str, owner_id: int) -> Middleware:
    """Every ``/api`` request must carry the owner's initData: 401 / 403 otherwise. Static files need none."""

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        if not request.path.startswith(API_PREFIX):
            return await handler(request)
        scheme, _, init_data = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != SCHEME or not init_data.strip():
            return error_response(401, "unauthorized", UNAUTHORIZED)
        try:
            user = validate_init_data(init_data.strip(), bot_token, now=clock.utcnow())
        except InitDataError as e:
            log.info("Mini App request rejected: %s", e)
            if e.expired:
                return error_response(401, "expired", EXPIRED)
            return error_response(401, "unauthorized", UNAUTHORIZED)
        if owner_id == 0 or user.id != owner_id:
            log.warning("Mini App request from user %s, not the owner", user.id)
            return error_response(403, "forbidden", FORBIDDEN)
        request[USER] = user
        return await handler(request)

    return middleware
