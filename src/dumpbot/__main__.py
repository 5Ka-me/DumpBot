"""Entry point: ``python -m dumpbot`` (or the ``dumpbot`` script).

logging -> Settings -> (with ``MIGRATE_ON_START``: ``alembic upgrade head``) -> app (engine, shared HTTP session, AI
clients, bot, dispatcher) -> long polling until SIGTERM / Ctrl+C. Pending updates are never dropped: Telegram keeps
them for 24 h and the raw-message table makes redelivery idempotent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from dumpbot import logging_setup
from dumpbot.config import Settings

if TYPE_CHECKING:
    from alembic.config import Config

    from dumpbot.bot.app import App

log = logging.getLogger("dumpbot")

EXIT_CONFIG_ERROR = 2
EXIT_MIGRATION_ERROR = 3
ALEMBIC_INI = "alembic.ini"  # in the working directory: /app in the image, next to migrations/


def load_settings() -> Settings:
    """Settings from env / .env; on errors log only the field names (values may be secrets) and exit 2."""
    try:
        return Settings()  # type: ignore[call-arg]  # required fields come from the environment
    except ValidationError as e:
        fields = ", ".join(f"{'.'.join(map(str, err['loc'])) or '?'} ({err['type']})" for err in e.errors())
        log.critical("invalid configuration: %s. See .env.example.", fields)
        raise SystemExit(EXIT_CONFIG_ERROR) from None


async def poll(app: App, **polling: Any) -> None:
    """Long polling until stopped. Pending updates are never dropped. ``polling`` is passed on to
    ``Dispatcher.start_polling`` (the end-to-end tests use ``handle_signals=False`` and a short timeout)."""
    await app.dp.start_polling(app.bot, allowed_updates=app.dp.resolve_used_update_types(), **polling)


async def run(settings: Settings) -> None:
    """Build the app and poll until stopped; resources are closed even if startup fails."""
    from dumpbot.bot.app import build_app  # heavy imports only when actually running

    app = await build_app(settings)
    try:
        await poll(app)
    finally:
        await app.cleanup()


def migrate(log_level: str | int | None, config: Config | None = None) -> None:
    """``alembic upgrade head`` in-process (``MIGRATE_ON_START``, for hosts without a separate migrate step). The URL
    comes from ``DATABASE_URL`` as for the ``alembic`` CLI (migrations/env.py). ``config`` defaults to ``alembic.ini``
    in the working directory; its logging section replaces ours, so ours is set up again afterwards. A failure is
    logged (``log_migration_failure``) and exits with ``EXIT_MIGRATION_ERROR``: the host shows the error and restarts
    the service instead of running a bot without its tables."""
    from alembic import command
    from alembic.config import Config

    log.info("applying database migrations (alembic upgrade head)")
    try:
        command.upgrade(config if config is not None else Config(ALEMBIC_INI), "head")
    except Exception as e:
        logging_setup.setup(log_level)
        log_migration_failure(e)
        raise SystemExit(EXIT_MIGRATION_ERROR) from None
    logging_setup.setup(log_level)
    log.info("database migrations are up to date")


def log_migration_failure(e: Exception) -> None:
    """A setup error (``is_setup_error``) is logged with its message and traceback: that message is what tells a bad
    URL, password or host apart. Anything else gets only the error type, the driver error and the code locations
    (``describe_error``, ``safe_traceback``), as everywhere else in the bot: a failed statement's text carries the SQL
    with its bound parameters, and the driver's message may quote a stored value (a data step binds item titles)."""
    if is_setup_error(e):
        log.critical("database migrations failed; exiting", exc_info=e)
        return
    from dumpbot.workers.jobs import describe_error, safe_traceback

    log.critical("database migrations failed: %s; exiting\n%s", describe_error(e), safe_traceback(e))


def is_setup_error(e: BaseException) -> bool:
    """True for errors raised before any statement ran, whose messages hold no stored data: Alembic's own (a missing
    revision), ``TypeError`` (an option the driver does not know, e.g. ``?sslmode=``), network errors (unknown host,
    refused connection, timeout) and the driver's connection errors (wrong password, missing database, the pooler's
    "Tenant or user not found"), which SQLAlchemy passes on unwrapped. Never when a SQLAlchemy statement error is
    anywhere in the chain."""
    from alembic.util import CommandError
    from asyncpg.exceptions import InterfaceError, PostgresError
    from sqlalchemy.exc import StatementError

    seen: list[BaseException] = []
    link: BaseException | None = e
    while link is not None and all(link is not s for s in seen):
        if isinstance(link, StatementError):
            return False
        seen.append(link)
        link = link.__cause__ or link.__context__
    return isinstance(e, CommandError | TypeError | OSError | PostgresError | InterfaceError)


def main() -> None:
    logging_setup.setup()
    settings = load_settings()
    logging_setup.setup(settings.log_level)
    if settings.migrate_on_start:
        migrate(settings.log_level)
    log.info(
        "starting (owner %s, local Whisper %s)", settings.owner_id or "not set", settings.local_stt_url.strip() or "off"
    )
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:  # Ctrl+C on Windows (no signal handlers there); shutdown has already run
        log.info("interrupted")


if __name__ == "__main__":
    main()
