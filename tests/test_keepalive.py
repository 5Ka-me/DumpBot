"""Keep-alive self-ping (``dumpbot.keepalive``): GETs the URL every interval against a local HTTP server (no outside
requests); failures are logged at WARNING at most once an hour with the error type or HTTP status only, never the URL's
query; ``stop`` cancels the loop, even with a ping in flight."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import AsyncIterator, Callable

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from dumpbot.keepalive import KeepAlive, url_host

SECRET = "s3cr3t-ping-key"
LOGGER = "dumpbot.keepalive"


class Target:
    """A local server that counts the pings; ``status`` and ``hold`` shape the answers."""

    def __init__(self) -> None:
        self.hits = 0
        self.status = 200
        self.hold = False  # True: answer only after ``release`` (a ping in flight)
        self.release = asyncio.Event()
        self.port = 0

    async def handle(self, request: web.Request) -> web.Response:
        self.hits += 1
        if self.hold:
            await self.release.wait()
        return web.Response(status=self.status, text="ok")

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/healthz?key={SECRET}"


@pytest.fixture
async def target() -> AsyncIterator[Target]:
    t = Target()
    app = web.Application()
    app.router.add_get("/healthz", t.handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    t.port = server.port or 0
    try:
        yield t
    finally:
        t.release.set()
        await server.close()


@pytest.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _until(condition: Callable[[], bool], limit_s: float = 5.0) -> None:
    deadline = time.monotonic() + limit_s
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://dump-bot.onrender.com/healthz?key=1", "dump-bot.onrender.com"),
        (" http://127.0.0.1:8080/healthz ", "127.0.0.1"),
        ("dump-bot.onrender.com/healthz", None),
        ("ftp://dump-bot.onrender.com/", None),
        ("https://", None),
        ("http://[::1", None),
        ("", None),
    ],
)
def test_url_host(url: str, host: str | None) -> None:
    assert url_host(url) == host


async def test_a_ping_that_answers(target: Target, http, caplog) -> None:
    keepalive = KeepAlive(target.url(), http, interval_s=600)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert await keepalive.ping() is True
        assert await keepalive.ping() is True
    assert target.hits == 2
    infos = [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]
    assert infos == ["keep-alive ping to 127.0.0.1 answered"]  # once; later answers are DEBUG
    assert SECRET not in caplog.text


async def test_failures_are_warned_at_most_once_an_hour(target: Target, http, caplog) -> None:
    clock = Clock()
    keepalive = KeepAlive(target.url(), http, interval_s=600, monotonic=clock)
    target.status = 503
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert await keepalive.ping() is False
        clock.now += 600
        assert await keepalive.ping() is False  # within the hour: DEBUG only
        assert len(_warnings(caplog)) == 1
        clock.now += 3000
        assert await keepalive.ping() is False  # an hour after the first warning
        target.status = 200
        assert await keepalive.ping() is True

    warnings = _warnings(caplog)
    assert len(warnings) == 2
    assert warnings[0] == (
        "keep-alive ping to 127.0.0.1 failed: HTTP 503; retrying every 600 s (this warning at most once an hour)"
    )
    assert "keep-alive ping to 127.0.0.1 works again" in caplog.text
    assert SECRET not in caplog.text and "/healthz" not in caplog.text and str(target.port) not in caplog.text


async def test_network_errors_name_the_exception_type_only(http, caplog) -> None:
    with socket.socket() as probe:  # a port nobody listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    keepalive = KeepAlive(f"http://127.0.0.1:{port}/healthz?key={SECRET}", http, interval_s=60)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert await keepalive.ping() is False
    [warning] = _warnings(caplog)
    assert warning.startswith("keep-alive ping to 127.0.0.1 failed: ClientConnectorError;")
    assert SECRET not in caplog.text and str(port) not in caplog.text


async def test_a_timeout_is_a_failure(target: Target, http, caplog) -> None:
    target.hold = True
    keepalive = KeepAlive(target.url(), http, interval_s=60, timeout_s=0.05)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert await keepalive.ping() is False
    [warning] = _warnings(caplog)
    assert "failed: " in warning and "Timeout" in warning


async def test_the_loop_pings_every_interval_until_stopped(target: Target, http) -> None:
    keepalive = KeepAlive(target.url(), http, interval_s=0.02)
    keepalive.start()
    keepalive.start()  # already running: no second loop
    assert keepalive.running
    await _until(lambda: target.hits >= 3)

    await keepalive.stop()
    assert not keepalive.running
    hits = target.hits
    await asyncio.sleep(0.1)
    assert target.hits == hits
    await keepalive.stop()  # twice is fine


async def test_the_first_ping_comes_after_one_interval(target: Target, http, caplog) -> None:
    keepalive = KeepAlive(target.url(), http, interval_s=60)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        keepalive.start()
        await asyncio.sleep(0.1)
        await keepalive.stop()
    assert target.hits == 0
    assert "keep-alive is on: GET 127.0.0.1 every 60 s" in caplog.text
    assert SECRET not in caplog.text


async def test_stop_abandons_a_ping_in_flight(target: Target, http, caplog) -> None:
    target.hold = True
    keepalive = KeepAlive(target.url(), http, interval_s=0.01)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        keepalive.start()
        await _until(lambda: target.hits == 1)
        await asyncio.wait_for(keepalive.stop(), timeout=2)
    assert not keepalive.running
    assert _warnings(caplog) == []  # cancellation is not a failure
