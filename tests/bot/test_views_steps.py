"""Views of checklists and related items (docs/features/iteration-2.md §C): the parent's checklist with its toggles,
[🪄 Разбить на шаги] and [🔗 Похожее], the "🔗 похоже на" line, a step's card and reminder, list rows
"(из «Переезд»)" / "(2/5)", the 🎉 message, /help. Reference time: Tue 2026-10-06 14:05 Europe/Warsaw."""

from __future__ import annotations

from datetime import date

from dumpbot.bot import views
from dumpbot.bot.cbdata import ItemCb, SubCb
from dumpbot.dto import CardExtras, RelatedRef, Step

BIG = 10**12
NO_EXTRAS = CardExtras()


def _steps(at) -> tuple[Step, ...]:
    return (
        Step(item_id=BIG + 1, position=1, title="Упаковать вещи", done=True),
        Step(item_id=BIG + 2, position=2, title="Заказать <машину>", done=False, due_at=at(2026, 10, 8, 15, 0),
             precision="exact"),
        Step(item_id=BIG + 3, position=3, title="Отдать ключи", done=False, due_at=at(2026, 10, 9, 10, 0),
             precision="date"),
    )  # fmt: skip


def test_parent_card_lists_its_steps_with_a_toggle_each(make_item, prefs, now, at, check_view, buttons):
    item = make_item(id=BIG, title="Переезд")
    text, kb = check_view(views.item_card(item, None, [], prefs, now, extras=CardExtras(steps=_steps(at))))
    assert text.split("\n") == [
        "📌 <b>Переезд</b>",
        "📋 Шаги: 1/3",
        "☑ 1. Упаковать вещи",
        "☐ 2. Заказать &lt;машину&gt; · чт 8 окт 15:00",
        "☐ 3. Отдать ключи · пт 9 окт",
        "🏷 Задача",
    ]
    assert buttons(kb) == [
        ("☑ 1", SubCb(a="undo", id=BIG + 1).pack()),
        ("☐ 2", SubCb(a="done", id=BIG + 2).pack()),
        ("☐ 3", SubCb(a="done", id=BIG + 3).pack()),
        ("✏️ Изменить", ItemCb(a="edit", id=BIG).pack()),
        ("🗑 Удалить", ItemCb(a="del", id=BIG).pack()),
    ]
    assert kb.inline_keyboard[0] == kb.inline_keyboard[0][:4]  # toggles in rows of up to four


def test_eight_steps_make_two_rows_of_toggles(make_item, prefs, now, check_view):
    steps = tuple(Step(item_id=n, position=n, title=f"Шаг {n}", done=False) for n in range(1, 9))
    _, kb = check_view(views.item_card(make_item(), None, [], prefs, now, extras=CardExtras(steps=steps)))
    assert [len(row) for row in kb.inline_keyboard[:2]] == [4, 4]


def test_split_button_only_for_an_open_top_level_task_without_steps(make_item, prefs, now, at, buttons):
    def labels(item, extras=NO_EXTRAS):
        return [text for text, _ in buttons(views.item_card(item, None, [], prefs, now, extras=extras)[1])]

    assert "🪄 Разбить на шаги" in labels(make_item())
    assert ("🪄 Разбить на шаги", ItemCb(a="split", id=1).pack()) in buttons(
        views.item_card(make_item(), None, [], prefs, now, extras=CardExtras())[1]
    )
    assert "🪄 Разбить на шаги" not in labels(make_item(), CardExtras(steps=_steps(at)))
    assert "🪄 Разбить на шаги" not in labels(make_item(kind="purchase"))
    assert "🪄 Разбить на шаги" not in labels(make_item(status="done"))
    assert "🪄 Разбить на шаги" not in labels(make_item(rrule="FREQ=DAILY", rec_active=True))
    assert "🪄 Разбить на шаги" not in labels(make_item(parent_id=5, position=1))
    # without extras the steps are unknown: no offer
    assert "🪄 Разбить на шаги" not in [t for t, _ in buttons(views.item_card(make_item(), None, [], prefs, now)[1])]


