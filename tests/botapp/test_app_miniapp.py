"""The Mini App in the bot's lifecycle (docs/features/iteration-2.md §D): the web server starts with the background
tasks and stops first at shutdown; the owner's menu button opens the app ("📱 Свалка") or stays the commands menu."""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetChatMenuButton
from aiogram.types import MenuButtonCommands, MenuButtonWebApp

from dumpbot.bot import app as app_module
from dumpbot.bot.app import Lifecycle, build_app, build_bot, menu_url
from dumpbot.context import AppContext
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
