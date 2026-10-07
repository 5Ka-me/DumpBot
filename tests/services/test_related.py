"""What makes items related (docs/features/iteration-2.md §C1, ``services.related``): the subject words of a text —
normalized by Postgres' 'russian' config, without reminder / time words, common verbs and numbers — and the code-side
check of the related ids the model names."""

from __future__ import annotations

from sqlalchemy import Text, cast, func, literal, literal_column, select

from dumpbot.services import related
from dumpbot.services.related import GENERIC_WORDS, MAX_WORDS, message_words, sharing_subject, subject_lexemes

RU = literal_column("'russian'::regconfig")


def test_message_words_skip_numbers_and_keep_order():
    words = message_words("Напомни часов в 17:00 прогуглить про визу, 5-го числа 17ч 2-х iPhone15 pi5 визу")
    assert words == ["напомни", "часов", "прогуглить", "про", "визу", "числа", "iphone15", "pi5"]
    assert message_words("") == message_words("  ") == message_words("17 00 & | ! ( ) :*") == []
    assert len(message_words(" ".join(f"слово{chr(0x430 + i)}" for i in range(40)))) == MAX_WORDS


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
    )
    for phrase in phrases:
        assert await subject_lexemes(session, phrase) == [], phrase


async def test_subject_lexemes_are_the_stems_of_the_rest(session):
    assert await subject_lexemes(session, "Напомни часов в 17 прогуглить про визу в черногорию") == ["виз", "черногор"]
    assert await subject_lexemes(session, "Купить наушники Sony") == ["soni", "наушник"]
    assert await subject_lexemes(session, "напомни через час покушать") == ["покуша"]
    assert await subject_lexemes(session, "завтра в 10 позвонить маме") == ["мам"]


async def test_the_lexeme_query_matches_the_search_index_exactly(session):
    query = related.lexeme_query(["наушник", "soni"])
    assert await session.scalar(select(cast(query, Text))) == "'наушник' | 'soni'"  # no second stemming
    for text, hit in (("Чехол для наушников", True), ("SONY", True), ("Наушный", False), ("наушн", False)):
        matched = select(func.to_tsvector(RU, literal(text)).op("@@")(query))
        assert await session.scalar(matched) is hit, text


async def test_sharing_subject_keeps_ids_with_a_common_subject_word(session, make, now):
    [eat, headphones, case, ticket] = await make(
        {"kind": "task", "title": "Покушать"},
        {"kind": "purchase", "title": "Купить наушники"},
        {"kind": "purchase", "title": "Чехол", "details": "для наушников Sony"},
        {"kind": "task", "title": "Купить билет", "details": "напомнить завтра в 17"},
        now=now,
    )
    ids = [ticket.item.id, case.item.id, eat.item.id, headphones.item.id]
    assert await sharing_subject(session, "Купить наушники Sony ", ids) == [case.item.id, headphones.item.id]
    # shared only by generic words ("купить", "напомнить", "завтра") or nothing at all -> nothing
    assert await sharing_subject(session, "Прогуглить про визу в Черногорию ", ids) == []
    assert await sharing_subject(session, "Купить завтра ", ids) == []
    assert await sharing_subject(session, "Наушники", []) == []
