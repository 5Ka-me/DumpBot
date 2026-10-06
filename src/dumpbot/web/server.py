"""The Mini App's web server inside the bot process (docs/features/iteration-2.md §D).

``build_web_app`` assembles the aiohttp application: middlewares (errors as JSON outermost, then the initData check of
every ``/api`` request), the API routes, and the frontend build from ``webapp_static_dir`` — a file that exists is
served as it is (``assets/`` cached for a year: their names carry a hash), any other path without a file extension
gets ``index.html`` (the app's own router). ``WebServer`` runs it with ``AppRunner`` / ``TCPSite`` and is started and
stopped by ``bot.app.Lifecycle``. The access log is DEBUG only and has no query strings (a search is message text).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
from aiohttp.web_request import BaseRequest
from aiohttp.web_response import StreamResponse

from dumpbot.context import AppContext
from dumpbot.web import api_inbox, api_items, api_lists, files
from dumpbot.web.auth import auth_middleware
from dumpbot.web.http import API_PREFIX, CTX, errors_middleware

log = logging.getLogger(__name__)

STATIC: Final = web.AppKey("static", Path)
MAX_BODY: Final = 256 * 1024  # JSON bodies only; the longest is an inbox text of 4096 characters
SHUTDOWN_TIMEOUT_S: Final = 5.0  # requests still running at shutdown get this long
ASSETS_CACHE: Final = "public, max-age=31536000, immutable"


class _AccessLog(AbstractAccessLogger):
    """``GET /api/items/12 200 14 ms`` at DEBUG: the path only (no query string, no headers)."""

    def log(self, request: BaseRequest, response: StreamResponse, time: float) -> None:
        self.logger.debug("%s %s %s %.0f ms", request.method, request.path, response.status, time * 1000)


def build_web_app(ctx: AppContext, *, static_dir: str | Path | None = None) -> web.Application:
    """The aiohttp application of the Mini App (not started)."""
    token = ctx.settings.bot_token.get_secret_value()
    app = web.Application(
        middlewares=[errors_middleware, auth_middleware(token, ctx.owner_id)], client_max_size=MAX_BODY
    )
    app[CTX] = ctx
    app[files.IMAGES] = files.ImageCache()
    app[STATIC] = Path(static_dir) if static_dir else Path()
    app.add_routes([*api_lists.routes(), *api_items.routes(), *api_inbox.routes(), *files.routes()])
    app.router.add_get("/{tail:.*}", frontend)
    app.on_response_prepare.append(_headers)
    return app


async def _headers(request: web.Request, response: web.StreamResponse) -> None:
    """No caching of API answers; no MIME sniffing anywhere. (Framing must stay allowed: Telegram Web shows the app
    in an iframe.)"""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith(API_PREFIX):
        response.headers.setdefault("Cache-Control", "no-store")


async def frontend(request: web.Request) -> web.FileResponse:
    """A file of the frontend build, else ``index.html`` for app routes (404 for API paths and missing files)."""
    if request.path.startswith(API_PREFIX) or request.path == API_PREFIX.rstrip("/"):
        raise web.HTTPNotFound()
    root = request.app[STATIC]
    tail = request.match_info.get("tail", "")
    target = _inside(root, tail)
    if target is not None and target.is_file():
        cache = ASSETS_CACHE if tail.startswith("assets/") else "no-cache"
        return web.FileResponse(target, headers={"Cache-Control": cache})
    if "." in tail.rsplit("/", 1)[-1]:  # a missing file, not a route of the app
        raise web.HTTPNotFound()
    index = root / "index.html"
    if not root.parts or not index.is_file():
        raise web.HTTPNotFound(text="The Mini App is not built (WEBAPP_STATIC_DIR).")
    return web.FileResponse(index, headers={"Cache-Control": "no-cache"})


def _inside(root: Path, tail: str) -> Path | None:
    """``root / tail`` when it stays inside ``root`` (no "..", no absolute paths), else None."""
    if not root.parts or not tail or "\\" in tail or "\x00" in tail:
        return None
    try:
        base = root.resolve()
        target = (base / tail).resolve()
    except (OSError, ValueError):
        return None
    return target if target.is_relative_to(base) and target != base else None


class WebServer:
    """The Mini App's HTTP server: ``start`` binds ``host:port``, ``stop`` lets running requests finish (up to
    ``SHUTDOWN_TIMEOUT_S``) and closes it."""

    def __init__(self, ctx: AppContext, *, host: str, port: int, static_dir: str | Path | None = None) -> None:
        self.host = host
        self.port = port
        self.static_dir = Path(static_dir) if static_dir else None
        self.app = build_web_app(ctx, static_dir=static_dir)
        self._runner: web.AppRunner | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None

    @property
    def bound_port(self) -> int | None:
        """The port actually bound (``port=0`` picks a free one)."""
        if self._runner is None or not self._runner.addresses:
            return None
        return int(self._runner.addresses[0][1])

    async def start(self) -> None:
        if self.static_dir is None or not (self.static_dir / "index.html").is_file():
            log.warning("Mini App frontend not found in WEBAPP_STATIC_DIR; only /api/* is served")
        runner = web.AppRunner(
            self.app,
            access_log_class=_AccessLog,
            access_log=logging.getLogger("dumpbot.web.access"),
            shutdown_timeout=SHUTDOWN_TIMEOUT_S,
        )
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.port).start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        log.info("Mini App web server listening on %s:%s", self.host, self.bound_port)

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
            log.info("Mini App web server stopped")
