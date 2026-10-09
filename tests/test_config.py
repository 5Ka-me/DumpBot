"""Settings: DATABASE_URL as hosts hand it out gets the asyncpg driver; the free-hosting switches (MIGRATE_ON_START,
KEEPALIVE_URL / KEEPALIVE_INTERVAL_S) and their limits."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from dumpbot.config import KEEPALIVE_MAX_S, KEEPALIVE_MIN_S, Settings

RENDER_IDLE_S = 15 * 60  # Render's free web service spins down after 15 minutes without inbound requests
SUPABASE = "postgres.abcdefghijkl:p%40ss%25word@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?ssl=require"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("DATABASE_URL", "MIGRATE_ON_START", "KEEPALIVE_URL", "KEEPALIVE_INTERVAL_S"):
        monkeypatch.delenv(key, raising=False)


def make(**overrides: Any) -> Settings:
    return Settings(_env_file=None, bot_token="1:x", chutes_api_key="k", **overrides)  # type: ignore[call-arg]


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg2"])
def test_database_url_gets_the_asyncpg_driver(scheme: str) -> None:
    assert make(database_url=f"{scheme}://{SUPABASE}").database_url == f"postgresql+asyncpg://{SUPABASE}"


def test_database_url_from_the_environment_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"postgresql://{SUPABASE}")
    assert make().database_url == f"postgresql+asyncpg://{SUPABASE}"


def test_default_and_asyncpg_urls_are_kept() -> None:
    assert make().database_url == "postgresql+asyncpg://dump:dump@127.0.0.1:5432/dump"
    assert make(database_url=f"postgresql+asyncpg://{SUPABASE}").database_url == f"postgresql+asyncpg://{SUPABASE}"


def test_free_hosting_switches_are_off_by_default() -> None:
    settings = make()
    assert settings.migrate_on_start is False
    assert settings.keepalive_url == ""
    assert settings.keepalive_interval_s == 600


def test_free_hosting_switches_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIGRATE_ON_START", "true")
    monkeypatch.setenv("KEEPALIVE_URL", "https://dump-bot.onrender.com/healthz")
    monkeypatch.setenv("KEEPALIVE_INTERVAL_S", "300")
    settings = make()
    assert settings.migrate_on_start is True
    assert settings.keepalive_url == "https://dump-bot.onrender.com/healthz"
    assert settings.keepalive_interval_s == 300


def test_keepalive_interval_always_beats_the_idle_limit() -> None:
    assert KEEPALIVE_MAX_S < RENDER_IDLE_S
    assert make(keepalive_interval_s=KEEPALIVE_MIN_S).keepalive_interval_s == 60
    assert make(keepalive_interval_s=KEEPALIVE_MAX_S).keepalive_interval_s == 840
    for bad in (0, 59.9, 840.1, RENDER_IDLE_S):
        with pytest.raises(ValidationError) as caught:
            make(keepalive_interval_s=bad)
        assert [err["loc"] for err in caught.value.errors()] == [("keepalive_interval_s",)]
