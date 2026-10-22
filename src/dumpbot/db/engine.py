from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from dumpbot.db import meter

# Every statement is bounded (asyncpg raises TimeoutError): a lock held by another client (an open DBeaver
# transaction) or a half-open socket fails one step of a background loop instead of hanging it forever.
# Our own statements take milliseconds.
COMMAND_TIMEOUT_S = 60.0

ASYNC_SCHEME: Final = "postgresql+asyncpg"
# Schemes of URLs as hosts hand them out (Supabase, Render, Heroku-style "postgres://"): SQLAlchemy would pick the
# sync psycopg2 driver for them, which is not installed and cannot run under asyncio anyway.
_SYNC_SCHEMES: Final = frozenset({"postgres", "postgresql", "postgresql+psycopg2"})


def normalize_url(url: str) -> str:
    """``postgres://``, ``postgresql://`` and ``postgresql+psycopg2://`` -> ``postgresql+asyncpg://``.

    Only the scheme changes: user, %-encoded password, host, port, database and query parameters stay exactly as
    given. Any other URL (already asyncpg, another database) is returned unchanged."""
    stripped = url.strip()
    scheme, sep, rest = stripped.partition("://")
    if sep and scheme.lower() in _SYNC_SCHEMES:
        return f"{ASYNC_SCHEME}://{rest}"
    return url


def make_engine(url: str, **kwargs) -> AsyncEngine:
    """The app's engine. Every round trip to the database counts (docs/features/iteration-4.md §7: ~150 ms from
    Render to Supabase), so:

    - no ``pool_pre_ping`` (three round trips on every checkout): ``meter.idle_ping`` pings only a connection that
      sat idle for a while, the one the pooler or the network may have dropped;
    - the pool hands out the connection used last (LIFO): it is the one that was surely alive a moment ago, and its
      prepared statements are cached (a statement new to a connection costs a second round trip);
    - ``meter.install``: every job logs how many calls it made and how long they took.
    """
    kwargs.setdefault("pool_size", 5)
    kwargs.setdefault("pool_use_lifo", True)
    kwargs.setdefault("connect_args", {"command_timeout": COMMAND_TIMEOUT_S})
    engine = create_async_engine(url, **kwargs)
    if not kwargs.get("pool_pre_ping"):
        meter.idle_ping(engine)
    meter.install(engine)
    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def autocommit_sessionmaker(sessionmaker: async_sessionmaker[AsyncSession]) -> async_sessionmaker[AsyncSession]:
    """Sessions on the same engine whose every statement commits on its own: no ``BEGIN`` / ``COMMIT`` /
    ``ROLLBACK`` round trips around it. For reads and for a single write that needs no transaction of its own
    (``commit()`` there is a no-op); several writes that must succeed together still belong in a ``sessionmaker``
    session. Falls back to ``sessionmaker`` itself when it is not bound to an engine."""
    bind = (getattr(sessionmaker, "kw", None) or {}).get("bind")
    if not isinstance(bind, AsyncEngine):
        return sessionmaker
    return async_sessionmaker(bind.execution_options(isolation_level="AUTOCOMMIT"), expire_on_commit=False)
