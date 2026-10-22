"""ai.garbage: transcripts that are not speech (docs/features/iteration-4.md §1)."""

from __future__ import annotations

import pytest

from dumpbot.ai import garbage
from dumpbot.ai.garbage import garbage_reason, is_hallucination


@pytest.mark.parametrize(
    "text",
    [
        "Купить хлеб и молоко",
        "Напомни завтра в 9 позвонить маме",
        "купить iPhone и AirPods",  # half Latin is fine in Russian
        "ааа, не забыть ключи",  # a short drawl is speech
        "Позвонить в 12:30, номер 555-12-34",
        "",
        "   ",
        "Продолжение следует завтра: доделать отчёт",  # starts like a hallucination, but is more
        "Субтитры к фильму скачать",
        "Корректор купить",
    ],
)
def test_speech_passes(text: str) -> None:
    assert garbage_reason(text, duration=5, language="ru") is None


@pytest.mark.parametrize("text", ["ааааааааааааааа", "Нуууууууууууууууу и что", "MMMMMMMMMMMMMMMMMMM"])
def test_one_letter_fifteen_times_in_a_row(text: str) -> None:
    assert garbage_reason(text) == garbage.REPEATED_LETTER


def test_fourteen_in_a_row_is_still_speech() -> None:
    assert garbage_reason("а" * 14) is None
    assert garbage_reason("а" * 15) == garbage.REPEATED_LETTER


def test_digits_and_punctuation_are_not_letters() -> None:
    assert garbage_reason("1" * 20) is None
    assert garbage_reason("." * 20) is None


def test_more_than_forty_characters_per_second() -> None:
    text = "Купить хлеб " * 9  # 108 characters
    assert garbage_reason(text, duration=2) == garbage.TOO_FAST  # 54 / s
    assert garbage_reason(text, duration=3) is None  # 36 / s
    assert garbage_reason(text, duration=None) is None  # unknown: not judged
    assert garbage_reason(text, duration=0) is None


def test_cyrillic_share_only_for_russian() -> None:
    text = "Thank you very much for the meeting today"
    assert garbage_reason(text, language="ru") == garbage.NOT_CYRILLIC
    assert garbage_reason(text, language="en") is None
    assert garbage_reason(text, language=None) is None  # auto: a mix of languages is normal


def test_cyrillic_share_threshold() -> None:
    # 3 Cyrillic letters of 10 = 30 %: enough; 2 of 10: not
    assert garbage_reason("абв abcdefg", language="ru") is None
    assert garbage_reason("аб abcdefgh", language="ru") == garbage.NOT_CYRILLIC


@pytest.mark.parametrize(
    "text",
    [
        "Продолжение следует...",
        "ПРОДОЛЖЕНИЕ СЛЕДУЕТ…",
        "Спасибо за просмотр!",
        "Субтитры сделал DimaTorzok",
        "Субтитры создавал DimaTorzok",
        "Редактор субтитров А.Семкин Корректор А.Егорова",
        "Thank you for watching!",
        "[Музыка]",
        "Подписывайтесь на канал!",
    ],
)
def test_known_hallucinations(text: str) -> None:
    assert is_hallucination(text)
    assert garbage_reason(text, duration=5) == garbage.HALLUCINATION


def test_hallucination_is_the_whole_transcript_only() -> None:
    assert not is_hallucination("Спасибо за просмотр презентации, Иван")
    assert not is_hallucination("")


def test_normalize() -> None:
    assert garbage.normalize("  Ещё — раз!!! Всё…  ") == "еще раз все"
