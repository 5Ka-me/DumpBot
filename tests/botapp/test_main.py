"""Entry point and logging setup."""

from __future__ import annotations

import importlib
import io
import logging
import os
import socket
import subprocess
import sys
from typing import Any

import asyncpg.exceptions as asyncpg_errors
import pytest
from alembic.util import CommandError

from dumpbot import logging_setup

TOKEN = "987654321:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
ENV_KEYS = (
    "BOT_TOKEN",
    "CHUTES_API_KEY",
    "OWNER_ID",
    "DATABASE_URL",
    "LOG_LEVEL",
    "MIGRATE_ON_START",
    "KEEPALIVE_URL",
    "KEEPALIVE_INTERVAL_S",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    levels = {name: logging.getLogger(name).level for name in logging_setup.LIBRARY_LEVELS}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, lib_level in levels.items():
        logging.getLogger(name).setLevel(lib_level)


# --- __main__ ----------------------------------------------------------------------------------------------------


def test_import_without_env_does_not_start(clean_env) -> None:
    module = importlib.import_module("dumpbot.__main__")
    importlib.reload(module)
    assert callable(module.main)


def test_missing_config_exits_with_field_names_only(tmp_path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS and not k.startswith("TEST_")}
    env["BOT_TOKEN"] = TOKEN  # present, but CHUTES_API_KEY is missing: the token must not be printed
    proc = subprocess.run(
        [sys.executable, "-m", "dumpbot"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 2
    assert "invalid configuration" in proc.stderr
    assert "chutes_api_key (missing)" in proc.stderr
    assert TOKEN not in proc.stderr and TOKEN.split(":")[1] not in proc.stderr


def test_main_runs_the_app_and_handles_ctrl_c(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    import dumpbot.__main__ as entry

    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("CHUTES_API_KEY", "key")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    seen: list[Any] = []

    async def fake_run(settings: Any) -> None:
        seen.append(settings)
        raise KeyboardInterrupt

    monkeypatch.setattr(entry, "run", fake_run)
    entry.main()  # returns normally

    [settings] = seen
    assert settings.bot_token.get_secret_value() == TOKEN
    assert logging.getLogger().level == logging.WARNING


@pytest.mark.parametrize(("flag", "migrated"), [("true", True), ("false", False), (None, False)])
def test_migrate_on_start_runs_before_the_bot(clean_env, monkeypatch: pytest.MonkeyPatch, flag, migrated) -> None:
    import dumpbot.__main__ as entry

    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("CHUTES_API_KEY", "key")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    if flag is not None:
        monkeypatch.setenv("MIGRATE_ON_START", flag)
    steps: list[Any] = []

    def fake_migrate(log_level: Any, config: Any = None) -> None:
        steps.append(("migrate", log_level, config))

    async def fake_run(settings: Any) -> None:
        steps.append("run")
        raise KeyboardInterrupt

    monkeypatch.setattr(entry, "migrate", fake_migrate)
    monkeypatch.setattr(entry, "run", fake_run)
    entry.main()
    assert steps == ([("migrate", "warning", None)] if migrated else []) + ["run"]


def test_migrate_uses_alembic_ini_and_restores_our_logging(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import alembic.command

    import dumpbot.__main__ as entry

    monkeypatch.chdir(tmp_path)
    calls: list[tuple[Any, str]] = []

    def upgrade(config: Any, revision: str) -> None:
        calls.append((config.config_file_name, revision))
        root = logging.getLogger()  # what alembic.ini's fileConfig does to the root logger
        root.handlers[:] = [logging.StreamHandler(io.StringIO())]
        root.setLevel(logging.WARNING)

    monkeypatch.setattr(alembic.command, "upgrade", upgrade)
    entry.migrate("debug")

    assert calls == [("alembic.ini", "head")]
    [handler] = logging.getLogger().handlers
    assert isinstance(handler.formatter, logging_setup.RedactingFormatter)
    assert logging.getLogger().level == logging.DEBUG


STORED = "Купить лекарство от USER-PRIVATE-TEXT"  # a stored value that a migration's data step binds


def _statement_error() -> Exception:
    """What SQLAlchemy raises for a failed statement: the SQL, the bound parameters and the driver's message, which
    quotes the bad argument."""
    from sqlalchemy.exc import DBAPIError

    driver = Exception(f"invalid input for query argument $2: '{STORED}'")
    return DBAPIError("UPDATE items SET title = $1 WHERE id = $2", (STORED, STORED), driver)


def _chained(top: Exception) -> Exception:
    top.__cause__ = _statement_error()
    return top


def _fail_migrations(monkeypatch: pytest.MonkeyPatch, caplog, error: Exception) -> logging.LogRecord:
    import alembic.command
    from alembic.config import Config

    import dumpbot.__main__ as entry

    def upgrade(config: Any, revision: str) -> None:
        raise error

    events: list[str] = []
    monkeypatch.setattr(alembic.command, "upgrade", upgrade)
    # Recorded instead of applied: the real setup would drop pytest's capturing handler from the root logger.
    monkeypatch.setattr(logging_setup, "setup", lambda level=None: events.append(f"setup {level}"))
    with caplog.at_level(logging.INFO, logger="dumpbot"), pytest.raises(SystemExit) as exited:
        entry.migrate("INFO", Config())

    assert exited.value.code == entry.EXIT_MIGRATION_ERROR == 3
    assert events == ["setup INFO"]  # our logging is back before the failure is logged
    [critical] = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    return critical


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (TypeError("connect() got an unexpected keyword argument 'sslmode'"), "'sslmode'"),
        (socket.gaierror(11001, "getaddrinfo failed"), "getaddrinfo failed"),
        (ConnectionRefusedError(1225, "The remote computer refused the network connection"), "refused"),
        (TimeoutError(), "TimeoutError"),
        (asyncpg_errors.InvalidPasswordError('password authentication failed for user "postgres.ref"'), "password"),
        (asyncpg_errors.InvalidCatalogNameError('database "dump" does not exist'), "does not exist"),
        (asyncpg_errors.InternalServerError("Tenant or user not found"), "Tenant or user not found"),
        (CommandError("Can't locate revision identified by 'abc'"), "Can't locate revision"),
    ],
)
def test_a_setup_error_is_logged_in_full(monkeypatch: pytest.MonkeyPatch, caplog, error, shown) -> None:
    """Errors raised before any statement ran: their messages hold no stored data and tell a bad URL, password or
    host apart."""
    critical = _fail_migrations(monkeypatch, caplog, error)
    assert critical.getMessage() == "database migrations failed; exiting"
    assert critical.exc_info is not None and critical.exc_info[1] is error  # with the message and the traceback
    assert shown in caplog.text


@pytest.mark.parametrize(
    ("make_error", "described"),
    [
        (_statement_error, "DBAPIError (Exception)"),
        (lambda: RuntimeError(STORED), "RuntimeError"),  # anything not known to be content-free
        (lambda: KeyError(STORED), "KeyError"),
        (lambda: _chained(RuntimeError(STORED)), "RuntimeError"),
        (lambda: _chained(TypeError(STORED)), "TypeError"),  # a setup error type, but a statement failed first
        (lambda: _chained(OSError(STORED)), "OSError"),
    ],
)
def test_other_errors_never_print_their_text(monkeypatch: pytest.MonkeyPatch, caplog, make_error, described) -> None:
    """A failed statement's text carries the SQL, the bound parameters and the driver's message, which may quote a
    stored value: only the type, the driver error and the code locations reach the host's logs."""
    critical = _fail_migrations(monkeypatch, caplog, make_error())
    assert critical.exc_info is None
    message = critical.getMessage()
    assert message.startswith(f"database migrations failed: {described} at test_main.py:")
    assert "; exiting\n" in message and 'File "' in message  # the code locations are kept
    assert STORED not in caplog.text and "USER-PRIVATE" not in caplog.text


async def test_run_polls_with_resolved_updates_and_always_cleans_up(monkeypatch: pytest.MonkeyPatch, tg) -> None:
    import dumpbot.__main__ as entry
    from dumpbot.bot import app as app_module
    from dumpbot.config import Settings

    settings = Settings(_env_file=None, bot_token="123456:TEST", chutes_api_key="k", owner_id=1)  # type: ignore[call-arg]
    built: list[Any] = []
    polls: list[dict[str, Any]] = []
    real_build = app_module.build_app

    async def build(s: Settings, **kwargs: Any) -> Any:
        app = await real_build(s, bot_session=tg, runners=lambda ctx: {}, exit_func=lambda code: None)

        async def start_polling(*bots: Any, **kw: Any) -> None:
            polls.append({"bots": bots, **kw})
            raise RuntimeError("network down")

        app.dp.start_polling = start_polling  # type: ignore[method-assign]
        built.append(app)
        return app

    monkeypatch.setattr(app_module, "build_app", build)
    with pytest.raises(RuntimeError):
        await entry.run(settings)

    [app] = built
    [poll] = polls
    assert poll["bots"] == (app.bot,)
    assert sorted(poll["allowed_updates"]) == ["callback_query", "message"]
    assert "drop_pending_updates" not in poll
    assert app.ctx.ai._http.closed and tg.closed


# --- logging_setup -----------------------------------------------------------------------------------------------


def test_setup_levels() -> None:
    logging_setup.setup("debug")
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("aiogram.event").level == logging.INFO
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING

    logging_setup.setup("ERROR")
    assert logging.getLogger("aiogram.event").level == logging.ERROR
    assert len(logging.getLogger().handlers) == 1  # replaced, not stacked


@pytest.mark.parametrize(("value", "level"), [("info", 20), ("WARNING", 30), (10, 10), ("nonsense", 20), (None, 20)])
def test_parse_level(value: Any, level: int) -> None:
    assert logging_setup.parse_level(value) == level


def test_format_is_one_line_and_scrubs_tokens() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging_setup.RedactingFormatter(logging_setup.FORMAT, logging_setup.DATE_FORMAT))
    logger = logging.getLogger("dumpbot.test.redact")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info("GET https://api.telegram.org/bot%s/getFile failed", TOKEN)
        logger.info("token %s leaked", TOKEN)
    finally:
        logger.removeHandler(handler)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert TOKEN.split(":")[1] not in stream.getvalue()
    assert "https://api.telegram.org/bot<token>/getFile" in lines[0]
    assert "INFO" in lines[0] and "dumpbot.test.redact:" in lines[0]
    assert lines[0][:4].isdigit()  # starts with the timestamp


def test_redact_keeps_ordinary_text() -> None:
    assert logging_setup.redact("job 12 at 10:30, item 123:456") == "job 12 at 10:30, item 123:456"
