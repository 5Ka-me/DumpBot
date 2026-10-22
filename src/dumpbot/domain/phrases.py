"""A few phrases read by the code itself, not only by the AI (docs/features/iteration-4.md §2).

- **Stop phrases**: «больше не напоминай», «закрывай вообще», «хватит повторять», «останови повтор» (and close
  variants) about a repeating item mean "stop the repetition and close this time", about a one-off one just "done".
  ``stop_request`` finds one in a message; ``only_stop_request`` says the message is nothing but that (then the
  processor needs no AI at all to apply it to the item replied to).
- **Clock times** of a message (``clock_times``): "в 9:00 и 18:00", "в 9 и в 18", "в 9 утра и 6 вечера", "at 9
  and 18". A repeating item that kept fewer of them than the message named says so on its card: «⚠️ понял только
  09:00» (``lost_times``).

Pure functions, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import time
from typing import Final

from dumpbot.domain.fmt import fmt_time

__all__ = ["clock_times", "lost_times", "only_stop_request", "stop_request", "understood_only"]

_STOP_RE: Final = re.compile(
    r"больше\s+не\s+(?:(?:надо|нужно)\s+)?(?:мне\s+)?(?:напомина|повторя|присыла|пиш)\w*"
    r"|не\s+(?:напомина|повторя)\w*\s+больше"
    r"|закр(?:ыва|о)\w*\s+(?:\w+\s+)?(?:вообще|совсем|насовсем|навсегда)"
    r"|хватит\s+(?:\w+\s+)?(?:повтор|напомина)\w*"
    r"|(?:останов|выключ|отключ|убер|убра|отмен|прекрат)\w*\s+(?:\w+\s+)?повтор\w*"
    r"|стоп\s+повтор\w*"
    r"|stop\s+(?:repeating|reminding)\w*|(?:don'?t|do\s+not)\s+remind\s+(?:me\s+)?(?:again|anymore|any\s+more)",
    re.IGNORECASE,
)
# Words that may come with a stop phrase without asking for anything else ("всё, больше не напоминай об этом").
_FILLER: Final = frozenset(
    {
        "а",
        "и",
        "ну",
        "все",
        "уже",
        "да",
        "ок",
        "окей",
        "ok",
        "okay",
        "please",
        "пожалуйста",
        "плиз",
        "спасибо",
        "давай",
        "тогда",
        "теперь",
        "просто",
        "лучше",
        "вообще",
        "совсем",
        "это",
        "этот",
        "эту",
        "этого",
        "эти",
        "этом",
        "том",
        "его",
        "ее",
        "их",
        "мне",
        "меня",
        "об",
        "обо",
        "о",
        "про",
        "по",
        "ему",
        "ей",
        "напоминание",
        "напоминания",
        "напоминаний",
        "задачу",
        "задача",
        "дело",
        "повтор",
        "повторы",
        "him",
        "it",
        "this",
        "me",
        "about",
    }
)
_WORD_RE: Final = re.compile(r"[a-zа-я0-9']+")

# "9:00", "18:30", "9.30", a bare hour "9" (with "утра", "вечера", "часов" after it).
_TIME_RE: Final = re.compile(
    r"(?<![\w:.,])(?P<h>[01]?\d|2[0-3])(?:(?P<sep>[:.])(?P<m>[0-5]\d))?(?![\d:])"
    r"(?P<post>\s*(?:утра|утром|дня|днем|вечера|вечером|ночи|часов|часа|час|ч|am|pm|a\.m\.|p\.m\.))?",
    re.IGNORECASE,
)
# A bare hour counts after "в" / "во" / "at"…
_LEAD_RE: Final = re.compile(r"(?:^|[\s(])(?:в|во|at)\s+$", re.IGNORECASE)
# …or right after another time: "в 9 и 18", "в 9, 13 и в 18", "at 9 and 18".
_JOIN_RE: Final = re.compile(r"\s*(?:,|и|and)?\s*(?:в|во|at)?\s*", re.IGNORECASE)
# A bare number followed by one of these is an amount or a date, not a time ("в 2 раза", "и 3 таблетки", "9 мая").
_NOT_TIME_RE: Final = re.compile(
    r"\s*(?:-?го\b|-?е\b|числ|раз\b|раза|таблет|капсул|капел|шт|минут|мин\b|сек|дн|день|дня|недел|месяц|год|лет\b"
    r"|янв|фев|мар|апр|мая|май|июн|июл|авг|сен|окт|ноя|дек|%|руб|р\b|\$|€|кг|км|мл|л\b|г\b|стакан|человек|чел\b"
    r"|пункт|страниц|стр\b|литр|ложк|kg|km|times|min|day|week|month|year|pills?|tablets?)",
    re.IGNORECASE,
)
# The end of a range is not a time of its own: "с 9:00 до 18:00", "9:00-18:00".
_RANGE_END_RE: Final = re.compile(r"(?:\bдо|\bпо|\bto|\btill|\buntil|[-–—])\s*$", re.IGNORECASE)
_PM_WORDS: Final = ("вечера", "вечером", "дня", "днем", "pm", "p.m.")


def stop_request(text: str | None) -> bool:
    """The message asks to stop a repetition (or to stop reminding at all): «больше не напоминай», «закрывай
    вообще», «хватит повторять», «останови повтор», "stop reminding me"…"""
    return bool(text) and _STOP_RE.search(_norm(text or "")) is not None


def only_stop_request(text: str | None) -> bool:
    """The message is a stop phrase and nothing more (filler words aside): «Закрывай вообще!», «всё, больше не
    напоминай об этом». A message that also says something else ("больше не напоминай, перенеси на пятницу") is
    left to the AI."""
    low = _norm(text or "")
    if not _STOP_RE.search(low):
        return False
    rest = _STOP_RE.sub(" ", low)
    return all(word in _FILLER for word in _WORD_RE.findall(rest))


def clock_times(text: str | None) -> list[time]:
    """The clock times a message names, in order, without repeats: "в 9:00 и 18:00" -> 09:00, 18:00; "в 9 утра и 6
    вечера" -> 09:00, 18:00; "в 9, 13 и 18". HH:MM always counts; a bare hour (or "9.30") only after "в" / "at", or
    right after another time ("и" / "," / "and" between); amounts and dates ("в 2 раза", "9 мая") and the end of a
    range ("с 9:00 до 18:00", "9:00-18:00") do not. Hours 1-7 without "вечера" stay as they are: ``lost_times``
    compares on a 12-hour dial."""
    if not text:
        return []
    low = _norm(text)
    out: list[time] = []
    last_end = -1
    for m in _TIME_RE.finditer(low):
        before = low[: m.start()]
        if _RANGE_END_RE.search(before):
            continue
        post = (m.group("post") or "").strip()
        if m.group("sep") != ":":
            lead = _LEAD_RE.search(before) is not None
            joined = last_end >= 0 and _JOIN_RE.fullmatch(low, last_end, m.start()) is not None
            if not (lead or (joined and m.group("sep") is None)):
                continue
            if not post and _NOT_TIME_RE.match(low, m.end()):
                continue
        hour, minute = int(m.group("h")), int(m.group("m") or 0)
        if post in _PM_WORDS and hour < 12:
            hour += 12
        last_end = m.end()
        if (t := time(hour, minute)) not in out:
            out.append(t)
    return out


def lost_times(named: Sequence[time], saved: Iterable[time]) -> list[time]:
    """The times of ``named`` (``clock_times`` of a message) that none of ``saved`` is: compared on a 12-hour dial,
    since "в 6" may well have been saved as 18:00."""
    kept = {(t.hour % 12, t.minute) for t in saved}
    return [t for t in named if (t.hour % 12, t.minute) not in kept]


def understood_only(times: Sequence[time]) -> str:
    """The card warning of a series that kept fewer times than the message named: "понял только 09:00" ("09:00 и
    13:00", "08:00, 13:00 и 18:00")."""
    words = [fmt_time(t) for t in times]
    listed = words[0] if len(words) == 1 else f"{', '.join(words[:-1])} и {words[-1]}"
    return f"понял только {listed}"


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")
