"""Views of iteration 4, §1: a voice note that was not understood, and the voice settings while the local Whisper is
paused."""

from __future__ import annotations

from dumpbot.bot import views
from dumpbot.bot.cbdata import AgainCb
from dumpbot.bot.routers.commands import START_TEXT
from dumpbot.domain.prefs import Prefs


def test_unheard_asks_to_repeat_with_a_retry_button(check_view, buttons) -> None:
    text, kb = check_view(views.unheard(42))
    assert text == "🎤 Не разобрал голосовое — повтори или напиши текстом"
    assert buttons(kb) == [("🔁 Ещё раз", AgainCb(raw=42).pack())]


def test_after_the_press_the_buttons_go(check_view) -> None:
    assert check_view(views.unheard_again()) == ("🎤 Распознаю ещё раз…", None)


def test_voice_menu_marks_a_paused_local_whisper(check_view, buttons) -> None:
    text, kb = check_view(views.settings_stt_menu(Prefs(stt_mode="local"), local_stt=True, local_down=True))
    assert "<i>Локальный Whisper сейчас недоступен — пока распознаю через chutes, проверю снова позже.</i>" in text
    assert ("✓ 💻 Локальный, при сбое — chutes (недоступен)", "s:stt_mode:local") in buttons(kb)
    assert ("🔀 Авто: chutes, при сбое — локальный", "s:stt_mode:auto") in buttons(kb)


def test_not_configured_wins_over_paused(buttons) -> None:
    text, kb = views.settings_stt_menu(Prefs(), local_stt=False, local_down=True)
    assert "недоступен" not in text
    assert ("💻 Локальный, при сбое — chutes (не настроен)", "s:stt_mode:local") in buttons(kb)


def test_settings_view_passes_the_pause_on() -> None:
    text, _ = views.settings_view("stt", Prefs(), local_stt=True, local_down=True)
    assert "сейчас недоступен" in text
    assert "сейчас недоступен" not in views.settings_view("stt", Prefs(), local_stt=True)[0]


def test_help_mentions_the_retry_and_start_still_fits_one_message() -> None:
    text = views.help_text()
    assert "шум без речи не сохраняю — «🔁 Ещё раз»" in text
    assert views.tg_len(f"{START_TEXT}\n\n{text}") <= views.MAX_TEXT  # /start sends both in one message