def test_related_line_and_button(make_item, prefs, now, at, check_view, buttons):
    refs = (
        RelatedRef(item_id=7, kind="purchase", title="Купить наушники <Sony>", created_at=at(2026, 9, 12, 10, 0)),
        RelatedRef(item_id=8, kind="note", title="Чехол", created_at=at(2025, 8, 3, 10, 0)),
        RelatedRef(item_id=9, kind="note", title="Ещё одно", created_at=at(2025, 1, 1, 10, 0)),
    )
    item = make_item(kind="purchase", title="Купить чехол")
    text, kb = check_view(views.item_card(item, None, [], prefs, now, extras=CardExtras(related=refs)))
    assert "🔗 похоже на: «Купить наушники &lt;Sony&gt;» (12 сен), «Чехол» (3 авг 2025) и ещё 1" in text.split("\n")
    assert ("🔗 Похожее", ItemCb(a="rel", id=1).pack()) in buttons(kb)

    one = views.item_card(item, None, [], prefs, now, extras=CardExtras(related=refs[:1]))[0]
    assert "🔗 похоже на: «Купить наушники &lt;Sony&gt;» (12 сен)" in one.split("\n")
    plain_text, plain_kb = views.item_card(item, None, [], prefs, now, extras=CardExtras())
    assert "похоже" not in plain_text and "🔗 Похожее" not in [t for t, _ in buttons(plain_kb)]


def test_step_card_and_reminder_name_the_parent(make_item, make_occ, prefs, now, at, check_view):
    step = make_item(title="Заказать машину", parent_id=3, position=2)
    text, _ = check_view(views.item_card(step, None, [], prefs, now, extras=CardExtras(parent_title="Переезд <дом>")))
    assert text.split("\n")[:2] == ["📌 <b>Заказать машину</b>", "↳ шаг из «Переезд &lt;дом&gt;»"]
    occ = make_occ(at(2026, 10, 6, 15, 0))
    reminder, _ = check_view(views.reminder_message(step, occ, None, prefs, now, parent_title="Переезд"))
    assert reminder.split("\n") == ["🔔 <b>Заказать машину</b>", "↳ шаг из «Переезд»"]


def test_rows_name_the_parent_or_count_the_steps(make_row, prefs, now, at, check_view):
    rows = [
        make_row(item_id=2, title="Заказать машину", parent_id=1, parent_title="Переезд & co",
                 due_at=at(2026, 10, 8, 15, 0)),
        make_row(item_id=1, title="Переезд", steps_done=2, steps_total=5, due_at=at(2026, 10, 10, 10, 0)),
        make_row(item_id=3, title="Уборка", rrule="FREQ=WEEKLY", steps_done=0, steps_total=2),
    ]  # fmt: skip
    text, _ = check_view(views.rows_list("📋 Открытые задачи", rows, prefs, now))
    assert text.split("\n")[1:] == [
        "• чт 8 окт 15:00 · 📌 Заказать машину (из «Переезд &amp; co»)",
        "• сб 10 окт 10:00 · 📌 Переезд (2/5)",
        "• 📌 Уборка (0/2) 🔁",
    ]


def test_steps_closed_message_and_help(check_view):
    assert check_view(views.steps_closed_text("Переезд <2>"))[0] == "🎉 Все шаги выполнены — «Переезд &lt;2&gt;» закрыт"
    help_text = views.help_text()
    for phrase in ("распиши по шагам", "🪄 Разбить на шаги", "разбей на шаги", "(из «Переезд»)", "2/5", "🔗 Похожее"):
        assert phrase in help_text, phrase
    assert "«↩️ Не сделано» сразу после — снимает эти отметки" in help_text
    assert "у задачи с шагами нет повтора" in help_text
    assert views.PURPOSE_LABELS["split"] == "разбивка на шаги"


def test_a_step_with_a_span_shows_it_as_words(make_item, prefs, now, at, check_view):
    """«упаковать вещи на следующей неделе» is any day of 12–18 Oct, not a Friday deadline (its reminder (2))."""
    week = Step(
        item_id=BIG + 1,
        position=1,
        title="Упаковать вещи",
        done=False,
        due_at=at(2026, 10, 16, 19, 0),
        precision="period",
        period_kind="week",
        period_start=date(2026, 10, 12),
        period_end=date(2026, 10, 18),
    )
    soon = Step(
        item_id=BIG + 2,
        position=2,
        title="Позвонить",
        done=False,
        due_at=at(2026, 10, 8, 19, 0),
        precision="soon",
        period_kind="soon",
        period_start=date(2026, 10, 6),
        period_end=date(2026, 10, 8),
    )
    text, _ = check_view(
        views.item_card(make_item(id=BIG), None, [], prefs, now, extras=CardExtras(steps=(week, soon)))
    )
    assert "☐ 1. Упаковать вещи · на следующей неделе" in text.split("\n")
    assert "☐ 2. Позвонить · чт 8 окт" in text.split("\n")  # "на днях" reads as its day, like list rows
    assert views.step_when(week, prefs, now) == "на следующей неделе"
