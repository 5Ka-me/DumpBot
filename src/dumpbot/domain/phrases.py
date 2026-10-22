"""A few phrases read by the code itself, not only by the AI (docs/features/iteration-4.md §2).

- **Stop phrases**: «больше не напоминай», «закрывай вообще», «хватит повторять», «останови повтор» (and close
  variants) about a repeating item mean "stop the repetition and close this time", about a one-off one just "done".
  ``stop_request`` finds one in a message; ``only_stop_request`` says the message is nothing but that (then the
  processor needs no AI at all to apply it to the item replied to).
- **Clock times** of a message (``clock_times``, ``named_times``): "в 9:00 и 18:00", "в 9 и в 18", "в 9 утра и 6
  вечера", "at 9 and 18". A repeating item that kept fewer of them than the message named says so on its card:
  «⚠️ понял только 09:00» (``lost_times``: one saved time stands for one named time; only a bare hour 1-12 — "в 8" —
  may have been saved in the other half of the day, 20:00; "8:00" or "8 утра" is 08:00 and nothing else).

Pure functions, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import time
from typing import Final

from dumpbot.domain.fmt import fmt_time

__all__ = [
    "NamedTime",
    "clock_times",
    "lost_times",
    "named_times",
    "only_stop_request",
    "stop_request",
    "understood_only",
]

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
# A bare number followed by one of these is an amount, a date or a place, not a time ("в 2 раза", "и 3 таблетки",
# "9 мая", "в 5 кабинете", "в 3 корпусе", "в 3 круга"); "1С" right after it is the accounting software ("сдать отчёт
# в 1С"), while "в 9 с Олегом" is a time.
_NOT_TIME_RE: Final = re.compile(
    r"[сc]\b"
    r"|\s*(?:-?го\b|-?е\b|числ|раз\b|раза|таблет|капсул|капел|шт|минут|мин\b|сек|дн|день|дня|недел|месяц|год|лет\b"
    r"|янв|фев|мар|апр|мая|май|июн|июл|авг|сен|окт|ноя|дек|%|руб|р\b|\$|€|кг|км|мл|л\b|г\b|стакан|человек|чел\b"
    r"|пункт|страниц|стр\b|литр|ложк|kg|km|times|min|day|week|month|year|pills?|tablets?"
    r"|кабинет|каб\b|корпус|зал(?:а|е|ах|ов)?\b|этаж|подъезд|класс(?:а|е|ах)?\b|аудитори|комнат|кв\b"
    r"|круг(?:а|е|ов)?\b|сло(?:й|я|е|ев|ях)\b|подход(?:а|ов)?\b|ряд(?:а|у|ах|ов)?\b|раунд|сет(?:а|ов)?\b"
    r"|room|floor|building|rounds?\b|sets?\b)",
    re.IGNORECASE,
)
# The end of a range is not a time of its own: "с 9:00 до 18:00", "9:00-18:00".
_RANGE_END_RE: Final = re.compile(r"(?:\bдо|\bпо|\bto|\btill|\buntil|[-–—])\s*$", re.IGNORECASE)
_PM_WORDS: Final = ("вечера", "вечером", "дня", "днем", "pm", "p.m.")
_HALF_OF_DAY_WORDS: Final = ("утра", "утром", "ночи", "am", "a.m.", *_PM_WORDS)


@dataclass(frozen=True)
class NamedTime:
    """A clock time a message names. ``either_half``: a bare hour 1-12 ("в 8", "в 8 часов") that may mean the other
    half of the day too (20:00); "8:00", "8 утра" and "20" mean just that."""

    at: time
    either_half: bool = False


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
    right after another time ("и" / "," / "and" between); amounts, dates and places ("в 2 раза", "9 мая", "в 5
    кабинете", "в 1С") and the end of a range ("с 9:00 до 18:00", "9:00-18:00") do not. Hours 1-7 without "вечера"
    stay as they are (``named_times`` marks such a bare hour: ``lost_times`` lets it match either half of the day)."""
    return [named.at for named in named_times(text)]


def named_times(text: str | None) -> list[NamedTime]:
    """``clock_times`` with what each one may mean (``NamedTime.either_half``)."""
    if not text:
        return []
    low = _norm(text)
    out: list[NamedTime] = []
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
        bare = m.group("m") is None and post not in _HALF_OF_DAY_WORDS and 1 <= hour <= 12
        if post in _PM_WORDS and hour < 12:
            hour += 12
        last_end = m.end()
        if (t := time(hour, minute)) not in (n.at for n in out):
            out.append(NamedTime(t, either_half=bare))
    return out


def lost_times(named: Sequence[NamedTime | time], saved: Iterable[time]) -> list[time]:
    """The times of ``named`` (``named_times`` of a message) that ``saved`` does not keep. Each saved time stands for
    one named time (a single 08:00 is not both "8:00" and "20:00"): the same time first, then — only for a bare hour
    ("в 6", ``either_half``) — the same time on a 12-hour dial, since "в 6" may well have been saved as 18:00. A plain
    ``time`` in ``named`` is taken as a bare hour when its hour is 1-12."""
    wanted = [n if isinstance(n, NamedTime) else NamedTime(n, either_half=1 <= n.hour <= 12) for n in named]
    left = list(saved)
    pending: list[NamedTime] = []
    for n in wanted:
        if n.at in left:
            left.remove(n.at)
        else:
            pending.append(n)
    lost: list[time] = []
    for n in pending:
        dial = (n.at.hour % 12, n.at.minute)
        match = next((t for t in left if (t.hour % 12, t.minute) == dial), None) if n.either_half else None
        if match is None:
            lost.append(n.at)
        else:
            left.remove(match)
    return lost


def understood_only(times: Sequence[time]) -> str:
    """The card warning of a series that kept fewer times than the message named: "понял только 09:00" ("09:00 и
    13:00", "08:00, 13:00 и 18:00")."""
    words = [fmt_time(t) for t in times]
    listed = words[0] if len(words) == 1 else f"{', '.join(words[:-1])} и {words[-1]}"
    return f"понял только {listed}"


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")
