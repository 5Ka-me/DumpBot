"""Application assembly: Bot, Dispatcher (middlewares, routers, error handler) and the background-task lifecycle.

``build_app`` wires everything from ``Settings`` (engine, shared aiohttp session, AI clients, bot, context,
dispatcher); ``__main__`` only adds logging and polling. Tests call the same factories with a fake bot session,
fake background runners and a fake exit function.

Lifecycle (``dp.startup`` / ``dp.shutdown``): at startup the owner's command menu and menu button are set, the job
worker and the scheduler start as tasks and, with ``WEBAPP_ENABLED``, the Mini App's web server starts
(docs/features/iteration-2.md §D; the menu button opens it when ``WEBAPP_URL`` is set, else it is the commands menu).
A task that stops unexpectedly is logged and the process exits with code 1 (Docker restarts it). So does a task that
stops making progress (a hung await, a blocked event loop): ``ctx.watchdog`` watches the loops from a thread. At
shutdown the web server stops taking requests first, updates still being handled may finish (``InFlight``), the tasks
are cancelled and awaited, then the resources are closed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommandScopeChat,
    ErrorEvent,
    LinkPreviewOptions,
    MenuButtonCommands,
    MenuButtonWebApp,
    WebAppInfo,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from dumpbot import logging_setup
from dumpbot.ai.client import ChutesClient
from dumpbot.ai.stt import ChutesWhisperSTT, LocalWhisperSTT, RoutedSTT
from dumpbot.bot import quiet_requests
from dumpbot.bot.middlewares import InFlight, OwnerOnly
from dumpbot.bot.routers import make_routers
from dumpbot.bot.routers.commands import bot_commands
from dumpbot.bot.routers.common import FAILED
from dumpbot.config import Settings
from dumpbot.context import AppContext
from dumpbot.db.engine import make_engine, make_sessionmaker
from dumpbot.workers.jobs import describe_error, safe_traceback

if TYPE_CHECKING:
    from dumpbot.web.server import WebServer

log = logging.getLogger(__name__)

Runner = Callable[[], Awaitable[None]]
RunnerFactory = Callable[[AppContext], Mapping[str, Runner]]
WebFactory = Callable[[AppContext], "WebServer | None"]
Cleanup = Callable[[], Awaitable[None]]

FAILED_MESSAGE: Final = "😢 Что-то пошло не так, сообщение не обработано — попробуй ещё раз."
CRASH_EXIT_CODE: Final = 1
DRAIN_TIMEOUT_S: Final = 10.0  # how long shutdown waits for updates still being handled
MENU_BUTTON_TEXT: Final = "📱 Свалка"


def build_bot(settings: Settings, *, session: BaseSession | None = None) -> Bot:
    """Bot with HTML parse mode and link previews off. ``session`` lets tests point it at a fake API."""
    return Bot(
        token=settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)),
    )


def default_runners(ctx: AppContext) -> dict[str, Runner]:
    """The two background loops: the job queue worker and the reminder/digest scheduler."""
    # Imported here: the processor pulls in the AI pipeline, which the dispatcher itself does not need.
    from dumpbot.services import processor
    from dumpbot.workers.jobs import JobWorker
    from dumpbot.workers.scheduler import Scheduler

    return {"jobs": JobWorker(ctx, processor.HANDLERS).run, "scheduler": Scheduler(ctx).run}


def default_web(ctx: AppContext) -> WebServer | None:
    """The Mini App's web server when ``WEBAPP_ENABLED`` (docs/features/iteration-2.md §D), else None."""
    settings = ctx.settings
    if not settings.webapp_enabled:
        return None
    from dumpbot.web.server import WebServer  # aiohttp.web and the API only when the Mini App is on

    static_dir = settings.webapp_static_dir.strip() or None
    return WebServer(ctx, host=settings.webapp_host, port=settings.webapp_port, static_dir=static_dir)


