"""Plain-text helpers of ``bot.views`` the Mini App API uses (docs/features/iteration-2.md §D) and the /help lines
about the app."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from dumpbot.bot import views
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import Row, Step

NOW = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)  # Tue 10:00 in Warsaw
PREFS = Prefs()


def _item(**kw):
    values = {
        "id": 1,
        "kind": "task",
        "title": "Созвон <b>",
        "status": "open",
        "someday": False,
        "period_kind": None,
        "period_start": None,
        "period_end": None,
        "when_spec": {},
        "rrule": None,
        "rec_active": False,
    }
    values.update(kw)
    return SimpleNamespace(**values)


def test_time_text_is_the_card_line_without_html() -> None:
    occ = SimpleNamespace(
        id=5, due_at=datetime(2026, 10, 7, 11, 0, tzinfo=UTC), precision="exact", status="open", done_at=None
    )
    reminder = SimpleNamespace(fire_at=datetime(2026, 10, 7, 11, 0, tzinfo=UTC), lead_minutes=0, status="pending")
    assert views.time_text(_item(), occ, [reminder], PREFS, NOW) == "🔔 завтра 13:00"
    assert views.time_text(_item(someday=True), occ, [reminder], PREFS, NOW) == "🌙 когда-нибудь"
    assert views.time_text(_item(), None, [], PREFS, NOW) is None


def test_row_and_step_when() -> None:
    row = Row(
        item_id=1,
        kind="task",
        title="Raspberry Pi",
        tags=[],
        status="open",
        due_at=None,
        occurrence_id=None,
        occ_status=None,
        rrule=None,
        created_at=NOW,
        snippet=None,
        period_kind="week",
        period_start=date(2026, 10, 5),
        period_end=date(2026, 10, 11),
    )
    assert views.row_when(row, PREFS, NOW) == "на этой неделе"
    step = Step(item_id=2, position=1, title="Шаг", done=False, due_at=datetime(2026, 10, 8, 13, 0, tzinfo=UTC))
    assert views.step_when(step, PREFS, NOW) == "чт 8 окт 15:00"


def test_repeat_text() -> None:
    assert views.repeat_text("FREQ=DAILY") == "каждый день"
    assert views.repeat_text(None) is None
    assert views.repeat_text("FREQ=SECONDLY") == "FREQ=SECONDLY"  # not describable: the rule as it is


def test_help_mentions_the_mini_app() -> None:
    text = views.help_text()
    assert "📱 Свалка" in text and "Mini App" in text
    assert views.tg_len(text) <= views.MAX_TEXT
