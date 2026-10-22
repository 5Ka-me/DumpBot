"""Transcripts that are not speech (docs/features/iteration-4.md §1).

Whisper writes something even for noise, a pocket recording or a cut-off voice note: a letter repeated for a whole
line, a page of text for two seconds of audio, the subtitle credits of the videos it was trained on ("Продолжение
следует…", "Субтитры сделал DimaTorzok", "Thank you for watching"). Such a transcript saved as a note is litter, so
``garbage_reason`` names what is wrong with it (or None), and speech-to-text tries once more by another path
(``ai.stt.RoutedSTT``) before the bot asks the owner to repeat.

Any one sign is enough:
- one letter ``REPEAT_MIN`` (15) times in a row ("аааааааааааааааа");
- more than ``MAX_CHARS_PER_S`` (40) characters per second of the recording (Telegram's ``duration``: whole seconds,
  cut down by the sending app, so a note of 1.9 s says 1 — the text is measured against ``duration + 1``);
- in Russian (the language asked for is ``ru``): Cyrillic letters are under ``MIN_CYRILLIC`` (30 %) of all letters —
  judged only from ``MIN_SCRIPT_LETTERS`` (20) letters on: "Zoom с Lead в 15:00", "Deploy на prod" or "OK." are a
  developer's real notes, and a second try could only repeat them;
- the whole transcript is a known hallucination (``HALLUCINATIONS``, ``HALLUCINATION_PREFIXES``).

An empty transcript is silence, not garbage: such a voice note is saved without AI as before.
"""

from __future__ import annotations

import re
from typing import Final

REPEATED_LETTER: Final = "repeated letter"
TOO_FAST: Final = "too many characters per second"
NOT_CYRILLIC: Final = "not cyrillic"
HALLUCINATION: Final = "known hallucination"

REPEAT_MIN: Final = 15
MAX_CHARS_PER_S: Final = 40.0
MIN_CYRILLIC: Final = 0.3
# Fewer letters say too little about the language (the short English hallucinations are in ``HALLUCINATIONS``).
MIN_SCRIPT_LETTERS: Final = 20

# Whole transcripts Whisper makes up on silence and noise (normalized: lower case, ё -> е, letters, digits and single
# spaces only). Kept short on purpose: only what never is a note of its own.
HALLUCINATIONS: Final[frozenset[str]] = frozenset(
    {
        "продолжение следует",
        "спасибо за просмотр",
        "всем спасибо за просмотр",
        "спасибо за внимание",
        "подписывайтесь на канал",
        "ставьте лайки и подписывайтесь на канал",
        "до новых встреч",
        "музыка",
        "аплодисменты",
        "динамичная музыка",
        "спокойная музыка",
        "thank you for watching",
        "thanks for watching",
        "thank you for watching and see you next time",
        "please subscribe",
        "subscribe to the channel",
        "you",
    }
)
# Whole transcripts starting with these words: subtitle credits ("Субтитры сделал DimaTorzok", "Субтитры создавал
# DimaTorzok", "Редактор субтитров А.Семкин Корректор А.Егорова", "Subtitles by the Amara.org community").
HALLUCINATION_PREFIXES: Final[tuple[str, ...]] = (
    "субтитры сделал",
    "субтитры сделала",
    "субтитры создавал",
    "субтитры создал",
    "субтитры подготовил",
    "субтитры подогнал",
    "субтитры делал",
    "субтитры by",
    "редактор субтитров",
    "subtitles by",
)

_REPEAT_RE: Final = re.compile(rf"([^\W\d_])\1{{{REPEAT_MIN - 1},}}")
_LETTER_RE: Final = re.compile(r"[^\W\d_]")
_CYRILLIC_RE: Final = re.compile(f"[{chr(0x400)}-{chr(0x4FF)}]")  # the Cyrillic block
_NOT_WORD_RE: Final = re.compile(r"[\W_]+")


def garbage_reason(text: str, *, duration: float | None = None, language: str | None = None) -> str | None:
    """Why ``text`` is not a transcript of speech (one of the constants above), or None when it looks like one.

    ``duration``: seconds of the recording as Telegram gives them, whole and cut down (None or 0: not known, that
    sign is skipped); ``language``: the language asked for (the Cyrillic share is checked only for ``ru``, and only
    for a transcript of at least ``MIN_SCRIPT_LETTERS`` letters).
    """
    text = text.strip()
    if not text:
        return None
    if _REPEAT_RE.search(text.lower()):
        return REPEATED_LETTER
    if duration and duration > 0 and len(text) / (duration + 1) > MAX_CHARS_PER_S:  # up to duration + 1 s long
        return TOO_FAST
    if is_hallucination(text):
        return HALLUCINATION
    if language == "ru":
        letters = _LETTER_RE.findall(text)
        if len(letters) >= MIN_SCRIPT_LETTERS and len(_CYRILLIC_RE.findall(text)) / len(letters) < MIN_CYRILLIC:
            return NOT_CYRILLIC
    return None


def is_hallucination(text: str) -> bool:
    """The whole transcript is a known Whisper hallucination (punctuation, case and «ё» do not matter)."""
    norm = normalize(text)
    if not norm:
        return False
    return norm in HALLUCINATIONS or any(norm == p or norm.startswith(f"{p} ") for p in HALLUCINATION_PREFIXES)


def normalize(text: str) -> str:
    """Lower case, «ё» -> «е», runs of anything but letters and digits -> one space."""
    return _NOT_WORD_RE.sub(" ", text.lower().replace("ё", "е")).strip()
