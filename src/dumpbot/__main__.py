"""Entry point: ``python -m dumpbot`` (or the ``dumpbot`` script).

logging -> Settings -> app (engine, shared HTTP session, AI clients, bot, dispatcher) -> long polling until
SIGTERM / Ctrl+C. Pending updates are never dropped: Telegram keeps them for 24 h and the raw-message table makes
redelivery idempotent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from dumpbot import logging_setup
from dumpbot.config import Settings

if TYPE_CHECKING:
    from dumpbot.bot.app import App

log = logging.getLogger("dumpbot")

EXIT_CONFIG_ERROR = 2


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


def main() -> None:
    logging_setup.setup()
    settings = load_settings()
    logging_setup.setup(settings.log_level)
    log.info("starting (owner %s)", settings.owner_id or "not set")
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:  # Ctrl+C on Windows (no signal handlers there); shutdown has already run
        log.info("interrupted")


if __name__ == "__main__":
    main()
