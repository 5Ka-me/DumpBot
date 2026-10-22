"""Where a job's time goes (docs/features/iteration-4.md §7): database calls, AI calls and the whole job.

``install(engine)`` makes the engine report every call that reaches the database to the ``Meter`` of the running
task: SQL statements, the ``BEGIN`` that asyncpg sends before the first statement of a transaction, commits,
rollbacks and the checkout pings of ``idle_ping``. The meter is a context variable, so a job counts only its own
calls (and those of the tasks it starts), not those of the bot's handlers, the scheduler or the Mini App running
meanwhile; without a meter nothing is counted.

``measure()`` opens a meter (the job worker: one per job), ``ai()`` marks a stretch of AI work (an LLM or STT call),
whose database calls do not count as AI time. ``Meter.summary()`` is the INFO line of every job:
``ai 1.92 s, db 7 queries / 0.31 s, total 2.40 s`` — numbers only, never a statement's text or parameters. The
database time is wall time with at least one call under way: calls made at once on two connections count once.

``idle_ping`` replaces SQLAlchemy's ``pool_pre_ping`` (a ``BEGIN`` / ``SELECT`` / ``ROLLBACK`` on every checkout:
three round trips, ~0.5 s per session at 150 ms to the database): a pooled connection is checked with one
``SELECT 1`` only when it sat idle longer than ``IDLE_PING_S`` — the case pre-ping is for (the pooler or the
network dropped it meanwhile). A failed ping makes the pool replace the connection, as pre-ping does.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Final, NamedTuple

from sqlalchemy import event, exc
from sqlalchemy.engine import Connection, Dialect
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.util import await_only

__all__ = ["IDLE_PING_S", "PING_TIMEOUT_S", "Call", "Meter", "ai", "current", "idle_ping", "install", "measure"]

log = logging.getLogger(__name__)

IDLE_PING_S: Final = 30.0  # a pooled connection unused for longer is pinged before use
PING_TIMEOUT_S: Final = 10.0
_IDLE_SINCE: Final = "dumpbot_idle_since"  # connection_record.info key: when it was last returned to the pool
_STARTED: Final = "dumpbot_meter_t0"  # Connection.info key: when the statement running on it started
_INSTALLED: Final = "dumpbot_meter"


class Call(NamedTuple):
    """One database call of a traced meter: what it was ("SELECT", "INSERT items", "BEGIN", "COMMIT", "PING"…) and
    when it started and ended (``time.perf_counter``)."""

    what: str
    start: float
    end: float


@dataclass
class Meter:
    """Database calls and AI time of one job (see the module docstring). ``trace`` (when asked for) keeps every call
    in order, for tests."""

    queries: int = 0
    db_s: float = 0.0
    ai_s: float = 0.0
    started: float = field(default_factory=time.perf_counter)
    trace: list[Call] | None = None
    _running: int = 0
    _busy_since: float = 0.0

    @property
    def total_s(self) -> float:
        return time.perf_counter() - self.started

    def summary(self) -> str:
        """``ai X s, db Y queries / Z s, total T s``."""
        return f"ai {self.ai_s:.2f} s, db {self.queries} queries / {self.db_s:.2f} s, total {self.total_s:.2f} s"

    def begin(self) -> float:
        """A call starts; returns its start time (for ``end``)."""
        now = time.perf_counter()
        if self._running == 0:
            self._busy_since = now
        self._running += 1
        return now

    def end(self, what: str, start: float) -> None:
        now = time.perf_counter()
        self._running = max(0, self._running - 1)
        if self._running == 0:
            self.db_s += now - self._busy_since
        self.queries += 1
        if self.trace is not None:
            self.trace.append(Call(what, start, now))

    def extra(self, what: str) -> None:
        """A round trip inside a call being timed already (the ``BEGIN`` before a statement)."""
        now = time.perf_counter()
        self.queries += 1
        if self.trace is not None:
            self.trace.append(Call(what, now, now))


_current: ContextVar[Meter | None] = ContextVar("dumpbot_meter", default=None)


def current() -> Meter | None:
    """The meter of the running task, if any."""
    return _current.get()


@contextmanager
def measure(*, trace: bool = False) -> Iterator[Meter]:
    """A new meter for the code inside (and the tasks it starts)."""
    meter = Meter(trace=[] if trace else None)
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)


@contextmanager
def ai() -> Iterator[None]:
    """AI work (an LLM or STT call): its wall time minus the database time inside it goes to ``ai_s``."""
    meter = _current.get()
    if meter is None:
        yield
        return
    t0, db0 = time.perf_counter(), meter.db_s
    try:
        yield
    finally:
        meter.ai_s += max(0.0, time.perf_counter() - t0 - (meter.db_s - db0))


# --- engine hooks ------------------------------------------------------------------------------------------------


def install(engine: AsyncEngine) -> None:
    """Report the engine's database calls to the current meter (once per engine)."""
    sync = engine.sync_engine
    if sync.dialect.__dict__.get(_INSTALLED):
        return
    event.listen(sync, "before_cursor_execute", _before_statement)
    event.listen(sync, "after_cursor_execute", _after_statement)
    event.listen(sync, "handle_error", _failed_statement)
    _time_dialect(sync.dialect)
    sync.dialect.__dict__[_INSTALLED] = True


