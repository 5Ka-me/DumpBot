"""db.engine: every statement of the app's engine is bounded, so a DB wait can never hang a loop forever; database URLs
as hosts hand them out get the asyncpg driver."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from dumpbot.db import engine as engine_module
from dumpbot.db.engine import make_engine, normalize_url


async def test_statements_time_out(db_url, monkeypatch) -> None:
    monkeypatch.setattr(engine_module, "COMMAND_TIMEOUT_S", 0.3)  # 60 s in production
    eng = make_engine(db_url)
    try:
        async with eng.connect() as conn:
            with pytest.raises(TimeoutError):
                await conn.execute(text("SELECT pg_sleep(5)"))
        async with eng.connect() as conn:  # the pool still works afterwards
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await eng.dispose()


# --- normalize_url -----------------------------------------------------------------------------------------------

ASYNC = "postgresql+asyncpg://"
# What Supabase's "Connect" dialog hands out (session pooler), with a %-encoded password and query parameters.
TAIL = "postgres.abcdefghijkl:p%40ss%3Aw%2Frd%25@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?ssl=require&x=1"


@pytest.mark.parametrize("scheme", ["postgres://", "postgresql://", "postgresql+psycopg2://", "PostgreSQL://"])
def test_sync_urls_get_the_asyncpg_driver(scheme: str) -> None:
    assert normalize_url(scheme + TAIL) == ASYNC + TAIL


@pytest.mark.parametrize(
    "url",
    [
        ASYNC + TAIL,
        "postgresql+asyncpg://dump:dump@127.0.0.1:5432/dump",
        "postgresql+psycopg://u:p@h/db",  # another explicit driver is the user's choice
        "sqlite+aiosqlite:///x.db",
        "not a url",
        "",
    ],
)
def test_other_urls_are_left_alone(url: str) -> None:
    assert normalize_url(url) == url


def test_surrounding_whitespace_of_a_pasted_url_is_dropped() -> None:
    assert normalize_url(f"  postgresql://{TAIL}\n") == ASYNC + TAIL
