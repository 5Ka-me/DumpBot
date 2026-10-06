from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# Every statement is bounded (asyncpg raises TimeoutError): a lock held by another client (an open DBeaver
# transaction) or a half-open socket fails one step of a background loop instead of hanging it forever.
# Our own statements take milliseconds.
COMMAND_TIMEOUT_S = 60.0


def make_engine(url: str, **kwargs) -> AsyncEngine:
    kwargs.setdefault("pool_size", 5)
    kwargs.setdefault("pool_pre_ping", True)
    kwargs.setdefault("connect_args", {"command_timeout": COMMAND_TIMEOUT_S})
    return create_async_engine(url, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
