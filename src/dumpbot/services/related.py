"""What makes two items "about the same thing" for "🔗 похоже на …" (docs/features/iteration-2.md §C1).

Words are normalized by Postgres with the config of ``items.search_tsv`` (``to_tsvector('russian', …)``: Cyrillic by
the Russian stemmer, ASCII by the English one, stop words dropped). Generic words never make two items related:
reminder / intent words, time words, common action verbs (``GENERIC_WORDS``) and numbers. "Напомни часов в 17
прогуглить про визу" and "Напомни через час покушать" share "напомн" and "час", not a subject; "Купить наушники Sony"
and "Купить наушники" share "наушник". ``GENERIC_WORDS`` are stemmed in SQL by the same config, so every inflection
with the same stem is generic too ("напомни" / "напомнить" / "напомню" -> "напомн").

Used twice: the OR query of ``query.related_candidates`` is built from the subject lexemes of the message only, and
``items`` keeps a related id the model named only when that item's title / details share a subject lexeme with the
new item's title / details.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from sqlalchemy import ColumnElement, cast, func, literal, literal_column, select
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot.db.models import Item

__all__ = ["GENERIC_WORDS", "MAX_WORDS", "lexeme_query", "message_words", "sharing_subject", "subject_lexemes"]

# Words that say when or how, not what. Inflections that stem differently are listed too ("купи" / "куплю").
_INTENT_WORDS: Final = """
напомни напомнить напомню напоминание напоминалка надо нужно нужна нужен нужны хочу хотел хочется сделать сделай
сделаю делать задача заметка идея мысль запиши записать запомни забыть забудь важно срочно
"""
_TIME_WORDS: Final = """
сегодня завтра послезавтра вчера сейчас скоро позже утро утром утра день днём дня вечер вечером вечера ночь ночью ночи
обед обеда полдень полночь час часа часов полчаса минута минуты минут неделя недели неделю месяц месяца число числа год
года году выходные выходных будни раз каждый каждую каждое следующий следующей понедельник вторник среда среду четверг
пятница пятницу суббота субботу воскресенье январь февраль март апрель май мая июнь июль август сентябрь октябрь ноябрь
декабрь today tomorrow tonight morning evening night noon hour minute day week month year weekend monday tuesday
wednesday thursday friday saturday sunday january february march april june july august september october november
december
"""
_ACTION_WORDS: Final = """
купить купи куплю покупка позвонить позвони написать напиши посмотреть посмотри глянуть проверить проверь прогуглить
погуглить загуглить гуглить найти найди заказать закажи оплатить оплати заплатить забрать забери отправить отправь
сходить сходи buy call check remind find order pay google
"""
GENERIC_WORDS: Final[tuple[str, ...]] = tuple((_INTENT_WORDS + _TIME_WORDS + _ACTION_WORDS).split())
MAX_WORDS: Final = 30  # words of a message that are looked at
_WORD_RE: Final = re.compile(r"[^\W_]+", re.UNICODE)  # letters and digits only: safe in tsquery syntax
# Numbers with a short suffix are not words: "5-го", "10-й", "2-х", "17ч", "10мин".
_NUMBERISH_RE: Final = re.compile(r"\d+\s*[-–]\s*[^\W\d_]{1,3}(?![^\W_])|(?<![^\W_])\d+[^\W\d_]{1,3}(?![^\W_])")
_RU = literal_column("'russian'::regconfig")


def message_words(text: str) -> list[str]:
    """Unique lowercase words of ``text`` in order: letters / digits only, no numbers ("в 15:00" must not match
    every item with a 15, "5-го" is no word), at most ``MAX_WORDS``."""
    plain = _NUMBERISH_RE.sub(" ", text or "")
    words = dict.fromkeys(w.lower() for w in _WORD_RE.findall(plain))
    return [w for w in words if len(w) > 1 and not w.isdigit()][:MAX_WORDS]


async def subject_lexemes(session: AsyncSession, text: str) -> list[str]:
    """The lexemes of ``text`` that can name a subject: its words normalized by ``to_tsvector('russian', …)`` minus
    the stems of ``GENERIC_WORDS`` and numbers (sorted, as Postgres returns them). Empty when nothing is left."""
    words = message_words(text)
    if not words:
        return []
    generic = func.tsvector_to_array(func.to_tsvector(_RU, literal(" ".join(GENERIC_WORDS))))
    stmt = select(func.tsvector_to_array(func.ts_delete(func.to_tsvector(_RU, literal(" ".join(words))), generic)))
    lexemes = await session.scalar(stmt) or []
    return [lx for lx in lexemes if len(lx) > 1 and not lx.isdigit() and _WORD_RE.fullmatch(lx)]


def lexeme_query(lexemes: Sequence[str]) -> ColumnElement[str]:
    """``'a' | 'b' | …`` as a tsquery of these exact lexemes (already normalized: no second stemming). Lexemes are
    letters / digits only (``subject_lexemes``), so quoting them is safe."""
    return cast(literal(" | ".join(f"'{lx}'" for lx in lexemes)), TSQUERY)


async def sharing_subject(session: AsyncSession, text: str, item_ids: Sequence[int]) -> list[int]:
    """Those of ``item_ids`` (in their order) whose title / details share a subject lexeme with ``text`` (the new
    item's title and details): the code-side check of the related ids the model named."""
    if not item_ids:
        return []
    lexemes = await subject_lexemes(session, text)
    if not lexemes:
        return []
    doc = func.to_tsvector(_RU, func.concat_ws(" ", Item.title, Item.details))
    stmt = select(Item.id).where(Item.id.in_(list(item_ids)), doc.op("@@", is_comparison=True)(lexeme_query(lexemes)))
    found = set((await session.scalars(stmt)).all())
    return [i for i in item_ids if i in found]
