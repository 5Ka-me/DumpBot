"""initData validation (core.telegram.org/bots/webapps) and the 401 / 403 of every /api request."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode

import pytest

from dumpbot.web import auth

TOKEN = "123456:TEST-token-for-the-mini-app"
NOW = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
OWNER = 4242


def make(user_id: int = OWNER, *, auth_date: datetime = NOW, token: str = TOKEN, **extra: str) -> str:
    fields = {
        "query_id": "AAH0dGVzdA",
        "user": json.dumps({"id": user_id, "first_name": "Дима", "last_name": "Б", "username": "dima"}),
        "auth_date": str(int(auth_date.timestamp())),
        **extra,
    }
    return urlencode({**fields, "hash": auth.sign(fields, token)})


def test_signature_follows_the_telegram_algorithm() -> None:
    # Written out by hand from the docs: secret = HMAC_SHA256(key="WebAppData", msg=token);
    # hash = hex(HMAC_SHA256(key=secret, msg="auth_date=…\nquery_id=…\nuser=…")).
    fields = {"user": '{"id":1}', "auth_date": "1700000000", "query_id": "q"}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    check = 'auth_date=1700000000\nquery_id=q\nuser={"id":1}'
    assert auth.sign(fields, TOKEN) == hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()


def test_valid_init_data_gives_the_user() -> None:
    user = auth.validate_init_data(make(signature="abc", start_param="x"), TOKEN, now=NOW + timedelta(hours=1))
    assert user == auth.WebAppUser(id=OWNER, first_name="Дима", last_name="Б", username="dima", language_code=None)


def test_the_signature_field_is_part_of_the_checked_data() -> None:
    data = dict(parse_qsl(make(signature="abc")))
    data["signature"] = "forged"
    with pytest.raises(auth.InitDataError, match="bad signature"):
        auth.validate_init_data(urlencode(data), TOKEN, now=NOW)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(user=json.dumps({"id": 1, "first_name": "Чужой"})),  # changed user
        lambda d: d.update(auth_date=str(int(NOW.timestamp()) + 60)),  # changed date
        lambda d: d.update(hash="0" * 64),
        lambda d: d.pop("hash"),
    ],
)
def test_tampered_init_data_is_rejected(mutate) -> None:
    data = dict(parse_qsl(make()))
    mutate(data)
    with pytest.raises(auth.InitDataError) as e:
        auth.validate_init_data(urlencode(data), TOKEN, now=NOW)
    assert not e.value.expired


def test_another_bots_signature_is_rejected() -> None:
    with pytest.raises(auth.InitDataError, match="bad signature"):
        auth.validate_init_data(make(token="999:OTHER"), TOKEN, now=NOW)


def test_init_data_older_than_a_day_is_expired() -> None:
    data = make(auth_date=NOW - timedelta(hours=24))
    assert auth.validate_init_data(data, TOKEN, now=NOW).id == OWNER  # exactly 24 h: still fine
    with pytest.raises(auth.InitDataError) as e:
        auth.validate_init_data(data, TOKEN, now=NOW + timedelta(seconds=1))
    assert e.value.expired


def test_init_data_from_the_future_is_rejected() -> None:
    auth.validate_init_data(make(auth_date=NOW + timedelta(minutes=4)), TOKEN, now=NOW)  # clock skew
    with pytest.raises(auth.InitDataError, match="future"):
        auth.validate_init_data(make(auth_date=NOW + timedelta(minutes=10)), TOKEN, now=NOW)


@pytest.mark.parametrize("value", ["", "garbage", "a=1&a=2", "x" * 9000, "auth_date=1&hash=zz"])
def test_malformed_init_data_is_rejected(value: str) -> None:
    with pytest.raises(auth.InitDataError):
        auth.validate_init_data(value, TOKEN, now=NOW)


def test_signed_data_without_a_user_is_rejected() -> None:
    fields = {"auth_date": str(int(NOW.timestamp())), "query_id": "q"}
    data = urlencode({**fields, "hash": auth.sign(fields, TOKEN)})
    with pytest.raises(auth.InitDataError, match="user"):
        auth.validate_init_data(data, TOKEN, now=NOW)


# --- the middleware ------------------------------------------------------------------------------------------------


async def test_owner_gets_in(api) -> None:
    reply = await api.get("/api/me")
    assert reply.status == 200
    assert reply.data["user"] == {"id": OWNER, "first_name": "Дима", "last_name": None, "username": "dima"}


@pytest.mark.parametrize("header", ["", "tma", "Bearer abc", "tma not-init-data"])
async def test_missing_or_bad_init_data_is_401(api, header: str) -> None:
    reply = await api.get("/api/today", auth_header=header)
    assert reply.status == 401
    assert reply.data["error"]["code"] == "unauthorized"
    assert "📱 Свалка" in reply.data["error"]["message"]


async def test_expired_init_data_is_401_expired(api) -> None:
    reply = await api.get("/api/today", auth_header=f"tma {api.init_data(age=timedelta(hours=25))}")
    assert (reply.status, reply.data["error"]["code"]) == (401, "expired")


async def test_someone_else_is_403(api, caplog) -> None:
    init_data = api.init_data(user_id=777)
    with caplog.at_level(logging.INFO):
        reply = await api.get("/api/today", auth_header=f"tma {init_data}")
    assert (reply.status, reply.data["error"]["code"]) == (403, "forbidden")
    assert "777" in caplog.text
    assert init_data not in caplog.text and TOKEN not in caplog.text


async def test_every_api_path_needs_auth_even_unknown_ones(api) -> None:
    for method, path in [("GET", "/api/feed"), ("POST", "/api/inbox"), ("GET", "/api/files/1/0"), ("GET", "/api/x")]:
        assert (await api.call(method, path, auth_header="")).status == 401


async def test_errors_and_logs_never_contain_init_data_or_the_token(api, caplog) -> None:
    bad = api.init_data().replace("Dima", "Eve") + "&hash=0"
    with caplog.at_level(logging.DEBUG):
        reply = await api.get("/api/today", auth_header=f"tma {bad}")
    assert reply.status == 401
    body = json.dumps(reply.data, ensure_ascii=False)
    assert TOKEN not in body and "query_id" not in body
    assert TOKEN not in caplog.text and "query_id" not in caplog.text


async def test_setup_mode_owner_zero_lets_nobody_in(ctx, static_dir, frozen) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from dumpbot.web.server import build_web_app

    ctx.settings.owner_id = 0
    client = TestClient(TestServer(build_web_app(ctx, static_dir=static_dir), host="127.0.0.1"))
    await client.start_server()
    try:
        fields = {
            "user": json.dumps({"id": OWNER, "first_name": "Дима"}),
            "auth_date": str(int(frozen.now.timestamp())),
        }
        header = f"tma {urlencode({**fields, 'hash': auth.sign(fields, TOKEN)})}"
        async with client.get("/api/me", headers={"Authorization": header}) as response:
            assert response.status == 403
    finally:
        await client.close()
