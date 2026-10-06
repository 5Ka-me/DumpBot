"""stdlib logging: one line per record (time, level, logger, message) on stderr.

Privacy: message text, transcripts and OCR are never logged at INFO or above (each module follows that rule);
as a second safeguard the formatter scrubs anything that looks like a bot token (``bot123:ABC…`` in Telegram
URLs, a bare ``123:ABC…`` token), including inside tracebacks.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Final

FORMAT: Final = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT: Final = "%Y-%m-%d %H:%M:%S"

# Third-party loggers and their most verbose level (aiogram.event logs one line per update, no content).
LIBRARY_LEVELS: Final[dict[str, int]] = {
    "aiogram.event": logging.INFO,
    "sqlalchemy.engine": logging.WARNING,
    "asyncio": logging.WARNING,
}

_TOKEN_RE: Final = re.compile(r"\b(bot)?\d{5,}:[A-Za-z0-9_-]{30,}")


def redact(text: str) -> str:
    """Replace bot tokens (alone or inside a Telegram API URL) with a placeholder."""
    return _TOKEN_RE.sub(lambda m: f"{m.group(1) or ''}<token>", text)


class RedactingFormatter(logging.Formatter):
    """Formats as usual, then scrubs bot tokens from the whole line (message, exception and stack text)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def parse_level(level: str | int | None) -> int:
    """``"debug"`` / ``"INFO"`` / ``20`` -> logging level; unknown values -> INFO."""
    if isinstance(level, int):
        return level
    value = logging.getLevelName(str(level or "INFO").strip().upper())
    return value if isinstance(value, int) else logging.INFO


def setup(level: str | int | None = "INFO") -> None:
    """Configure the root logger (replacing earlier handlers, so calling it twice is safe)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(FORMAT, DATE_FORMAT))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root_level = parse_level(level)
    root.setLevel(root_level)
    # Never more verbose than these (SQL echo could carry message text), never chattier than the root either.
    for name, lib_level in LIBRARY_LEVELS.items():
        logging.getLogger(name).setLevel(max(lib_level, root_level))


def flush() -> None:
    """Flush the root handlers (before ``os._exit``, which skips the interpreter's own cleanup)."""
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:  # pragma: no cover - a broken stream must not block the exit
            pass
