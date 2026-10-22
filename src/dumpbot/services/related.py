"""What makes two items "about the same thing" for "🔗 похоже на …" (docs/features/iteration-2.md §C1).

Words are normalized by Postgres with the config of ``items.search_tsv`` (``to_tsvector('russian', …)``: Cyrillic by
the Russian stemmer, ASCII by the English one, stop words dropped). Only words that can name a subject count:

- no verbs in the infinitive: titles are "verb + object" ("Принять таблетки", "Принять посылку" share only the verb),
  so a Cyrillic word ending in -ть / -ться / -чься / -тись / -йти (not a noun of ``_TI_NOUNS``: «кровать», «сеть») is
  dropped before stemming; common verbs are also in ``GENERIC_WORDS`` with their imperatives ("прими", "отмени");
- no reminder / intent / courtesy words, time words, event-category nouns ("встреча", "созвон", "день рождения"),
  generic qualifiers ("новый") and common English verbs (``GENERIC_WORDS``, stemmed in SQL by the same config, so every
  inflection with the same stem is generic too: "напомни" / "напомнить" / "напомню" -> "напомн");
- no numbers, number words ("семь", "пятого", "полвосьмого": exact forms, «семья» stays), URLs, and time phrases
  with words that are also subjects: "через час", "часов в 17", "5 часов" ("часы" the watch stays), "8 марта",
  "в августе" (the names Марта / Августа stay).

"Напомни часов в 17 прогуглить про визу" and "Напомни через час покушать" share nothing; "Купить наушники Sony" and
"Наушники" share "наушник".

Used twice: the OR query of ``query.related_candidates`` is built from the subject lexemes of the message, and
``items`` keeps a related id the model named only when the subject lexemes of both items' titles / details meet.
Known limits (exact lexemes): "виза" / "визовый", "мама" / "мамин", "наушники" / "AirPods" do not meet.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from sqlalchemy import ColumnElement, ScalarSelect, Text, and_, cast, func, literal, literal_column, select
from sqlalchemy.dialects.postgresql import ARRAY, TSQUERY, aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot.db.models import Item

__all__ = [
    "GENERIC_WORDS",
    "MAX_WORDS",
    "NUMBER_WORDS",
    "lexeme_query",
    "message_words",
    "sharing_subject",
    "subject_lexeme_array",
    "subject_lexeme_sets",
    "subject_lexemes",
]

# Words that say when or how, not what. Inflections that stem differently are listed too ("купи" / "куплю").
_INTENT_WORDS: Final = """
напомни напомнить напомню напоминание напоминалка напоминай напоминать надо нужно нужна нужен нужны хочу хотел
хочется сделать сделай сделаю делать задача заметка идея мысль запиши записать запомни забыть забудь важно срочно
пожалуйста плиз поставь поставить поставлю глянь глянуть ещё обязательно просто кстати давай насчёт повод
todo task note idea reminder please pls
"""
_TIME_WORDS: Final = """
сегодня завтра послезавтра вчера сейчас скоро позже утро утром утра день днём дня вечер вечером вечера ночь ночью ночи
обед обеда полдень полночь полчаса около минута минуты минут неделя недели неделю месяц месяца число числа год года году
выходные выходных будни раз каждый каждую каждое следующий следующей понедельник вторник среда среду четверг пятница
пятницу суббота субботу воскресенье январь февраль апрель май мая июнь июль сентябрь октябрь ноябрь декабрь
утренний утренняя утренние вечерний вечерняя вечерние дневной дневная ночной ночная ежедневно ежедневный ежедневная
еженедельно еженедельный ежемесячно ежемесячный сегодняшний завтрашний вчерашний весна весной лето летом осень
осенью зима зимой весенний весенняя летний летняя осенний осенняя зимний зимняя конец конце начало начале середина
середине today tomorrow tonight morning evening night noon hour minute min mins day week month year weekend daily
weekly monthly every next later soon pm clock monday tuesday wednesday thursday friday saturday sunday january
february march april may june july august september october november december
"""
# The kind of an event and generic qualifiers are no subject: "Встреча с Олегом" / "Встреча с банком", "Новый
# телефон" / "Новые кроссовки", "AirPods Pro" / "MacBook Pro".
_KIND_WORDS: Final = """
встреча созвон звонок рождения др новый новая новое новые старый старая старое старые new pro max plus mini ultra
"""
_ACTION_WORDS: Final = """
купить купи куплю покупка покупать закупить закупи докупить докупи позвонить позвони написать напиши посмотреть
посмотри глянуть проверить проверь прогуглить погуглить загуглить гуглить найти найди заказать закажи оплатить оплати
заплатить забрать забери отправить отправь сходить сходи принять прими примите взять возьми отдать отдай вернуть верни
отменить отмени перенести перенеси забронировать забронируй продлить продли получить получи узнать узнай спросить
спроси уточнить уточни ответить ответь обсудить обсуди поговорить поговори встретиться записаться запишись сдать сдай
отнести отнеси отвезти отвези принести принеси привезти привези занести вынести вынеси починить почини помыть помой
убрать убери приготовить приготовь прочитать прочитай почитать почитай скачать скачай установить установи обновить
обнови выкинуть выкинь выбросить выброси поменять поменяй сменить смени подготовить подготовь подписать подпиши
заполнить заполни распечатать распечатай съездить съезди поехать поезжай пойти идти иди зайти зайди заехать заедь
начать начни закончить закончи доделать доделай подумать подумай решить реши выбрать выбери попробовать попробуй
помочь помоги лечь испечь подстричь покушать есть поесть съесть пообедать поужинать позавтракать
buy call check remind find order pay google get go need want make do send write read watch take ask meet book cancel
renew fix clean try remember look see pick bring return start finish prepare update install download print sign fill
change schedule
"""
GENERIC_WORDS: Final[tuple[str, ...]] = tuple((_INTENT_WORDS + _TIME_WORDS + _KIND_WORDS + _ACTION_WORDS).split())

_UNITS: Final = """
ноль нуля один одна одно одни одного одной одному одним одних два две двух двум двумя три трех трем тремя четыре
четырех четырем четырьмя пять пяти пятью шесть шести шестью семь семи восемь восьми восемью девять девяти девятью
десять десяти десятью сорок сорока пятьдесят пятидесяти шестьдесят шестидесяти семьдесят восемьдесят девяносто
девяноста сто ста двести триста тысяча тысячи полтора полторы полутора половина половины половине половину четверть
четверти пара пару пары несколько нескольких zero one two three four five six seven eight nine ten eleven twelve
fifteen twenty thirty forty fifty half quarter couple few several first second third
"""
_TEENS: Final = "одиннадцат двенадцат тринадцат четырнадцат пятнадцат шестнадцат семнадцат восемнадцат девятнадцат"
_ORDINALS: Final = f"перв втор трет четверт пят шест седьм восьм девят десят {_TEENS} двадцат тридцат сороков"
_ORDINAL_ENDINGS: Final = "ый ий ой ая ья ое ье ого ьего ому ьему ом ьем ую ью ые ьи ых ьих"
# Number words as written (exact forms, "ё" read as "е"), not stemmed like GENERIC_WORDS: "семь" must not take
# "семья" ("сем") with it, nor "пару" "пар". "Пол" + an ordinal is a spoken time ("полвосьмого", "полтретьего").
NUMBER_WORDS: Final[frozenset[str]] = frozenset(
    _UNITS.split()
    + [t + e for t in [*_TEENS.split(), "двадцат", "тридцат"] for e in ("ь", "и")]
    + [o + e for o in _ORDINALS.split() for e in _ORDINAL_ENDINGS.split()]
    + ["пол" + o + ("ьего" if o == "трет" else "ого") for o in _ORDINALS.split()[:12]]
)
# Nouns that end like an infinitive.
_TI_NOUNS: Final = frozenset(
    """
    мать зять кровать печать благодать знать память путь суть ртуть жуть муть сеть плеть треть нить прыть ноготь
    локоть коготь ломоть плоть копоть деготь
    """.split()
)
MAX_WORDS: Final = 30  # words of a message that are looked at
_WORD_RE: Final = re.compile(r"[^\W_]+", re.UNICODE)  # letters and digits only: safe in tsquery syntax
_INFINITIVE_RE: Final = re.compile(r"[а-я]+(?:[аеиоуыэюя]ть|ться|чься|тись|йти)")
# Numbers with a short suffix are not words: "5-го", "10-й", "2-х", "17ч", "10мин".
_NUMBERISH_RE: Final = re.compile(r"\d+\s*[-–]\s*[^\W\d_]{1,3}(?![^\W_])|(?<![^\W_])\d+[^\W\d_]{1,3}(?![^\W_])")
_LETTER_DIGIT_RE: Final = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")  # "iphone15" -> "iphone 15"
_URL_RE: Final = re.compile(r"(?:https?://|www\.|t\.me/)\S+|\b(?:[\w-]+\.)+[a-z]{2,}/\S*", re.I)
_B: Final = r"(?<![^\W_])"  # a word starts here
_E: Final = r"(?![^\W_])"  # a word ends here
_NUM: Final = rf"(?:\d+|{'|'.join(sorted(NUMBER_WORDS, key=len, reverse=True))}){_E}"
_HOUR: Final = rf"час(?:а|у|ов|ом|ам|ами|ах|ик|ика|иков|ок|очек){_E}"  # "час" as a unit, not "часы" (a watch)
# "Час" is a time unit with a quantity or a preposition ("2 часа", "через час", "часов в 17", "в течение часа");
# "часы" / "часов" alone is the watch.
_HOUR_PHRASE_RE: Final = re.compile(
    rf"{_B}(?:{_NUM}|пол|каждые|целых|ближайшие|сколько)\s*[-–]?\s*{_HOUR}"
    rf"|{_B}(?:через|за|на|в|во|к|до|после|около|каждый|целый|ровно|течение|течении|\w+ом)\s+час(?:а|у|ок|ик|ика)?{_E}"
    rf"|{_B}{_HOUR}\s+(?:(?:в|к|до|около)\s+{_NUM}|через|назад|спустя|ночи|дня|утра|вечера){_E}"
)


def _month(soft: str, hard: str, may: str) -> str:
    """A month name in one case: "января" / "марта" / "мая" for ("я", "а", "мая")."""
    return rf"(?:(?:январ|феврал|апрел|июн|июл|сентябр|октябр|ноябр|декабр){soft}|(?:март|август){hard}|{may}){_E}"


# A month is a date in the case its number or preposition asks for: "8 марта", "до марта", "в марте", "к марту",
# "на март". The name Марта / Августа is not: "Марте" after "к", "Марту", "Марты", "с Мартой" stay.
_DATE_PHRASE_RE: Final = re.compile(
    rf"{_B}(?:\d+\s*[-–]?\s*(?:го|е|ое|м|му)?|{_NUM}|до|с|со|после|около|начал[оеа]|конц[еа]|конец|середин[аеуы]"
    rf"|этого|следующего|прошлого)\s+{_month('я', 'а', 'мая')}"
    rf"|{_B}(?:в|во|этом|следующем|прошлом)\s+{_month('е', 'е', 'мае')}"
    rf"|{_B}(?:к|по)\s+{_month('ю', 'у', 'маю')}"
    rf"|{_B}(?:в|во|на|за|весь|каждый|через)\s+{_month('ь', '', 'май')}"
    rf"|{_B}перед\s+{_month('ем', 'ом', 'маем')}"
)
# Bare month names whose stem is also a name's ("март" / "Марта"): dropped as written, like NUMBER_WORDS.
_MONTH_WORDS: Final = frozenset({"март", "август"})
_RU = literal_column("'russian'::regconfig")


def message_words(text: str) -> list[str]:
    """Unique lowercase words of ``text`` ("ё" read as "е") in order that can name a subject: letters / digits only,
    no URLs, numbers or number words ("в 15:00" must not match every item with a 15, "5-го" is no word), no hour or
    date phrases ("через час", "8 марта"), no Cyrillic infinitives; at most ``MAX_WORDS``."""
    plain = _URL_RE.sub(" ", (text or "").lower().replace("ё", "е"))
    plain = _DATE_PHRASE_RE.sub(" ", _HOUR_PHRASE_RE.sub(" ", plain))
    plain = _LETTER_DIGIT_RE.sub(" ", _NUMBERISH_RE.sub(" ", plain))
    words = dict.fromkeys(_WORD_RE.findall(plain))
    return [w for w in words if len(w) > 1 and not w.isdigit() and not _never_subject(w)][:MAX_WORDS]


def _never_subject(word: str) -> bool:
    """A number word, a bare "март" / "август", or a Cyrillic infinitive (not "кровать", "сеть")."""
    if word in NUMBER_WORDS or word in _MONTH_WORDS:
        return True
    return _INFINITIVE_RE.fullmatch(word) is not None and word not in _TI_NOUNS


async def subject_lexemes(session: AsyncSession, text: str) -> list[str]:
    """The lexemes of ``text`` that can name a subject: its ``message_words`` normalized by
    ``to_tsvector('russian', …)`` minus the stems of ``GENERIC_WORDS`` (sorted, as Postgres returns them). Empty when
    nothing is left."""
    return (await subject_lexeme_sets(session, [text]))[0]


async def subject_lexeme_sets(session: AsyncSession, texts: Sequence[str]) -> list[list[str]]:
    """``subject_lexemes`` of each text, in one query."""
    words = [message_words(t) for t in texts]
    todo = [i for i, w in enumerate(words) if w]
    out: list[list[str]] = [[] for _ in texts]
    if not todo:
        return out
    generic = func.tsvector_to_array(func.to_tsvector(_RU, literal(" ".join(GENERIC_WORDS))))
    columns = [
        func.tsvector_to_array(func.ts_delete(func.to_tsvector(_RU, literal(" ".join(words[i]))), generic)).label(
            f"t{i}"
        )
        for i in todo
    ]
    row = (await session.execute(select(*columns))).one()
    for i, lexemes in zip(todo, row, strict=True):
        out[i] = [lx for lx in lexemes or [] if len(lx) > 1 and not lx.isdigit() and _WORD_RE.fullmatch(lx)]
    return out


def subject_lexeme_array(words: Sequence[str]) -> ScalarSelect[list[str]]:
    """``subject_lexemes`` of ``message_words`` ``words`` as an SQL ``text[]`` (``'{}'`` when nothing is left), for a
    query that needs them without asking for them first: one round trip to the database instead of two
    (docs/features/iteration-4.md §7). The lexemes are kept as ``subject_lexeme_sets`` keeps them: longer than one
    character, not a number, nothing that could break a tsquery."""
    generic = func.tsvector_to_array(func.to_tsvector(_RU, literal(" ".join(GENERIC_WORDS))))
    found = (
        func.unnest(func.tsvector_to_array(func.ts_delete(func.to_tsvector(_RU, literal(" ".join(words))), generic)))
        .table_valued("lexeme")
        .render_derived()
    )
    lexeme = found.c.lexeme
    usable = and_(
        func.length(lexeme) > 1,
        lexeme.op("!~")(literal("^[0-9]+$")),
        lexeme.op("!~")(literal(r"[\s'\\:&|!()<>*]")),
    )
    return (
        select(func.coalesce(func.array_agg(aggregate_order_by(lexeme, lexeme)), literal([], ARRAY(Text))))
        .where(usable)
        .scalar_subquery()
    )


def lexeme_query(lexemes: Sequence[str]) -> ColumnElement[str]:
    """``'a' | 'b' | …`` as a tsquery of these exact lexemes (already normalized: no second stemming). Lexemes are
    letters / digits only (``subject_lexemes``), so quoting them is safe."""
    return cast(literal(" | ".join(f"'{lx}'" for lx in lexemes)), TSQUERY)


async def sharing_subject(session: AsyncSession, text: str, item_ids: Sequence[int]) -> list[int]:
    """Those of ``item_ids`` (in their order) whose title / details have a subject lexeme in common with ``text``
    (the new item's title and details), both sides by ``subject_lexemes``: the code-side check of the related ids the
    model named. "Позвонить Марте" and "Цветы на 8 марта" share nothing: "8 марта" is a date on that side."""
    if not item_ids:
        return []
    rows = (await session.execute(select(Item.id, Item.title, Item.details).where(Item.id.in_(list(item_ids))))).all()
    if not rows:
        return []
    sets = await subject_lexeme_sets(session, [text, *(f"{title} {details or ''}" for _, title, details in rows)])
    mine = set(sets[0])
    found = {row[0] for row, theirs in zip(rows, sets[1:], strict=True) if mine.intersection(theirs)}
    return [i for i in item_ids if i in found]
