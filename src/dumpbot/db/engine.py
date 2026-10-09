from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

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
    kwargs.setdefault("pool_size", 5)
    kwargs.setdefault("pool_pre_ping", True)
    kwargs.setdefault("connect_args", {"command_timeout": COMMAND_TIMEOUT_S})
    return create_async_engine(url, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