def menu_url(settings: Settings) -> str | None:
    """``WEBAPP_URL`` for the menu button: only with ``WEBAPP_ENABLED`` and an https:// address (Telegram opens Mini
    Apps over HTTPS only)."""
    url = settings.webapp_url.strip()
    if not settings.webapp_enabled or not url:
        return None
    if not url.lower().startswith("https://"):
        log.warning("WEBAPP_URL must start with https://; the menu button stays the commands menu")
        return None
    return url


class Lifecycle:
    """Startup / shutdown of the background tasks and the Mini App's web server (registered on ``dp.startup`` /
    ``dp.shutdown``)."""

    def __init__(
        self,
        ctx: AppContext,
        *,
        runners: RunnerFactory = default_runners,
        web: WebFactory = default_web,
        cleanup: Cleanup | None = None,
        exit_func: Callable[[int], Any] = os._exit,
    ) -> None:
        self._ctx = ctx
        self._runners = runners
        self._web_factory = web
        self._cleanup = cleanup
        self._exit = exit_func
        self._stopping = False
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.web: WebServer | None = None
        self.in_flight = InFlight()  # registered on the dispatcher by build_dispatcher

    async def startup(self, bot: Bot) -> None:
        self._stopping = False
        await self._set_commands(bot)
        await self._set_menu_button(bot)
        for name, runner in self._runners(self._ctx).items():
            task = asyncio.create_task(runner(), name=f"dumpbot-{name}")
            task.add_done_callback(self._on_done)
            self.tasks[name] = task
        self._ctx.watchdog.start(self._on_stall)
        log.info("started background tasks: %s", ", ".join(self.tasks) or "none")
        await self._start_web()

    async def shutdown(self) -> None:
        """Stop the web server, let running update handlers finish, stop the background tasks, close the
        resources."""
        self._stopping = True
        self._ctx.watchdog.stop()
        await self._stop_web()
        await self.in_flight.wait_idle(DRAIN_TIMEOUT_S)
        tasks = list(self.tasks.values())
        self.tasks.clear()
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, result in zip(tasks, results, strict=True):
            if isinstance(result, Exception):
                log.warning("background task %s ended with %s", task.get_name(), describe_error(result))
        if self._cleanup is not None:
            await self._cleanup()
        log.info("stopped")

    async def _set_commands(self, bot: Bot) -> None:
        owner_id = self._ctx.owner_id
        if owner_id == 0:
            log.warning("OWNER_ID is not set: setup mode, the bot only answers /start with the sender's id")
            return
        try:
            await bot.set_my_commands(bot_commands(), scope=BotCommandScopeChat(chat_id=owner_id))
        except TelegramAPIError as e:  # the menu is cosmetic: polling still works without it
            log.warning("could not set the command menu: %s", describe_error(e))

    async def _set_menu_button(self, bot: Bot) -> None:
        """The owner's menu button: "📱 Свалка" opening the Mini App (``menu_url``), else the commands menu."""
        owner_id = self._ctx.owner_id
        if owner_id == 0:
            return
        url = menu_url(self._ctx.settings)
        button = MenuButtonWebApp(text=MENU_BUTTON_TEXT, web_app=WebAppInfo(url=url)) if url else MenuButtonCommands()
        try:
            await bot.set_chat_menu_button(chat_id=owner_id, menu_button=button)
        except TelegramAPIError as e:  # cosmetic as well
            log.warning("could not set the menu button: %s", describe_error(e))

    async def _start_web(self) -> None:
        """Start the Mini App's web server, if any. A failure (the port is taken) is logged; the bot works on."""
        server = self._web_factory(self._ctx)
        if server is None:
            return
        try:
            await server.start()
        except Exception as e:
            log.error("Mini App web server did not start: %s", describe_error(e))
            return
        self.web = server

    async def _stop_web(self) -> None:
        server, self.web = self.web, None
        if server is None:
            return
        try:
            await server.stop()
        except Exception as e:
            log.warning("stopping the Mini App web server failed: %s", describe_error(e))

    def _on_done(self, task: asyncio.Task[None]) -> None:
        if self._stopping or task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            log.critical("background task %s stopped unexpectedly; exiting", task.get_name())
        else:
            log.critical(
                "background task %s crashed: %s; exiting\n%s", task.get_name(), describe_error(exc), safe_traceback(exc)
            )
        logging_setup.flush()
        self._exit(CRASH_EXIT_CODE)

    def _on_stall(self, stalled: dict[str, float]) -> None:
        """Called from the watchdog thread: the event loop may be blocked, so this exits directly."""
        if self._stopping:
            return
        details = ", ".join(f"{name} for {age:.0f} s" for name, age in sorted(stalled.items()))
        log.critical("background loop made no progress (%s); exiting", details)
        logging_setup.flush()
        self._exit(CRASH_EXIT_CODE)


