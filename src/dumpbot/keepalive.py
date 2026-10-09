"""Keep-alive self-ping for free hosts that put a service to sleep without inbound traffic (README, "Бесплатный хостинг:
Render + Supabase").

Render's free web service spins down after 15 minutes without inbound HTTP requests. Long polling only makes outbound
requests, so the bot would fall asleep (no answers, no reminders) until someone opens the Mini App. With
``KEEPALIVE_URL`` (e.g. ``https://<service>.onrender.com/healthz``) ``KeepAlive`` GETs that address every
``KEEPALIVE_INTERVAL_S``: the request leaves the container and comes back through the host's edge, which counts as
inbound traffic.

Best effort: a failed ping is logged at WARNING at most once an hour, with the exception type or the HTTP status only
(never the URL: its query may hold a secret), and the next ping comes on schedule. It is not one of the watched
background loops: it neither reports to ``ctx.watchdog`` nor can make it stop the process. ``bot.app.Lifecycle``
starts it next to the Mini App's web server and stops it at shutdown; it uses the app's shared aiohttp session.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
from collections.abc import Callable
from typing import Final
from urllib.parse import urlsplit

import aiohttp

__all__ = ["REQUEST_TIMEOUT_S", "WARN_EVERY_S", "KeepAlive", "is_internal_host", "url_host"]

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S: Final = 30.0  # well below the shortest interval (60 s)
WARN_EVERY_S: Final = 3600.0


def url_host(url: str) -> str | None:
    """The host of an ``http(s)://`` URL; None for anything else."""
    with contextlib.suppress(ValueError):
        parts = urlsplit(url.strip())
        if parts.scheme.lower() in ("http", "https") and parts.hostname:
            return parts.hostname
    return None


def is_internal_host(host: str) -> bool:
    """True for a host that a request reaches without passing the hosting's public edge, so the ping would not count
    as inbound traffic: ``localhost`` (and ``*.localhost``), a name without dots (a container or a private-network
    service, e.g. ``bot``) and an IP address that is not global (loopback, private, link-local, ``0.0.0.0``, CGNAT)."""
    name = host.strip().rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(name).is_global
    except ValueError:
        return "." not in name


class KeepAlive:
    """GET ``url`` every ``interval_s`` seconds in a background task (``start`` / ``stop``)."""

    def __init__(
        self,
        url: str,
        http: aiohttp.ClientSession,
        *,
        interval_s: float,
        timeout_s: float = REQUEST_TIMEOUT_S,
        warn_every_s: float = WARN_EVERY_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url.strip()
        self.host = url_host(self.url) or "?"
        self.interval_s = interval_s
        self._http = http
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._warn_every_s = warn_every_s
        self._now = monotonic
        self._task: asyncio.Task[None] | None = None
        self._last_warning: float | None = None
        self._failing = False
        self._answered = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start pinging (the first ping comes after one interval); a second call while running does nothing."""
        if self.running:
            return
        log.info("keep-alive is on: GET %s every %.0f s", self.host, self.interval_s)
        self._task = asyncio.create_task(self.run(), name="dumpbot-keepalive")

    async def stop(self) -> None:
        """Cancel the loop and wait for it (a ping in flight is abandoned)."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.wait([task])
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.warning("keep-alive ended with %s", type(exc).__name__)

    async def run(self) -> None:
        """Ping every ``interval_s`` until cancelled; a failure never ends the loop."""
        while True:
            await asyncio.sleep(self.interval_s)
            await self.ping()

    async def ping(self) -> bool:
        """One GET; True when the answer is below 400. Errors are logged (rate-limited), never raised."""
        try:
            async with self._http.get(self.url, timeout=self._timeout) as response:
                status = response.status
        except Exception as e:  # network errors and timeouts; cancellation passes through
            self._failed(type(e).__name__)
            return False
        if status >= 400:
            self._failed(f"HTTP {status}")
            return False
        self._succeeded()
        return True

    def _failed(self, what: str) -> None:
        self._failing = True
        now = self._now()
        if self._last_warning is not None and now - self._last_warning < self._warn_every_s:
            log.debug("keep-alive ping to %s failed: %s", self.host, what)
            return
        self._last_warning = now
        log.warning(
            "keep-alive ping to %s failed: %s; retrying every %.0f s (this warning at most once an hour)",
            self.host,
            what,
            self.interval_s,
        )

    def _succeeded(self) -> None:
        if self._failing:
            log.info("keep-alive ping to %s works again", self.host)
        elif not self._answered:
            log.info("keep-alive ping to %s answered", self.host)
        else:
            log.debug("keep-alive ping to %s answered", self.host)
        self._failing = False
        self._answered = True
