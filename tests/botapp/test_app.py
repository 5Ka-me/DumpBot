"""App assembly: bot defaults, dispatcher wiring, setup mode, error handler, lifecycle of background tasks."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage, SetMyCommands
from aiogram.types import BotCommandScopeChat

from dumpbot.bot import app as app_module
from dumpbot.bot.app import App, Lifecycle, build_app, build_bot, build_dispatcher
from dumpbot.bot.cbdata import ItemCb
from dumpbot.bot.middlewares import InFlight, OwnerOnly
from dumpbot.bot.routers.commands import BOT_COMMANDS
from dumpbot.context import AppContext
from dumpbot.services import ingest, items
from dumpbot.watchdog import Watchdog

OWNER = 4242
SECRET = "купить подарок Ане"  # stands for message text that must never reach the logs


def _ctx(settings, bot) -> AppContext:
    return AppContext(settings=settings, sessionmaker=None, bot=bot, ai=None, stt=None)  # type: ignore[arg-type]


class Runners:
    """Fake background loops: record start / cancel; ``fail`` makes one crash, ``finish`` makes one return."""

    def __init__(self, *, fail: str | None = None, finish: str | None = None) -> None:
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.fail = fail
        self.finish = finish
        self.contexts: list[AppContext] = []

    def __call__(self, ctx: AppContext) -> dict[str, Any]:
        self.contexts.append(ctx)
        return {name: self._loop(name) for name in ("jobs", "scheduler")}

    def _loop(self, name: str) -> Any:
        async def run() -> None:
            self.started.append(name)
            if name == self.fail:
                raise RuntimeError(SECRET)
            if name == self.finish:
                return
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.append(name)
                raise

        return run


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# --- bot / dispatcher --------------------------------------------------------------------------------------------


def test_build_bot_defaults(settings_factory, tg) -> None:
    bot = build_bot(settings_factory(), session=tg)
    assert bot.session is tg
    assert bot.default.parse_mode == ParseMode.HTML
    assert bot.default.link_preview is not None and bot.default.link_preview.is_disabled is True
    assert bot.id == 123456


def test_dispatcher_wiring(settings_factory, tg) -> None:
    settings = settings_factory()
    ctx = _ctx(settings, build_bot(settings, session=tg))
    dp = build_dispatcher(ctx)

    assert dp["ctx"] is ctx
    assert [r.name for r in dp.sub_routers] == ["commands", "settings", "edit", "callbacks", "capture"]
    owner_only = [m for m in dp.update.outer_middleware if isinstance(m, OwnerOnly)]
    assert [m.owner_id for m in owner_only] == [OWNER]
    assert sum(isinstance(m, InFlight) for m in dp.update.outer_middleware) == 1
    assert sorted(dp.resolve_used_update_types()) == ["callback_query", "message"]
    assert dp.startup.handlers == []  # no lifecycle given (aiogram's FSM registers its own shutdown hook)


def test_bot_commands_are_valid() -> None:
    names = [c for c, _ in BOT_COMMANDS]
    assert len(set(names)) == len(names)
    assert {"today", "tasks", "recent", "search", "ask", "recurring", "settings", "usage", "help", "start"} <= set(
        names
    )
    assert all(c.isascii() and c.islower() and 1 <= len(c) <= 32 for c in names)
    assert all(1 <= len(d) <= 256 for _, d in BOT_COMMANDS)


# --- setup mode (OWNER_ID=0) -------------------------------------------------------------------------------------


async def test_setup_mode_answers_start_with_the_user_id(settings_factory, tg, updates) -> None:
    settings = settings_factory(owner_id=0)
    bot = build_bot(settings, session=tg)
    dp = build_dispatcher(_ctx(settings, bot))

    await dp.feed_update(bot, updates.message("/start", from_id=98765))
    await dp.feed_update(bot, updates.message("привет", from_id=98765))
    await dp.feed_update(bot, updates.message("/start", from_id=98765, chat_id=-100, chat_type="group"))
    await dp.feed_update(bot, updates.callback(ItemCb(a="del", id=1), from_id=98765))

    [reply] = tg.of(SendMessage)
    assert reply.chat_id == 98765
    assert "98765" in reply.text and "OWNER_ID=98765" in reply.text
    assert len(tg.requests) == 1


# --- error handler -----------------------------------------------------------------------------------------------


async def test_handler_errors_are_logged_without_text(feed, updates, seed, tg, monkeypatch, caplog) -> None:
    created = await seed.item()

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(items, "delete_item", boom)
    with caplog.at_level(logging.DEBUG):
        result = await feed(updates.callback(ItemCb(a="del", id=created.item.id)))

    assert result is True  # handled by on_error, not raised
    assert tg.of(AnswerCallbackQuery)[-1].text == "😢 Не получилось, попробуй ещё раз"
    assert "RuntimeError" in caplog.text
    assert SECRET not in caplog.text


async def test_failed_message_gets_a_reply(feed, updates, tg, caplog) -> None:
    tg.fail(SendMessage, TelegramBadRequest(method=SendMessage(chat_id=OWNER, text="x"), message="chat not found"))
    with caplog.at_level(logging.ERROR):
        assert await feed(updates.message("/help")) is True
    assert "TelegramBadRequest" in caplog.text
    assert tg.texts[-1] == app_module.FAILED_MESSAGE


async def test_capture_failure_tells_the_owner(feed, updates, tg, monkeypatch) -> None:
    async def db_down(*args: Any, **kwargs: Any) -> None:
        raise ConnectionRefusedError

    monkeypatch.setattr(ingest, "save_incoming", db_down)
    await feed(updates.message("важная мысль"))

    assert tg.texts == [app_module.FAILED_MESSAGE]
    assert tg.reactions == []


async def test_in_flight_tracks_running_handlers() -> None:
    in_flight = InFlight()
    release = asyncio.Event()

    async def handler(event: Any, data: dict[str, Any]) -> str:
        await release.wait()
        return "done"

    task = asyncio.create_task(in_flight(handler, object(), {}))
    await _settle()
    assert in_flight.active == 1
    assert await in_flight.wait_idle(0.01) is False
    release.set()
    assert await in_flight.wait_idle(1) is True
    assert await task == "done" and in_flight.active == 0


async def test_shutdown_waits_for_updates_being_handled(settings_factory, tg) -> None:
    settings = settings_factory()
    bot = build_bot(settings, session=tg)
    order: list[str] = []

    async def cleanup() -> None:
        order.append("cleanup")

    lifecycle = Lifecycle(_ctx(settings, bot), runners=lambda ctx: {}, cleanup=cleanup, exit_func=lambda code: None)

    async def slow(event: Any, data: dict[str, Any]) -> None:
        await asyncio.sleep(0.05)
        order.append("handler")

    handling = asyncio.create_task(lifecycle.in_flight(slow, object(), {}))
    await _settle()
    await lifecycle.shutdown()
    await handling

    assert order == ["handler", "cleanup"]


# --- lifecycle ---------------------------------------------------------------------------------------------------


async def test_startup_and_shutdown(settings_factory, tg) -> None:
    settings = settings_factory()
    bot = build_bot(settings, session=tg)
    ctx = _ctx(settings, bot)
    runners, exits, cleaned = Runners(), [], []

    async def cleanup() -> None:
        cleaned.append(True)

    lifecycle = Lifecycle(ctx, runners=runners, cleanup=cleanup, exit_func=exits.append)
    dp = build_dispatcher(ctx, lifecycle=lifecycle)

    await dp.emit_startup(bot=bot)
    await _settle()

    [cmd] = tg.of(SetMyCommands)
    assert cmd.scope == BotCommandScopeChat(chat_id=OWNER)
    assert [c.command for c in cmd.commands] == [c for c, _ in BOT_COMMANDS]
    assert cmd.language_code is None
    assert sorted(runners.started) == ["jobs", "scheduler"]
    assert runners.contexts == [ctx]

    await dp.emit_shutdown()

    assert sorted(runners.cancelled) == ["jobs", "scheduler"]
    assert lifecycle.tasks == {}
    assert cleaned == [True]
    assert exits == []


async def test_crashed_background_task_exits_the_process(settings_factory, tg, caplog) -> None:
    settings = settings_factory()
    bot = build_bot(settings, session=tg)
    runners, exits = Runners(fail="scheduler"), []
    lifecycle = Lifecycle(_ctx(settings, bot), runners=runners, exit_func=exits.append)

    with caplog.at_level(logging.CRITICAL):
        await lifecycle.startup(bot)
        await _settle()

    assert exits == [1]
    assert "dumpbot-scheduler crashed: RuntimeError" in caplog.text
    assert SECRET not in caplog.text
    await lifecycle.shutdown()
    assert exits == [1]


async def test_background_task_that_returns_exits_too(settings_factory, tg) -> None:
    settings = settings_factory()
    bot = build_bot(settings, session=tg)
    exits: list[int] = []
    lifecycle = Lifecycle(_ctx(settings, bot), runners=Runners(finish="jobs"), exit_func=exits.append)

    await lifecycle.startup(bot)
    await _settle()
    await lifecycle.shutdown()

    assert exits == [1]


async def test_blocked_event_loop_exits_the_process(settings_factory, tg, caplog) -> None:
    settings = settings_factory()
    bot = build_bot(settings, session=tg)
    ctx = _ctx(settings, bot)
    ctx.watchdog = Watchdog(check_every_s=0.01)
    exits: list[int] = []

    def runners(ctx_: AppContext) -> dict[str, Any]:
        async def stuck() -> None:
            ctx_.watchdog.watch("scheduler", 0.1)
            time.sleep(0.5)  # noqa: ASYNC251  (on purpose: the whole event loop is stuck, no task ever ends)
            await asyncio.Event().wait()

        return {"scheduler": stuck}

    lifecycle = Lifecycle(ctx, runners=runners, exit_func=exits.append)
    with caplog.at_level(logging.CRITICAL):
        await lifecycle.startup(bot)
        await _settle()

    assert exits == [1]  # from the watchdog thread, while the loop was blocked
    assert "background loop made no progress (scheduler for " in caplog.text
    await lifecycle.shutdown()
    assert exits == [1]


async def test_hung_await_exits_the_process(settings_factory, tg) -> None:
    settings = settings_factory()
    bot = build_bot(settings, session=tg)
    ctx = _ctx(settings, bot)
    clock = [0.0]
    ctx.watchdog = Watchdog(check_every_s=0.01, monotonic=lambda: clock[0])
    exits: list[int] = []
    lifecycle = Lifecycle(ctx, runners=Runners(), exit_func=exits.append)

    await lifecycle.startup(bot)
    ctx.watchdog.watch("jobs", 60)  # the fake loops never beat, like an await that never returns
    clock[0] += 30
    await asyncio.sleep(0.05)
    assert exits == []
    clock[0] += 31
    for _ in range(100):
        if exits:
            break
        await asyncio.sleep(0.01)
    assert exits == [1]
    await lifecycle.shutdown()


async def test_setup_mode_skips_the_command_menu(settings_factory, tg) -> None:
    settings = settings_factory(owner_id=0)
    bot = build_bot(settings, session=tg)
    runners, exits = Runners(), []
    lifecycle = Lifecycle(_ctx(settings, bot), runners=runners, exit_func=exits.append)

    await lifecycle.startup(bot)
    await _settle()
    await lifecycle.shutdown()

    assert tg.of(SetMyCommands) == []
    assert sorted(runners.started) == ["jobs", "scheduler"]  # scheduler keeps the heartbeat fresh
    assert exits == []


async def test_command_menu_failure_does_not_stop_startup(settings_factory, tg) -> None:
    settings = settings_factory()
    bot = build_bot(settings, session=tg)
    tg.fail(SetMyCommands, TelegramBadRequest(method=SetMyCommands(commands=[]), message="bad"))
    runners = Runners()
    lifecycle = Lifecycle(_ctx(settings, bot), runners=runners, exit_func=lambda code: None)

    await lifecycle.startup(bot)
    await _settle()
    await lifecycle.shutdown()

    assert sorted(runners.started) == ["jobs", "scheduler"]


def test_default_runners_are_the_worker_and_the_scheduler(settings_factory, tg) -> None:
    settings = settings_factory()
    ctx = _ctx(settings, build_bot(settings, session=tg))
    runners = app_module.default_runners(ctx)
    assert set(runners) == {"jobs", "scheduler"}
    assert runners["jobs"].__self__.__class__.__name__ == "JobWorker"
    assert runners["scheduler"].__self__.__class__.__name__ == "Scheduler"


# --- build_app ---------------------------------------------------------------------------------------------------


async def test_build_app_and_cleanup(settings_factory, tg) -> None:
    settings = settings_factory(database_url="postgresql+asyncpg://nobody:x@127.0.0.1:1/none")
    runners = Runners()
    app = await build_app(settings, bot_session=tg, runners=runners, exit_func=lambda code: None)

    assert isinstance(app, App)
    assert app.dp["ctx"] is app.ctx
    assert app.ctx.bot is app.bot and app.bot.session is tg
    assert app.ctx.settings is settings
    assert app.dp.startup.handlers and app.dp.shutdown.handlers
    http = app.ctx.ai._http  # the shared session
    assert http is app.ctx.stt._backends["chutes"]._http and not http.closed
    assert not app.ctx.stt.local_configured  # LOCAL_STT_URL is empty
    assert app.lifecycle.keepalive is None  # KEEPALIVE_URL is empty

    await app.dp.emit_startup(bot=app.bot)
    await _settle()
    await app.dp.emit_shutdown()  # cancels the tasks and closes everything
    assert http.closed and tg.closed
    await app.cleanup()  # second close (main's finally) is a no-op
    assert sorted(runners.cancelled) == ["jobs", "scheduler"]


async def test_build_app_with_the_local_whisper(settings_factory, tg) -> None:
    settings = settings_factory(
        database_url="postgresql+asyncpg://nobody:x@127.0.0.1:1/none", local_stt_url="http://whisper:9000"
    )
    app = await build_app(settings, bot_session=tg, runners=Runners(), exit_func=lambda code: None)
    try:
        stt = app.ctx.stt
        assert stt.local_configured
        assert stt.order("auto") == ["chutes", "local"] and stt.order("local") == ["local", "chutes"]
        local = stt._backends["local"]
        assert local._url == "http://whisper:9000/transcribe"
        assert local._http is app.ctx.ai._http  # the shared session, closed once by cleanup
    finally:
        await app.cleanup()
