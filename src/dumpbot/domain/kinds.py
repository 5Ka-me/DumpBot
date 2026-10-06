"""Item kinds (the fixed user-facing categories) and message source kinds."""

from __future__ import annotations

from typing import Final, Literal, get_args

Kind = Literal["task", "event", "idea", "note", "purchase", "link", "meme", "quote", "other"]
KINDS: Final[tuple[str, ...]] = get_args(Kind)

KIND_LABELS: Final[dict[str, str]] = {
    "task": "Задача",
    "event": "Событие",
    "idea": "Идея",
    "note": "Заметка",
    "purchase": "Покупка",
    "link": "Ссылка",
    "meme": "Мем",
    "quote": "Цитата",
    "other": "Другое",
}

KIND_EMOJI: Final[dict[str, str]] = {
    "task": "📌",
    "event": "📅",
    "idea": "💡",
    "note": "📝",
    "purchase": "🛒",
    "link": "🔗",
    "meme": "😂",
    "quote": "💬",
    "other": "📦",
}

# Kinds that can be done / not done and get a default reminder when no time is given.
ACTIONABLE: Final[frozenset[str]] = frozenset({"task", "event", "purchase"})

# Where the content of a message came from (affects prompt hints and confirmation logic).
SourceKind = Literal["text", "voice", "photo", "forward", "link", "other"]

Precision = Literal["exact", "slot", "date", "default", "relative", "fallback", "none"]
