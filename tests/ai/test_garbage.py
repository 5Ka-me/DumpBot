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
    # Telegram's duration is whole seconds, cut down: a "1 s" note is up to 2 s long, so it is measured as duration + 1
    text = "Купить хлеб " * 9  # 108 characters
    assert garbage_reason(text, duration=1) == garbage.TOO_FAST  # 54 / s even over 2 s
    assert garbage_reason(text, duration=2) is None  # 36 / s over 3 s
    assert garbage_reason(text, duration=None) is None  # unknown: not judged
    assert garbage_reason(text, duration=0) is None


def test_a_fast_speaker_just_under_two_seconds_is_speech() -> None:
    # 46 characters in a note Telegram calls 1 s (it may be 1.9 s): a quick list, not a page made up from noise
    assert garbage_reason("Купить хлеб, молоко, яйца, сыр и масло завтра", duration=1, language="ru") is None


def test_cyrillic_share_only_for_russian() -> None:
    text = "Thank you very much for the meeting today"
    assert garbage_reason(text, language="ru") == garbage.NOT_CYRILLIC
    assert garbage_reason(text, language="en") is None
    assert garbage_reason(text, language=None) is None  # auto: a mix of languages is normal


def test_cyrillic_share_threshold() -> None:
    # 6 Cyrillic letters of 20 = 30 %: enough; 5 of 20: not
    assert garbage_reason("абвгде abcdefghijklmn", language="ru") is None
    assert garbage_reason("абвгд abcdefghijklmno", language="ru") == garbage.NOT_CYRILLIC


@pytest.mark.parametrize(
    "text",
    ["Zoom с Lead в 15:00", "Zoom call в 3", "Deploy на prod", "Code review", "Pull request", "OK.", "Okay."],
)
def test_short_notes_with_english_words_are_speech(text: str) -> None:
    # under 20 letters the share says nothing (and a second try would only hear the same words again)
    assert garbage_reason(text, duration=3, language="ru") is None


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
