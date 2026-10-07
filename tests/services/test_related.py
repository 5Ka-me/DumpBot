"""What makes items related (docs/features/iteration-2.md §C1, ``services.related``): the subject words of a text —
normalized by Postgres' 'russian' config, without infinitives, reminder / time / courtesy words, event kinds, common
verbs, numbers, number words, URLs and hour / date phrases — and the code-side check of the related ids the model
names (both titles by the same rule)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Text, cast, func, literal, literal_column, select

from dumpbot.db.models import ItemLink
from dumpbot.services import query, related
from dumpbot.services.related import (
    GENERIC_WORDS,
    MAX_WORDS,
    NUMBER_WORDS,
    message_words,
    sharing_subject,
    subject_lexemes,
)

RU = literal_column("'russian'::regconfig")


def test_message_words_skip_numbers_and_keep_order():
    words = message_words("Напомни часов в 17:00 прогуглить про визу, 5-го числа 17ч 2-х iPhone15 pi5 визу")
    # "часов в 17" is a time, "прогуглить" an infinitive; "iPhone15" reads as "iPhone 15"
    assert words == ["напомни", "про", "визу", "числа", "iphone", "pi"]
    assert message_words("") == message_words("  ") == message_words("17 00 & | ! ( ) :*") == []
    assert len(message_words(" ".join(f"слово{chr(0x430 + i)}" for i in range(40)))) == MAX_WORDS
    assert message_words("Ёлка ещё") == ["елка", "еще"]


def test_message_words_drop_urls_number_words_and_infinitives():
    assert message_words("https://www.youtube.com/watch?v=abc123&utm_source=telegram") == []
    assert message_words("глянь youtube.com/watch?v=abc, www.ozon.ru и t.me/channel/5 про ремонт") == [
        "глянь",
        "про",
        "ремонт",
    ]
    spoken = "в семь, пятого числа, полвосьмого, без пятнадцати восемь, в половине девятого, двадцать первого"
    assert message_words(spoken) == ["числа", "без"]
    assert message_words("позвонить семье") == ["семье"]  # "семь" is a number, "семья" is not
    assert {"семь", "пятого", "полвосьмого", "полтретьего", "двадцать", "пару"} <= NUMBER_WORDS
    infinitives = "покушать принять записаться постричься пойти пройтись"
    assert message_words(f"{infinitives} кровать сеть память") == ["кровать", "сеть", "память"]


def test_message_words_keep_watches_and_names_out_of_time_phrases():
    hours = "в 17 часов, часов в 17, через 2 часа, в течение часа, пару часов, пол часа, через час, за час"
    assert message_words(hours) == ["через"]  # a stop word
    words = message_words("Отнести часы в ремонт, ремешок для часов")
    assert words == ["отнести", "часы", "ремонт", "ремешок", "для", "часов"]
    dates = "8 марта, 5-го августа, в марте, к маю, до августа, в конце марта, на март, восьмого марта, в мае, август"
    assert message_words(dates) == []
    assert message_words("Позвонить Марте, к Августе, подарок для Марты, с Мартой, Майя") == [
        "марте",
        "августе",
        "подарок",
        "для",
        "марты",
        "мартой",
        "майя",
    ]


async def test_every_generic_word_is_dropped_with_its_inflections(session):
    for start in range(0, len(GENERIC_WORDS), MAX_WORDS):
        chunk = " ".join(GENERIC_WORDS[start : start + MAX_WORDS])
        assert await subject_lexemes(session, chunk) == [], chunk
    phrases = (
        "напомнить напомню напомни",
        "купи куплю купить",
        "часов в 17 через час",
        "в среду утром, в субботу вечером, в октябре, 5-го числа",
        "Remind me to buy and check it tomorrow morning",
        "напомни пожалуйста, поставь напоминание, напоминай каждый час, глянь ещё",
        "принял отменил получил узнал ответил",
        "утренняя вечерние ежедневный зимнюю летом в конце",
        "новые встреча созвон звонок день рождения др",
        "get read need to renew new pro max watch book cancel",
    )
    for phrase in phrases:
        assert await subject_lexemes(session, phrase) == [], phrase


async def test_subject_lexemes_are_the_stems_of_the_rest(session):
    assert await subject_lexemes(session, "Напомни часов в 17 прогуглить про визу в черногорию") == ["виз", "черногор"]
    assert await subject_lexemes(session, "Купить наушники Sony") == ["soni", "наушник"]
    assert await subject_lexemes(session, "завтра в 10 позвонить маме") == ["мам"]
    # a bare verb names no subject: «Покушать» is related to nothing
    assert await subject_lexemes(session, "напомни через час покушать") == []
    assert await subject_lexemes(session, "Купить часы") == ["час"]
    assert await subject_lexemes(session, "Позвонить Марте") == ["март"]
    assert await subject_lexemes(session, "Подарок для Марты на 8 марта") == ["март", "подарок"]


async def test_the_lexeme_query_matches_the_search_index_exactly(session):
    query_ = related.lexeme_query(["наушник", "soni"])
    assert await session.scalar(select(cast(query_, Text))) == "'наушник' | 'soni'"  # no second stemming
    for text, hit in (("Чехол для наушников", True), ("SONY", True), ("Наушный", False), ("наушн", False)):
        matched = select(func.to_tsvector(RU, literal(text)).op("@@")(query_))
        assert await session.scalar(matched) is hit, text


async def test_sharing_subject_keeps_ids_with_a_common_subject_word(session, make, now):
    [eat, headphones, case, ticket, flowers] = await make(
        {"kind": "task", "title": "Покушать"},
        {"kind": "purchase", "title": "Купить наушники"},
        {"kind": "purchase", "title": "Чехол", "details": "для наушников Sony"},
        {"kind": "task", "title": "Купить билет", "details": "напомнить завтра в 17"},
        {"kind": "purchase", "title": "Цветы на 8 марта"},
        now=now,
    )
    ids = [ticket.item.id, case.item.id, eat.item.id, headphones.item.id, flowers.item.id]
    assert await sharing_subject(session, "Купить наушники Sony ", ids) == [case.item.id, headphones.item.id]
    # shared only by generic words ("купить", "напомнить", "завтра") or nothing at all -> nothing
    assert await sharing_subject(session, "Прогуглить про визу в Черногорию ", ids) == []
    assert await sharing_subject(session, "Купить завтра ", ids) == []
    assert await sharing_subject(session, "Покушать ", ids) == []
    # "марта" is a date there: the other side is judged by the same rule
    assert await sharing_subject(session, "Позвонить Марте ", ids) == []
    assert await sharing_subject(session, "Наушники", []) == []
    assert await sharing_subject(session, "Наушники", [10_000]) == []


# --- related pairs: a candidate for the new message, and linked when the model names it -----------------------------


async def _pair(session, make, now, old_title: str, new_title: str) -> tuple[bool, bool]:
    """The old item saved 5 days ago from a message with its title; is it a RELATED CANDIDATE of a message with the
    new title, and is the link written when the model names it?"""
    [old] = await make({"kind": "task", "title": old_title}, now=now - timedelta(days=5), source_text=old_title)
    candidate = old.item.id in [r.item_id for r in await query.related_candidates(session, new_title, now)]
    spec = {"kind": "task", "title": new_title, "related": [old.item.id]}
    await make(spec, now=now, source_text=new_title, related_allowed={old.item.id})
    links = (await session.execute(select(ItemLink.related_id))).scalars().all()
    return candidate, old.item.id in links


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # the same verb or connector only
        ("Принять посылку от курьера", "Принять таблетки"),
        ("Отменить подписку Netflix", "Отменить встречу с Олегом"),
        ("Забронировать отель в Праге", "Забронировать столик в ресторане"),
        ("Продлить страховку на машину", "Продлить подписку на спортзал"),
        ("Получить посылку на почте", "Получить справку в поликлинике"),
        ("Узнать про ипотеку в банке", "Узнать расписание электричек"),
        ("Ответить Олегу на письмо", "Ответить на отзыв клиента"),
        ("Взять зонт", "Взять справку с работы"),
        ("Позвонить Саше насчёт ремонта", "Спросить у Олега насчёт отпуска"),
        ("Написать в банк по поводу кредита", "Позвонить врачу по поводу анализов"),
        ("Покушать", "Покушать"),
        # time adjectives, seasons, qualifiers, event kinds
        ("Утренняя пробежка", "Утренние витамины"),
        ("Вечерняя прогулка с собакой", "Вечерние таблетки"),
        ("Ежедневный отчёт", "Ежедневная зарядка"),
        ("Поменять резину на зимнюю", "Купить зимнюю куртку"),
        ("Новый телефон", "Купить новые кроссовки"),
        ("Встреча с Олегом", "Встреча с банком"),
        ("Созвон с командой", "Созвон с заказчиком"),
        ("День рождения Маши", "День рождения Пети"),
        ("Позвонить Марте", "Купить цветы на 8 марта"),
        # English
        ("Get a haircut", "Get milk"),
        ("Read Dune", "Read the contract"),
        ("Need new tires", "Need to renew passport"),
        ("New phone", "New sneakers"),
        ("Buy AirPods Pro", "MacBook Pro battery replacement"),
    ],
)
async def test_generic_words_neither_bring_nor_link(session, make, now, old, new):
    assert await _pair(session, make, now, old, new) == (False, False)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Купить наушники", "Наушники Sony"),
        ("Наушники Sony", "Купить наушники"),
        ("Позвонить Марте", "Подарок для Марты"),
        ("Позвонить Августе", "Подарок для Августы"),
        ("Отнести часы в ремонт", "Купить ремешок для часов"),
        ("iPhone 15", "Чехол на iPhone15"),
        ("Утренняя пробежка", "Кроссовки для пробежки"),
    ],
)
async def test_the_same_subject_is_brought_and_linked(session, make, now, old, new):
    assert await _pair(session, make, now, old, new) == (True, True)


@pytest.mark.parametrize(
    "message",
    [
        "напомни пожалуйста позвонить маме",
        "поставь напоминание позвонить маме",
        "напоминай каждый час пить воду",
        "глянь ещё раз",
        "напомни в семь позвонить маме",
        "пятого числа позвонить маме",
        "полвосьмого позвонить маме",
        "без пятнадцати восемь позвонить маме",
        "в половине девятого позвонить маме",
    ],
)
async def test_courtesy_reminder_and_number_words_bring_no_candidates(session, make, now, message):
    [bread] = await make(
        {"kind": "purchase", "title": "Купить хлеб"},
        now=now - timedelta(days=3),
        source_text="напомни пожалуйста, поставь напоминание: напоминай каждый час, глянь ещё, в семь, пятого числа, "
        "полвосьмого, без пятнадцати восемь, в половине девятого купить хлеб",
    )
    assert bread.item.id not in [r.item_id for r in await query.related_candidates(session, message, now)]


async def test_a_link_brings_no_candidates_from_its_url(session, make, now):
    await make(
        {"kind": "task", "title": "Настроить Telegram-бота"},
        {"kind": "purchase", "title": "Ремешок для Apple Watch"},
        {"kind": "link", "title": "Видео", "details": "https://www.youtube.com/watch?v=xyz"},
        now=now - timedelta(days=3),
    )
    link = "https://www.youtube.com/watch?v=abc123&utm_source=telegram"
    assert await query.related_candidates(session, link, now) == []