async def on_error(event: ErrorEvent) -> bool:
    """Last-resort handler: log type and code location only (exception text can carry message content or the
    bot token), tell the owner (toast or reply), mark the update handled."""
    e = event.exception
    log.error("update %s failed: %s\n%s", event.update.update_id, describe_error(e), safe_traceback(e))
    update = event.update
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(FAILED)
        elif update.message is not None:  # e.g. the DB is down: the owner must know the message was not saved
            await update.message.answer(FAILED_MESSAGE)
    except Exception as notify_error:
        log.debug("could not report the failure: %s", type(notify_error).__name__)
    return True


def build_dispatcher(ctx: AppContext, *, lifecycle: Lifecycle | None = None) -> Dispatcher:
    """Dispatcher with MemoryStorage FSM, the owner whitelist, ``ctx`` for handlers and the routers."""
    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    dp.update.outer_middleware(lifecycle.in_flight if lifecycle is not None else InFlight())
    dp.update.outer_middleware(OwnerOnly(ctx.owner_id))
    dp.errors.register(on_error)
    dp.include_routers(*make_routers())
    if lifecycle is not None:
        dp.startup.register(lifecycle.startup)
        dp.shutdown.register(lifecycle.shutdown)
    return dp


@dataclass
class App:
    ctx: AppContext
    dp: Dispatcher
    bot: Bot
    lifecycle: Lifecycle
    cleanup: Cleanup


@dataclass
class _Resources:
    """Closes what ``build_app`` opened, once (called from shutdown and again from ``main``'s finally)."""

    bot: Bot
    ai: ChutesClient
    stt: RoutedSTT
    http: aiohttp.ClientSession
    engine: AsyncEngine
    closed: bool = field(default=False)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        steps: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
            ("ai", self.ai.close),
            ("stt", self.stt.close),
            ("http", self.http.close),
            ("bot session", self.bot.session.close),
            ("engine", self.engine.dispose),
        ]
        for name, close in steps:
            try:
                await close()
            except Exception as e:
                log.warning("closing %s failed: %s", name, describe_error(e))


async def build_app(
    settings: Settings,
    *,
    bot_session: BaseSession | None = None,
    runners: RunnerFactory = default_runners,
    web: WebFactory = default_web,
    exit_func: Callable[[int], Any] = os._exit,
) -> App:
    """Everything the bot needs, not started yet (no DB connection or network call happens here).

    Must be called inside a running event loop (aiohttp sessions bind to it).
    """
    engine = make_engine(settings.database_url)
    sessionmaker = make_sessionmaker(engine)
    http = aiohttp.ClientSession()
    ai = ChutesClient(settings, sessionmaker, http)
    local = LocalWhisperSTT(settings, sessionmaker, http) if settings.local_stt_url.strip() else None
    stt = RoutedSTT(ChutesWhisperSTT(settings, sessionmaker, http), local)
    bot = build_bot(settings, session=bot_session)
    ctx = AppContext(settings=settings, sessionmaker=sessionmaker, bot=bot, ai=ai, stt=stt)
    quiet_requests.install(ctx)
    resources = _Resources(bot=bot, ai=ai, stt=stt, http=http, engine=engine)
    lifecycle = Lifecycle(ctx, runners=runners, web=web, cleanup=resources.close, exit_func=exit_func)
    dp = build_dispatcher(ctx, lifecycle=lifecycle)
    return App(ctx=ctx, dp=dp, bot=bot, lifecycle=lifecycle, cleanup=resources.close)
