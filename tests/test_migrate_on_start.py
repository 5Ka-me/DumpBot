"""MIGRATE_ON_START for real (``dumpbot.__main__.migrate``) on a scratch database of the test Postgres
(``<test db>_migstart``, created and dropped here; skipped without ``TEST_DATABASE_URL``).

The database URL is given as hosts hand it out: ``postgres://`` / ``postgresql://`` without a driver, with a password
that needs %-encoding. migrations/env.py must give it the asyncpg driver and keep the password intact. In a
subprocess laid out like the image (alembic.ini and migrations/ in the working directory) the run ends with our own
log format again (alembic.ini's logging config is replaced back); a failure exits with code 3 and a traceback.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from dumpbot import logging_setup

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "p@ss:w/rd%1 #?"  # every character that must be %-encoded in a URL
OUR_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (\w+) +dumpbot: (.+)$", re.MULTILINE)


@dataclass
class Scratch:
    host: str
    port: int
    database: str
    role: str

    def url(self, scheme: str, database: str | None = None) -> str:
        """A URL as Supabase / Render show it: no driver, the password %-encoded."""
        return f"{scheme}://{self.role}:{quote(PASSWORD, safe='')}@{self.host}:{self.port}/{database or self.database}"

    def asyncpg_url(self) -> str:
        return self.url("postgresql+asyncpg")


async def _admin(url: str, *statements: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture
def scratch() -> Iterator[Scratch]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    parsed = make_url(url)
    if parsed.host == "localhost":
        parsed = parsed.set(host="127.0.0.1")
    name = f"{parsed.database}_migstart"
    admin = parsed.render_as_string(hide_password=False)
    drop = (f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', f'DROP ROLE IF EXISTS "{name}"')
    asyncio.run(
        _admin(
            admin,
            *drop,
            f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{PASSWORD}'",
            f'CREATE DATABASE "{name}" OWNER "{name}"',
        )
    )
    try:
        yield Scratch(host=parsed.host or "127.0.0.1", port=parsed.port or 5432, database=name, role=name)
    finally:
        asyncio.run(_admin(admin, *drop))


async def _scalars(url: str, statement: str) -> list[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return [row[0] for row in await conn.execute(text(statement))]
    finally:
        await engine.dispose()


def _head() -> str:
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head
    return head


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_migrate_takes_the_url_as_hosts_give_it(scratch: Scratch, scheme: str, monkeypatch, tmp_path) -> None:
    import dumpbot.__main__ as entry

    monkeypatch.chdir(tmp_path)  # no .env, no alembic.ini here
    monkeypatch.setenv("DATABASE_URL", scratch.url(scheme))
    monkeypatch.setattr(logging_setup, "setup", lambda level=None: None)  # keep the test run's logging
    cfg = Config()  # no ini file: no logging config either
    cfg.set_main_option("script_location", str(ROOT / "migrations"))

    entry.migrate("INFO", cfg)
    entry.migrate("INFO", cfg)  # already at head: nothing to do

    url = scratch.asyncpg_url()
    assert asyncio.run(_scalars(url, "SELECT version_num FROM alembic_version")) == [_head()]
    tables = asyncio.run(_scalars(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    assert {"items", "jobs", "raw_messages", "app_state"} <= set(tables)


def _image_layout(tmp_path: Path) -> Path:
    """/app of the image: alembic.ini and migrations/ in the working directory."""
    app = tmp_path / "app"
    app.mkdir()
    shutil.copy(ROOT / "alembic.ini", app / "alembic.ini")
    shutil.copytree(ROOT / "migrations", app / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    return app


def _run_migrate(app: Path, database_url: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "LOG_LEVEL") and not k.startswith("TEST_")}
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-c", "from dumpbot.__main__ import migrate; migrate('INFO')"],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_migrate_in_the_image_layout_restores_our_logging(scratch: Scratch, tmp_path) -> None:
    proc = _run_migrate(_image_layout(tmp_path), scratch.url("postgresql"))
    assert proc.returncode == 0, proc.stderr
    assert "Running upgrade" in proc.stderr  # alembic's own log lines, from alembic.ini's logging config
    ours = OUR_LINE.findall(proc.stderr)
    assert ours[-1] == ("INFO", "database migrations are up to date")  # our format again after alembic
    assert PASSWORD not in proc.stderr and quote(PASSWORD, safe="") not in proc.stderr
    versions = asyncio.run(_scalars(scratch.asyncpg_url(), "SELECT version_num FROM alembic_version"))
    assert versions == [_head()]


def test_failed_migrations_exit_with_code_3(scratch: Scratch, tmp_path) -> None:
    proc = _run_migrate(_image_layout(tmp_path), scratch.url("postgres", database=f"{scratch.database}_missing"))
    assert proc.returncode == 3
    assert ("CRITICAL", "database migrations failed; exiting") in OUR_LINE.findall(proc.stderr)
    assert "Traceback (most recent call last)" in proc.stderr
    assert PASSWORD not in proc.stderr and quote(PASSWORD, safe="") not in proc.stderr


def test_the_image_has_alembic_ini_in_its_working_directory() -> None:
    """``migrate()`` reads ``alembic.ini`` from the working directory: the runtime stage must put it (and migrations/)
    in its WORKDIR, and start the bot with ``python -m dumpbot`` (Render's Docker Command stays empty)."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    runtime = dockerfile[dockerfile.index("AS runtime") :]
    assert "WORKDIR /app\n" in runtime
    assert "COPY alembic.ini ./\n" in runtime and "COPY migrations ./migrations\n" in runtime
    assert runtime.rstrip().endswith('CMD ["python", "-m", "dumpbot"]')
