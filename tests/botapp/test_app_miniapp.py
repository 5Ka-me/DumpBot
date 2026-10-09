"""The Mini App in the bot's lifecycle (docs/features/iteration-2.md §D): the web server starts with the background
tasks and stops first at shutdown; the owner's menu button opens the app ("📱 Свалка") or stays the commands menu.
The keep-alive ping (``KEEPALIVE_URL``) starts next to the web server, pings its ``/healthz``, stays out of the
watchdog and stops before the web server."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetChatMenuButton
from aiogram.types import MenuButtonCommands, MenuButtonWebApp

from dumpbot.bot import app as app_module
from dumpbot.bot.app import Lifecycle, build_app, build_bot, default_keepalive, menu_url
from dumpbot.context import AppContext
from dumpbot.keepalive import KeepAlive
from dumpbot.web.server import WebServer

OWNER = 4242
URL = "https://dump.tail1234.ts.net"


def _ctx(settings, bot) -> AppContext:
    return AppContext(settings=settings, sessionmaker=None, bot=bot, ai=None, stt=None)  # type: ignore[arg-type]


def _no_runners(ctx: AppContext) -> dict[str, Any]:
    return {}


@pytest.fixture
def build(tmp_path: Path) -> Path:
    root = tmp_path / "webapp"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>Свалка</title>", encoding="utf-8")
    return root


def _web_on_free_port(static_dir: Path | None):
    def factory(ctx: AppContext) -> WebServer:
        return WebServer(ctx, host="127.0.0.1", port=0, static_dir=static_dir)

    return factory


async def _get(port: int, path: str) -> tuple[int, str]:
    async with aiohttp.ClientSession() as http, http.get(f"http://127.0.0.1:{port}{path}") as response:
        return response.status, await response.text()


# --- web server ----------------------------------------------------------------------------------------------------


async def test_lifecycle_starts_and_stops_the_web_server(settings_factory, tg, build) -> None:
    settings = settings_factory(webapp_enabled=True)
    bot = build_bot(settings, session=tg)
    lifecycle = Lifecycle(
        _ctx(settings, bot), runners=_no_runners, web=_web_on_free_port(build), exit_func=lambda code: None
    )

    await lifecycle.startup(bot)
    server = lifecycle.web
    assert server is not None and server.running
    port = server.bound_port
    assert port
    assert await _get(port, "/") == (200, "<!doctype html><title>Свалка</title>")
    status, body = await _get(port, "/api/me")  # no initData
    assert status == 401 and '"unauthorized"' in body

    await lifecycle.shutdown()
    assert lifecycle.web is None and not server.running
    with pytest.raises(aiohttp.ClientConnectionError):
        await _get(port, "/")


async def test_web_server_is_off_by_default(settings_factory, tg) -> None:
    settings = settings_factory()
    assert app_module.default_web(_ctx(settings, build_bot(settings, session=tg))) is None
    enabled = settings_factory(webapp_enabled=True, webapp_port=18080, webapp_static_dir="/srv/app")
    server = app_module.default_web(_ctx(enabled, build_bot(enabled, session=tg)))
    assert isinstance(server, WebServer)
    assert (server.host, server.port, server.static_dir) == ("0.0.0.0", 18080, Path("/srv/app"))


async def test_a_taken_port_does_not_stop_the_bot(settings_factory, tg, caplog) -> None:
    settings = settings_factory(webapp_enabled=True)
    bot = build_bot(settings, session=tg)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]

        def factory(ctx: AppContext) -> WebServer:
            return WebServer(ctx, host="127.0.0.1", port=port)

        exits: list[int] = []
        lifecycle = Lifecycle(_ctx(settings, bot), runners=_no_runners, web=factory, exit_func=exits.append)
        with caplog.at_level(logging.ERROR):
            await lifecycle.startup(bot)
        assert lifecycle.web is None and exits == []
        assert "Mini App web server did not start" in caplog.text
        await lifecycle.shutdown()


async def test_web_server_stops_before_the_drain(settings_factory, tg, build) -> None:
    settings = settings_factory(webapp_enabled=True)
    bot = build_bot(settings, session=tg)
    order: list[str] = []
    lifecycle = Lifecycle(
        _ctx(settings, bot), runners=_no_runners, web=_web_on_free_port(build), exit_func=lambda code: None
    )
    await lifecycle.startup(bot)
    server = lifecycle.web
    assert server is not None
    stop = server.stop

    async def recording_stop() -> None:
        order.append("web")
        await stop()

    async def wait_idle(limit_s: float) -> bool:
        order.append("drain")
        return True

    server.stop = recording_stop  # type: ignore[method-assign]
    lifecycle.in_flight.wait_idle = wait_idle  # type: ignore[method-assign]
    await lifecycle.shutdown()
    assert order == ["web", "drain"]


async def test_build_app_passes_the_web_factory(settings_factory, tg, build) -> None:
    settings = settings_factory(database_url="postgresql+asyncpg://nobody:x@127.0.0.1:1/none", webapp_enabled=True)
    app = await build_app(
        settings, bot_session=tg, runners=_no_runners, web=_web_on_free_port(build), exit_func=lambda code: None
    )
    await app.dp.emit_startup(bot=app.bot)
    await asyncio.sleep(0)
    assert app.lifecycle.web is not None and app.lifecycle.web.running
    await app.dp.emit_shutdown()
    assert app.lifecycle.web is None
    await app.cleanup()


# --- keep-alive ----------------------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_keepalive_pings_healthz_and_stops_before_the_web_server(settings_factory, tg, build, caplog) -> None:
    settings = settings_factory(webapp_enabled=True)
    bot = build_bot(settings, session=tg)
    ctx = _ctx(settings, bot)
    port = _free_port()
    exits: list[int] = []
    order: list[str] = []

    def web(c: AppContext) -> WebServer:
        return WebServer(c, host="127.0.0.1", port=port, static_dir=build)

    def healthz_hits() -> int:
        return sum(r.getMessage().startswith("GET /healthz 200") for r in caplog.records)

    async with aiohttp.ClientSession() as http:
        keepalive = KeepAlive(f"http://127.0.0.1:{port}/healthz", http, interval_s=0.02)
        lifecycle = Lifecycle(ctx, runners=_no_runners, web=web, keepalive=keepalive, exit_func=exits.append)
        with caplog.at_level(logging.DEBUG, logger="dumpbot"):
            await lifecycle.startup(bot)
            assert keepalive.running and lifecycle.web is not None
            deadline = time.monotonic() + 5
            while healthz_hits() < 3:
                assert time.monotonic() < deadline, "no keep-alive pings"
                await asyncio.sleep(0.01)

            server = lifecycle.web
            stop_keepalive, stop_web = keepalive.stop, server.stop

            async def recording_keepalive_stop() -> None:
                order.append("keepalive")
                await stop_keepalive()

            async def recording_web_stop() -> None:
                order.append("web")
                await stop_web()

            keepalive.stop = recording_keepalive_stop  # type: ignore[method-assign]
            server.stop = recording_web_stop  # type: ignore[method-assign]
            await lifecycle.shutdown()

    assert order == ["keepalive", "web"]
    assert not keepalive.running and lifecycle.web is None
    assert "keep-alive is on: GET 127.0.0.1 every 0 s" in caplog.text
    assert "keep-alive ping to 127.0.0.1 answered" in caplog.text
    assert ctx.watchdog._limits == {} and ctx.watchdog._beats == {}  # neither watched nor beating
    assert exits == []


async def test_default_keepalive(settings_factory, caplog) -> None:
    async with aiohttp.ClientSession() as http:
        assert default_keepalive(settings_factory(), http) is None
        settings = settings_factory(keepalive_url=" https://dump-bot.onrender.com/healthz ", keepalive_interval_s=300)
        keepalive = default_keepalive(settings, http)
        assert isinstance(keepalive, KeepAlive)
        assert (keepalive.url, keepalive.host, keepalive.interval_s) == (
            "https://dump-bot.onrender.com/healthz",
            "dump-bot.onrender.com",
            300,
        )
        assert keepalive._http is http
        with caplog.at_level(logging.WARNING):
            assert default_keepalive(settings_factory(keepalive_url="dump-bot.onrender.com/healthz"), http) is None
        assert "KEEPALIVE_URL must be an http(s):// address; keep-alive is off" in caplog.text


def _app_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == app_module.log.name and r.levelno == logging.WARNING]


@pytest.mark.parametrize(
    ("value", "used"),
    [(60, 60), (300, 300), (840, 840), (0, 60), (59.9, 60), (900, 840), (1800, 840), (float("nan"), 600)],
)
async def test_default_keepalive_keeps_the_interval_within_limits(settings_factory, caplog, value, used) -> None:
    async with aiohttp.ClientSession() as http:
        with caplog.at_level(logging.WARNING):
            off = default_keepalive(settings_factory(keepalive_interval_s=value), http)
            assert off is None and _app_warnings(caplog) == []  # keep-alive off: the interval does not matter
            settings = settings_factory(
                keepalive_url="https://dump-bot.onrender.com/healthz", keepalive_interval_s=value
            )
            keepalive = default_keepalive(settings, http)
    assert keepalive is not None and keepalive.interval_s == used
    if value == used:
        assert _app_warnings(caplog) == []
    else:
        assert _app_warnings(caplog) == [
            "KEEPALIVE_INTERVAL_S must be from 60 to 840 s (free hosts sleep after 15 minutes without requests); "
            f"using {used} s"
        ]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/healthz",
        "http://127.0.0.1:8080/healthz",
        "http://0.0.0.0:8080/healthz",
        "http://[::1]:8080/healthz",
        "http://10.0.0.5:8080/healthz",
        "http://bot:8080/healthz",
    ],
)
async def test_default_keepalive_warns_about_an_address_inside_the_container(settings_factory, caplog, url) -> None:
    """Such a ping is answered by the bot itself and never passes the host's edge: Render still puts the service to
    sleep. Warned once at startup; pinged anyway (other setups may want it)."""
    async with aiohttp.ClientSession() as http:
        with caplog.at_level(logging.WARNING):
            keepalive = default_keepalive(settings_factory(keepalive_url=url), http)
    assert keepalive is not None and keepalive.url == url
    [warning] = _app_warnings(caplog)
    assert warning == (
        f"KEEPALIVE_URL points inside the container or a private network ({keepalive.host}): free hosts only count "
        "requests through their public address, e.g. https://<service>.onrender.com/healthz"
    )


async def test_default_keepalive_is_quiet_about_a_public_address(settings_factory, caplog) -> None:
    async with aiohttp.ClientSession() as http:
        with caplog.at_level(logging.WARNING):
            settings = settings_factory(keepalive_url="https://dump-bot.onrender.com/healthz")
            assert default_keepalive(settings, http) is not None
    assert _app_warnings(caplog) == []


async def test_build_app_gives_keepalive_the_shared_session(settings_factory, tg) -> None:
    settings = settings_factory(
        database_url="postgresql+asyncpg://nobody:x@127.0.0.1:1/none",
        keepalive_url="http://127.0.0.1:1/healthz",  # never reached: the first ping is due in 10 minutes
    )
    app = await build_app(settings, bot_session=tg, runners=_no_runners, exit_func=lambda code: None)
    keepalive = app.lifecycle.keepalive
    assert keepalive is not None and keepalive.interval_s == 600
    assert keepalive._http is app.ctx.ai._http
    await app.dp.emit_startup(bot=app.bot)
    assert keepalive.running
    await app.dp.emit_shutdown()
    assert not keepalive.running and app.ctx.ai._http.closed
    await app.cleanup()


# --- menu button ---------------------------------------------------------------------------------------------------


async def test_menu_button_opens_the_mini_app(settings_factory, tg) -> None:
    settings = settings_factory(webapp_enabled=True, webapp_url=f" {URL} ")
    bot = build_bot(settings, session=tg)
    lifecycle = Lifecycle(_ctx(settings, bot), runners=_no_runners, web=lambda ctx: None, exit_func=lambda c: None)
    await lifecycle.startup(bot)
    await lifecycle.shutdown()

    [call] = tg.of(SetChatMenuButton)
    assert call.chat_id == OWNER
    assert call.menu_button == MenuButtonWebApp(text="📱 Свалка", web_app={"url": URL})


@pytest.mark.parametrize(
    "overrides",
    [
        {},  # the Mini App is off
        {"webapp_enabled": True},  # no URL yet
        {"webapp_enabled": True, "webapp_url": "http://dump.tail1234.ts.net"},  # not HTTPS
        {"webapp_url": URL},  # URL without WEBAPP_ENABLED
    ],
)
async def test_otherwise_the_menu_button_is_the_commands_menu(settings_factory, tg, overrides) -> None:
    settings = settings_factory(**overrides)
    bot = build_bot(settings, session=tg)
    lifecycle = Lifecycle(_ctx(settings, bot), runners=_no_runners, web=lambda ctx: None, exit_func=lambda c: None)
    await lifecycle.startup(bot)
    await lifecycle.shutdown()
    [call] = tg.of(SetChatMenuButton)
    assert call.chat_id == OWNER and isinstance(call.menu_button, MenuButtonCommands)


async def test_no_menu_button_in_setup_mode(settings_factory, tg) -> None:
    settings = settings_factory(owner_id=0, webapp_enabled=True, webapp_url=URL)
    bot = build_bot(settings, session=tg)
    lifecycle = Lifecycle(_ctx(settings, bot), runners=_no_runners, web=lambda ctx: None, exit_func=lambda c: None)
    await lifecycle.startup(bot)
    await lifecycle.shutdown()
    assert tg.of(SetChatMenuButton) == []


async def test_menu_button_failure_does_not_stop_startup(settings_factory, tg, caplog) -> None:
    settings = settings_factory(webapp_enabled=True, webapp_url=URL)
    bot = build_bot(settings, session=tg)
    tg.fail(SetChatMenuButton, TelegramBadRequest(method=SetChatMenuButton(), message="bad"))
    lifecycle = Lifecycle(_ctx(settings, bot), runners=_no_runners, web=lambda ctx: None, exit_func=lambda c: None)
    with caplog.at_level(logging.WARNING):
        await lifecycle.startup(bot)
    await lifecycle.shutdown()
    assert "could not set the menu button" in caplog.text


def test_menu_url(settings_factory) -> None:
    assert menu_url(settings_factory(webapp_enabled=True, webapp_url=URL)) == URL
    assert menu_url(settings_factory(webapp_enabled=True, webapp_url="")) is None
    assert menu_url(settings_factory(webapp_url=URL)) is None
