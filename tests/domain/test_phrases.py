"""Phrases the code reads itself (docs/features/iteration-4.md §2): stop phrases and the clock times of a message."""

from __future__ import annotations

from datetime import time

import pytest

from dumpbot.domain.phrases import clock_times, lost_times, only_stop_request, stop_request, understood_only

# --- stop phrases -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "больше не напоминай",
        "Больше не напоминать!",
        "больше не надо напоминать",
        "больше не повторяй",
        "не напоминай больше",
        "закрывай вообще",
        "Закрой это совсем",
        "закрывай насовсем",
        "хватит повторять",
        "хватит мне напоминать",
        "останови повтор",
        "отмени повторы",
        "выключи повторение",
        "стоп повтор",
        "stop reminding me",
        "don't remind me again",
    ],
)
def test_stop_phrases_are_found_and_alone(text: str):
    assert stop_request(text) is True
    assert only_stop_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "всё, больше не напоминай об этом",
        "Ну всё, закрывай вообще, спасибо",
        "хватит повторять это, пожалуйста",
    ],
)
def test_stop_phrase_with_filler_words_is_alone(text: str):
    assert only_stop_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "больше не напоминай, перенеси на пятницу",
        "отмени повторы таблеток",  # names what: the AI finds the item
        "хватит повторять, теперь по пятницам",
    ],
)
def test_stop_phrase_with_more_is_not_alone(text: str):
    assert stop_request(text) is True
    assert only_stop_request(text) is False


@pytest.mark.parametrize(
    "text",
    ["", None, "сделал", "готово", "перенеси на завтра", "напоминай каждый день", "повтори ещё раз", "закрой задачу"],
)
def test_not_a_stop(text: str | None):
    assert stop_request(text) is False
    assert only_stop_request(text) is False


# --- clock times --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "times"),
    [
        ("каждый день в 9:00 и 18:00", [time(9), time(18)]),
        ("в 9 и в 18 пить таблетки", [time(9), time(18)]),
        ("в 9 и 18", [time(9), time(18)]),
        ("в 9 утра и 6 вечера", [time(9), time(18)]),
        ("в 9, 13 и 18", [time(9), time(13), time(18)]),
        ("в 8:30 и 20:00", [time(8, 30), time(20)]),
        ("в 9.30 и 20:00", [time(9, 30), time(20)]),
        ("в 18 часов", [time(18)]),
        ("at 9 and 18", [time(9), time(18)]),
        ("в 9:00 и ещё раз в 9:00", [time(9)]),
        # amounts, dates, ranges and other numbers are not times
        ("каждый день в 9 пить 2 таблетки", [time(9)]),
        ("в 9 и 2 таблетки", [time(9)]),
        ("в 2 раза больше в 10", [time(10)]),
        ("9 мая в 10", [time(10)]),
        ("с 9:00 до 18:00", [time(9)]),
        ("9:00-18:00", [time(9)]),
        ("iPhone15 в 2026 году", []),
        ("1.10 в банк", []),
        ("", []),
    ],
)
def test_clock_times(text: str, times: list[time]):
    assert clock_times(text) == times


def test_lost_times_compares_on_a_twelve_hour_dial():
    assert lost_times([time(9), time(18)], [time(9)]) == [time(18)]
    assert lost_times([time(9), time(6)], [time(9), time(18)]) == []  # "в 6" saved as 18:00
    assert lost_times([time(9), time(18)], []) == [time(9), time(18)]


def test_understood_only():
    assert understood_only([time(9)]) == "понял только 09:00"
    assert understood_only([time(9), time(13)]) == "понял только 09:00 и 13:00"
    assert understood_only([time(8), time(13), time(18)]) == "понял только 08:00, 13:00 и 18:00"