def _before_statement(conn: Connection, cursor: Any, statement: str, *args: Any) -> None:
    meter = _current.get()
    if meter is None:
        return
    conn.info[_STARTED] = meter.begin()
    # asyncpg's adapter sends the transaction's BEGIN right before its first statement: one more round trip, timed
    # with the statement.
    adapter = conn.connection.dbapi_connection
    if getattr(adapter, "_started", True) is False and getattr(adapter, "isolation_level", None) != "autocommit":
        meter.extra("BEGIN")


def _after_statement(conn: Connection, cursor: Any, statement: str, *args: Any) -> None:
    _statement_done(conn, statement)


def _failed_statement(context: Any) -> None:
    conn = getattr(context, "connection", None)
    if conn is not None:
        _statement_done(conn, getattr(context, "statement", None) or "")


def _statement_done(conn: Connection, statement: str) -> None:
    start = conn.info.pop(_STARTED, None)
    meter = _current.get()
    if meter is not None and start is not None:
        meter.end(_kind(statement), start)


def _kind(statement: str) -> str:
    """ "SELECT", "INSERT items", "UPDATE raw_messages"…: what a call was, without its text."""
    words = statement.split(None, 3)
    if not words:
        return "?"
    head = words[0].upper()
    if head == "INSERT" and len(words) >= 3:
        return f"INSERT {words[2]}"
    if head == "UPDATE" and len(words) >= 2:
        return f"UPDATE {words[1]}"
    if head == "DELETE" and len(words) >= 3:
        return f"DELETE {words[2]}"
    return head


def _time_dialect(dialect: Dialect) -> None:
    """Commits and rollbacks that reach the database (asyncpg's adapter sends nothing for a connection without a
    transaction: a pool reset after a commit, a session in autocommit)."""
    commit, rollback = dialect.do_commit, dialect.do_rollback

    def do_commit(dbapi_connection: Any) -> None:
        _timed("COMMIT", dbapi_connection, commit)

    def do_rollback(dbapi_connection: Any) -> None:
        _timed("ROLLBACK", dbapi_connection, rollback)

    dialect.do_commit = do_commit  # type: ignore[method-assign]
    dialect.do_rollback = do_rollback  # type: ignore[method-assign]


def _timed(what: str, dbapi_connection: Any, call: Callable[[Any], None]) -> None:
    meter = _current.get()
    if meter is None or not getattr(dbapi_connection, "_started", True):
        call(dbapi_connection)
        return
    start = meter.begin()
    try:
        call(dbapi_connection)
    finally:
        meter.end(what, start)


# --- checkout ping of idle connections ---------------------------------------------------------------------------


def idle_ping(engine: AsyncEngine, *, idle_s: float = IDLE_PING_S) -> None:
    """Ping a pooled connection on checkout only when it was idle longer than ``idle_s`` (see the module docstring).
    For engines made without ``pool_pre_ping``."""
    pool = engine.sync_engine.pool

    def connected(dbapi_connection: Any, record: Any) -> None:
        record.info[_IDLE_SINCE] = time.perf_counter()

    def checked_in(dbapi_connection: Any, record: Any) -> None:
        if record is not None:
            record.info[_IDLE_SINCE] = time.perf_counter()

    def checked_out(dbapi_connection: Any, record: Any, proxy: Any) -> None:
        since = record.info.get(_IDLE_SINCE)
        if since is not None and time.perf_counter() - since <= idle_s:
            return
        try:
            _ping(record.driver_connection)
        except Exception as e:
            idle = time.perf_counter() - since if since is not None else 0.0
            log.info("pooled connection idle for %.0f s is gone (%s), reconnecting", idle, type(e).__name__)
            raise exc.DisconnectionError() from e

    event.listen(pool, "connect", connected)
    event.listen(pool, "checkin", checked_in)
    event.listen(pool, "checkout", checked_out)


def _ping(driver_connection: Any) -> None:
    """``SELECT 1`` by the simple query protocol: one round trip, no prepared statement, no transaction."""
    meter = _current.get()
    start = meter.begin() if meter is not None else 0.0
    try:
        await_only(driver_connection.execute("SELECT 1", timeout=PING_TIMEOUT_S))
    finally:
        if meter is not None:
            meter.end("PING", start)
