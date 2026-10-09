"""Alembic environment: async engine (asyncpg), schema from ``dumpbot.db.base.Base.metadata``.

Database URL, first hit wins:
1. ``sqlalchemy.url`` set programmatically on the Alembic Config (tests);
2. the ``DATABASE_URL`` environment variable (Docker Compose sets it);
3. ``DATABASE_URL`` from ``.env`` in the current directory or next to alembic.ini (local dev);
4. the default of ``dumpbot.config.Settings``.

Like ``Settings``, a ``postgres://`` / ``postgresql://`` URL (as Supabase or Render hand it out) gets the asyncpg
driver (``dumpbot.db.engine.normalize_url``).

``Settings`` is never instantiated, so migrations run without BOT_TOKEN / CHUTES_API_KEY.
A caller may also pass a ready sync connection in ``config.attributes["connection"]``.
"""

from __future__ import annotations

import asyncio
import os
import warnings
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from dumpbot.config import Settings
from dumpbot.db import models  # noqa: F401  (registers all tables on Base.metadata)
from dumpbot.db.base import Base
from dumpbot.db.engine import normalize_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Postgres stores generated-column expressions in normalized form, so autogenerate always "sees" a change
# in items.search_tsv and warns. It cannot alter generated columns anyway: change that column by hand.
warnings.filterwarnings("ignore", message=r"Computed default on .* cannot be modified")

URL_KEY = "DATABASE_URL"
CONFIGURE_OPTS = {
    "target_metadata": target_metadata,
    "compare_type": True,
    "compare_server_default": True,
}


def read_dotenv(path: Path) -> dict[str, str]:
    """Tiny .env reader: ``KEY=VALUE``, optional ``export``, quotes, ``#`` comments. Missing file -> {}."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()  # inline comment, like python-dotenv
        values[key.strip().upper()] = value
    return values


def dotenv_paths() -> list[Path]:
    paths = [Path.cwd() / ".env"]
    if config.config_file_name:
        near_ini = Path(config.config_file_name).resolve().parent / ".env"
        if near_ini not in paths:
            paths.append(near_ini)
    return paths


def database_url() -> str:
    return normalize_url(_configured_url())


def _configured_url() -> str:
    if explicit := config.get_main_option("sqlalchemy.url"):
        return explicit
    for key in (URL_KEY, URL_KEY.lower()):
        if value := os.environ.get(key):
            return value
    for path in dotenv_paths():
        if value := read_dotenv(path).get(URL_KEY):
            return value
    return Settings.model_fields["database_url"].default


def run_migrations_offline() -> None:
    """``alembic upgrade head --sql``: print SQL instead of executing it."""
    context.configure(url=database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"}, **CONFIGURE_OPTS)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **CONFIGURE_OPTS)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # The URL is passed in the dict (not via set_main_option) so "%" in passwords needs no escaping.
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        asyncio.run(run_async_migrations())
    else:
        do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
