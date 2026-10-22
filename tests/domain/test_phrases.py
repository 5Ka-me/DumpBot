"""Phrases the code reads itself (docs/features/iteration-4.md §2): stop phrases and the clock times of a message."""

from __future__ import annotations

from datetime import time

import pytest

from dumpbot.domain.phrases import (
    NamedTime,
    clock_times,
    lost_times,
    named_times,
    only_stop_request,
    stop_request,
    understood_only,
)

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
        # places and the accounting software after "в": no times
        ("каждый вторник в 18:00 кружок в 5 кабинете", [time(18)]),
        ("по пятницам в 17:00 сдать отчёт в 1С", [time(17)]),
        ("в 10:00 пара в 3 корпусе", [time(10)]),
        ("в 9 тренировка в 2 зала, в 3 круга", [time(9)]),
        ("встреча в 9 с Олегом и в 18 с Машей", [time(9), time(18)]),  # "с" after a space is a time's company
    ],
)
def test_clock_times(text: str, times: list[time]):
    assert clock_times(text) == times


def test_lost_times_compares_on_a_twelve_hour_dial():
    assert lost_times([time(9), time(18)], [time(9)]) == [time(18)]
    assert lost_times([time(9), time(6)], [time(9), time(18)]) == []  # "в 6" saved as 18:00
    assert lost_times([time(9), time(18)], []) == [time(9), time(18)]


def test_named_times_know_a_bare_hour():
    assert named_times("в 8 и 20:00, в 7 утра, в 6 вечера, в 9.30, в 10 часов") == [
        NamedTime(time(8), either_half=True),
        NamedTime(time(20)),
        NamedTime(time(7)),
        NamedTime(time(18)),
        NamedTime(time(9, 30)),
        NamedTime(time(10), either_half=True),
    ]


def test_only_a_bare_hour_may_be_the_other_half_of_the_day():
    """ "8:00 и 20:00" kept as 08:00 lost 20:00 (the 12-hour dial would have hidden it); "в 6" kept as 18:00 did not."""
    assert lost_times(named_times("каждый день в 8:00 и 20:00"), [time(8)]) == [time(20)]
    assert lost_times(named_times("в 9 и в 6"), [time(9), time(18)]) == []


def test_one_kept_time_stands_for_one_named_time():
    assert lost_times(named_times("в 8 и в 20"), [time(8)]) == [time(20)]  # one 08:00 is not both
    assert lost_times(named_times("в 8 и в 20"), [time(8), time(20)]) == []


def test_understood_only():
    assert understood_only([time(9)]) == "понял только 09:00"
    assert understood_only([time(9), time(13)]) == "понял только 09:00 и 13:00"
    assert understood_only([time(8), time(13), time(18)]) == "понял только 08:00, 13:00 и 18:00"
