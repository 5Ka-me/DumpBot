"""db.engine: every statement of the app's engine is bounded, so a DB wait can never hang a loop forever."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from dumpbot.db import engine as engine_module
from dumpbot.db.engine import make_engine


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
