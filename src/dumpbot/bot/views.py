"""Bot message texts and keyboards (HTML parse mode). Pure functions, no I/O.

Builders return ``(text_html, markup | None)``; ``usage_text`` and ``help_text`` return text only.

Rules:
- every user/LLM string is passed through ``html.escape(s, quote=False)`` (``href`` values with ``quote=True``);
- user strings are clipped *before* they are escaped and wrapped in tags, and each line keeps its tags balanced,
  so ``fit`` can drop whole lines to stay within Telegram's 4096-character limit (counted in UTF-16 units);
- dates are rendered in Russian by ``domain.fmt`` relative to the ``now`` argument (never the wall clock);
- an item's time is its notification time (docs/requirements.md, 2026-10-07): the card's time line is the moment(s)
  it notifies, grouped by day — "🔔 сегодня 13:00", "🔔 завтра 09:30 и 10:15", "🔔 вс 11 окт 10:00, пн 12 окт
  09:00" ("за день и за час"); "🔕 сегодня 13:00" with reminders off, "⏰ сегодня 13:00 · просрочено" once past,
  "⏰ сегодня 13:00" when done;
- fuzzy time (docs/features/iteration-2.md §A) has its own time lines: "🗓 на этой неделе (до вс 11 окт) · 🔔 пт
  19:00", "⏳ до пт 9 окт · 🔔 чт 19:00, пт 10:00", "🌙 когда-нибудь";
- checklists and related items (§C): a parent's card lists its steps "☐ 1. Упаковать вещи" with a [☐ 1] / [☑ 1]
  toggle each; a step's card and reminder name the parent; list rows read "📌 Заказать машину (из «Переезд»)" and
  "📌 Переезд (2/5)"; "🔗 похоже на: «Купить наушники Sony» (12 сен)" with a [🔗 Похожее] button;
- iteration 3 (docs/features/iteration-3.md): /today, /tasks and the digests number their open lines with [✅ N]
  buttons (§2, "numbered lists" below); an edit applied by description is answered with a short line
  ("✅ Закрыл «…»", "🕒 Перенёс «…» на …", "🗑 Удалил «…»") and [↩️ Отменить] (§3, ``edit_applied``); after a
  downtime one silent "😴 Меня не было с 19:00 до 23:19…" (§5, ``downtime_notice``), a missed evening digest shows up
  in the next morning digest as "🌙 Вчерашний итог (не успел прислать)" (``morning_sections``), a reminder held over
  quiet hours says how late it is from its own time; a reply to a voice note shows what was heard, "🎤 «…»" (§6,
  ``heard_line``, ``with_heard``; cards through ``CardExtras.heard``), and a voice note that gave only notes / ideas
  gets "🎤 «…» → 💡 Идея «…»" with [✏️ Изменить] [🗑 Не то — удалить] for each of them (``heard_item``);
- iteration 4 (docs/features/iteration-4.md §1): a voice note whose transcript was not speech (twice) gets no items
  but "🎤 Не разобрал голосовое — повтори или напиши текстом" with [🔁 Ещё раз] (``unheard``; after the press:
  ``unheard_again``); the voice settings mark the local mode «(недоступен)» while the local Whisper is paused;
- iteration 4, §2-3: a reminder's buttons follow the item's type (``reminder_kind``): a one-off at a time [⏰ 1 час]
  [⏰ 3 часа] [📅 Завтра] (🌙 «Вечером» is gone from new reminders), a span / "на днях" / a deadline [➡️ След.
  неделя] (a month: «След. месяц») [📅 Выбрать день] [💤 Когда-нибудь], a series [⏰ 1 час] [⏹ Хватит повторять];
  ✅ on a series' reminder adds [⏹ Больше не повторять]. A series with several times a day reads "🔁 каждый день в
  09:00 и 18:00" (``describe_rrule_ru``).

Re-rendering in place (for the callbacks router):
- card after done / undone / kind / norem / stoprec / back -> ``item_card`` with fresh item, occurrence and
  pending reminders (a done occurrence shows "✅ сделано" and [↩️ Не сделано]);
- reminder message after ``OccCb done`` -> ``reminder_done`` (alias ``done_text``; a series: with [⏹ Больше не
  повторять]); after ``undone`` / ``rem`` -> ``reminder_message(item, occ, None, ...)``; after
  ``s15``/``s60``/``s180``/``tmr``/``eve`` -> ``reminder_snoozed`` with the new reminder returned by ``items.snooze``
  (no buttons: the snoozed reminder brings its own); after ``nw`` / ``nm`` / ``DayCb`` -> ``reminder_moved``; after
  ``smd`` -> ``reminder_someday``; ``pick`` -> ``day_picker``; ``stop`` -> ``reminder_done`` of the closed instance
  (or ``reminder_stopped``);
- after ``ItemCb del`` -> ``deleted_card``; after ``undel`` -> ``item_card``;
- the reply to an edit by description after ``UndoCb`` -> ``item_card`` of the item as it is again;
- a list after ``ListCb`` -> ``today_list`` / ``tasks_list`` / ``morning_digest`` / ``evening_digest`` with the
  ``lines`` of ``pin_lines`` (``bot.lists``): the same numbers, each line as it is now.

Settings callbacks (``SetCb(key, v)``) emitted by the settings menus:

=================  =========================================  ==================================================
key                v                                          action
=================  =========================================  ==================================================
``menu``           ``main`` | ``tz`` | ``slots`` | ``quiet``  show that (sub)menu in place: ``settings_view(v)``
                   | ``digest`` | ``stt`` | ``budget``          (an unknown one, e.g. the old ``lead``: the main menu)
``close``          ``""``                                     close the menu (remove the keyboard)
``tz``             IANA name, e.g. ``Europe/Warsaw``          set ``Prefs.tz``
``quiet_toggle``   ``""``                                     flip ``Prefs.quiet_enabled``
``digest_toggle``  ``morning`` | ``evening``                  flip ``Prefs.digest_<v>_enabled``
``stt``            ``auto`` | ``ru`` | ``en``                 set ``Prefs.stt_language`` (``auto`` -> None)
``stt_mode``       ``auto`` | ``local`` | ``chutes``          set ``Prefs.stt_mode`` (speech-to-text order)
``budget``         dollars: ``1`` ``3`` ``5`` ``10``          set ``Prefs.budget_usd``
``ask``            a ``Prefs`` field name from ``ASK_FIELDS``  ask the user to type a value for that field: show
                                                              ``settings_ask(v, prefs)``, wait for text (FSM),
                                                              validate it (``HH:MM`` for times, IANA name for
                                                              ``tz``, number >= 0 for ``budget_usd``), save,
                                                              then show ``settings_view(ASK_FIELDS[v].menu)``
=================  =========================================  ==================================================

After any change the router saves prefs, wakes the scheduler and re-renders the submenu the button came from
(``SETTINGS_PARENT`` maps keys to it). Changes apply to new reminders only; existing ones are not re-planned. Buttons
of the removed "⏱ Напоминать за" (key ``lead``, ``ask`` ``lead_minutes``) answer "Кнопка устарела".
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dumpbot.bot.cbdata import (
    LIST_EVENING,
    LIST_MORNING,
    LIST_TASKS,
    LIST_TODAY,
    AgainCb,
    AskCb,
    ItemCb,
    KindCb,
    PickCb,
    SetCb,
)
from dumpbot.bot.keyboards import (
    BACK,
    ReminderKind,
    back_to_card_row,
    button,
    chunked,
    clip_label,
    day_picker_keyboard,
    digest_move_row,
    item_card_keyboard,
    list_buttons,
    markup,
    reminder_keyboard,
    undo_keyboard,
    undone_keyboard,
)
from dumpbot.domain.fmt import MONTHS_SHORT, fmt_date, fmt_day, fmt_dt, fmt_duration, fmt_time, plural_ru
from dumpbot.domain.kinds import ACTIONABLE, CALENDAR_PERIODS, KIND_EMOJI, KIND_LABELS, KINDS
from dumpbot.domain.prefs import SLOT_LABELS, SLOT_NAMES, Prefs
from dumpbot.domain.recurrence import describe_rrule_ru

if TYPE_CHECKING:
    from dumpbot.ai.schemas import Ask, EditPatch
    from dumpbot.db.models import Item, Occurrence, Reminder
    from dumpbot.dto import Agenda, CardExtras, DigestData, RelatedRef, Row, Step, UsageSummary

View = tuple[str, InlineKeyboardMarkup | None]
MenuView = tuple[str, InlineKeyboardMarkup]

MAX_TEXT: Final = 4096
TITLE_MAX: Final = 200
LIST_TITLE_MAX: Final = 120
DETAILS_MAX: Final = 300
WARNING_MAX: Final = 200
WARNINGS_SHOWN: Final = 5
TAG_MAX: Final = 30
TAGS_SHOWN: Final = 8
URL_LABEL_MAX: Final = 40
HREF_MAX: Final = 1000
QUESTION_MAX: Final = 300
CANDIDATES_MAX: Final = 5
STEP_TITLE_MAX: Final = 80
HEARD_MAX: Final = 120  # "🎤 «…»": the transcript of a voice note, clipped
PARENT_TITLE_MAX: Final = 60
RELATED_SHOWN: Final = 2  # titles named on the card's "🔗 похоже на" line
RELATED_SENT: Final = 3  # cards sent by [🔗 Похожее]
SPLIT_KINDS: Final[frozenset[str]] = frozenset({"task"})  # cards with [🪄 Разбить на шаги]
# Room kept free for lines that follow a long list (section headers, counters).
_TAIL_RESERVE: Final = 300

PENDING: Final[frozenset[str | None]] = frozenset({None, "pending", "sending"})
SOMEDAY_LINE: Final = "🌙 когда-нибудь"
# Precisions whose occurrence can be a deadline "⏳ до …" (role deadline): an explicit date or time.
_DEADLINE_PRECISIONS: Final[frozenset[str]] = frozenset({"exact", "slot", "date", "fallback"})
_MONTHS_IN: Final[tuple[str, ...]] = (
    "январе",
    "феврале",
    "марте",
    "апреле",
    "мае",
    "июне",
    "июле",
    "августе",
    "сентябре",
    "октябре",
    "ноябре",
    "декабре",
)
# First month of a season -> (when it is not the current one, when it is).
_SEASON_WORDS: Final[dict[int, tuple[str, str]]] = {
    3: ("весной", "этой весной"),
    6: ("летом", "этим летом"),
    9: ("осенью", "этой осенью"),
    12: ("зимой", "этой зимой"),
}
NEEDS_CONFIRM_HINT: Final = "проверь, правильно ли я понял"
LATE_AFTER: Final = timedelta(minutes=2)  # a reminder sent later than this after its time says so

# --- text helpers ------------------------------------------------------------------------------------------------


def esc(s: object) -> str:
    """Escape text for HTML parse mode (``<``, ``>``, ``&``)."""
    return html.escape(str(s), quote=False)


def clip(s: str | None, limit: int) -> str:
    """Single-line plain text of at most ``limit`` characters (with "…" when cut)."""
    s = " ".join((s or "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def q(s: str | None, limit: int = TITLE_MAX) -> str:
    """Clip, then escape: safe to wrap in tags."""
    return esc(clip(s, limit))


def tg_len(s: str) -> int:
    """Length as Telegram counts it (UTF-16 code units)."""
    return len(s.encode("utf-16-le")) // 2


_TAG_RE = re.compile(r"<[^>]*>")
_PARTIAL_ENTITY_RE = re.compile(r"&[#a-zA-Z0-9]*$")


def fit(text: str, limit: int = MAX_TEXT) -> str:
    """Cut ``text`` to ``limit`` by dropping whole trailing lines (each line keeps its tags balanced)."""
    if tg_len(text) <= limit:
        return text
    room = limit - 2  # "\n…"
    out: list[str] = []
    used = 0
    for line in text.split("\n"):
        cost = tg_len(line) + (1 if out else 0)
        if used + cost > room:
            break
        out.append(line)
        used += cost
    if not out:  # one huge first line: drop the markup and cut the plain text
        return _cut_units(_TAG_RE.sub("", text.split("\n", 1)[0]), limit - 1) + "…"
    return "\n".join(out) + "\n…"


def _cut_units(s: str, units: int) -> str:
    out: list[str] = []
    used = 0
    for ch in s:
        used += tg_len(ch)
        if used > units:
            break
        out.append(ch)
    return _PARTIAL_ENTITY_RE.sub("", "".join(out))


def fit_lines(lines: Sequence[str], budget: int) -> list[str]:
    """As many ``lines`` as fit into ``budget`` units, plus "… и ещё N" when some are left out."""
    costs = [tg_len(line) + 1 for line in lines]
    if sum(costs) <= budget:
        return list(lines)
    out: list[str] = []
    used = 0
    room = budget - 20  # for the "… и ещё N" line
    for i, (line, cost) in enumerate(zip(lines, costs, strict=True)):
        if used + cost > room:
            out.append(f"… и ещё {len(lines) - i}")
            break
        out.append(line)
        used += cost
    return out


def _local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz) if dt.tzinfo is not None else dt.replace(tzinfo=UTC).astimezone(tz)


def fmt_money(usd: float) -> str:
    if 0 < usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"


def _kind_emoji(kind: str | None) -> str:
    return KIND_EMOJI.get(kind or "", "📦")


def _kind_label(kind: str | None) -> str:
    return KIND_LABELS.get(kind or "", kind or "Другое")


# --- item card ---------------------------------------------------------------------------------------------------


def _pending(reminders: Sequence[Reminder]) -> list[Reminder]:
    return sorted((r for r in reminders if r.status in PENDING), key=lambda r: r.fire_at)


def _occ_done(item: Item, occ: Occurrence | None) -> bool:
    if occ is not None:
        return occ.status == "done"
    return item.status == "done"


def _notify_times(pending: Sequence[Reminder], tz: ZoneInfo, now: datetime) -> str:
    """When the item notifies: "🔔 сегодня 13:00", "🔔 завтра 09:30 и 10:15", "🔔 вс 11 окт 10:00, пн 12 окт 09:00"."""
    today = _local(now, tz).date()
    return _notify_line(pending, tz, lambda d: fmt_date(d, today=today))


def _notify_days(pending: Sequence[Reminder], tz: ZoneInfo, now: datetime) -> str:
    """Notifications of a span or a deadline, days as short as they stay clear: "🔔 пт 19:00", "🔔 чт 19:00, пт
    10:00"."""
    today = _local(now, tz).date()
    return _notify_line(pending, tz, lambda d: fmt_day(d, today=today))


def _notify_line(pending: Sequence[Reminder], tz: ZoneInfo, day_label: Callable[[date], str]) -> str:
    """ "🔔 " + the notification moments grouped by day: each day once, its times joined with "и" ("завтра 09:30 и
    10:15"), the days with commas."""
    by_day: dict[date, list[str]] = {}
    for reminder in pending:
        local = _local(reminder.fire_at, tz)
        times = by_day.setdefault(local.date(), [])
        if (clock_time := fmt_time(local.time())) not in times:
            times.append(clock_time)
    return "🔔 " + ", ".join(f"{day_label(d)} {_join_ru(times)}" for d, times in by_day.items())


def _join_ru(parts: Sequence[str]) -> str:
    """ "a", "a и b", "a, b и c"."""
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} и {parts[-1]}"


def period_label(kind: str | None, start: date, end: date, today: date) -> str:
    """A fuzzy span as words, relative to ``today``: "на этой неделе", "на следующей неделе", "на неделе с 19 окт",
    "в этом месяце", "в следующем месяце", "в ноябре", "в марте 2028", "этой осенью", "летом", "летом 2027",
    "на днях"."""
    if kind == "week":
        monday = today - timedelta(days=today.weekday())
        if start == monday:
            return "на этой неделе"
        if start == monday + timedelta(days=7):
            return "на следующей неделе"
        return f"на неделе с {start.day} {MONTHS_SHORT[start.month - 1]}"
    if kind == "month":
        months_ahead = (start.year - today.year) * 12 + start.month - today.month
        if months_ahead in (0, 1):
            return ("в этом месяце", "в следующем месяце")[months_ahead]
        name = f"в {_MONTHS_IN[start.month - 1]}"
        return name if start.year == today.year else f"{name} {start.year}"
    if kind == "season" and start.month in _SEASON_WORDS:
        plain, current = _SEASON_WORDS[start.month]
        if start <= today <= end:
            return current
        return plain if start.year == today.year else f"{plain} {start.year}"
    return "на днях" if kind == "soon" else ""


def _span_end(end: date, today: date) -> str:
    """End of a span for "(до вс 11 окт)": the plain date, with the year when it is another one."""
    return fmt_date(end, today=today, relative=False)


def _period_head(item: Item, occ: Occurrence, today: date) -> str | None:
    """ "🗓 на этой неделе (до вс 11 окт)" / "🗓 на днях" for an occurrence of a fuzzy span, else None."""
    if occ.precision == "soon":
        return "🗓 на днях"
    start, end = item.period_start, item.period_end
    if occ.precision != "period" or item.period_kind not in CALENDAR_PERIODS or start is None or end is None:
        return None
    return f"🗓 {period_label(item.period_kind, start, end, today)} (до {_span_end(end, today)})"


def _is_deadline(item: Item, occ: Occurrence) -> bool:
    return occ.precision in _DEADLINE_PRECISIONS and (item.when_spec or {}).get("role") == "deadline"


def _deadline_day(occ: Occurrence, tz: ZoneInfo, today: date) -> str:
    """ "пт 9 окт" (a date deadline) or "пт 9 окт 15:00" (one with a time)."""
    local = _local(occ.due_at, tz)
    day = fmt_date(local.date(), today=today, relative=False)
    return day if occ.precision in ("date", "fallback") else f"{day} {fmt_time(local.time())}"


def _fuzzy_time_line(
    item: Item, occ: Occurrence, reminders: Sequence[Reminder], prefs: Prefs, now: datetime
) -> str | None:
    """Time line of a period / "на днях" / deadline occurrence, None for other ones."""
    tz = prefs.zone
    today = _local(now, tz).date()
    head = _period_head(item, occ, today)
    if head is None and _is_deadline(item, occ):
        head = f"⏳ до {_deadline_day(occ, tz, today)}"
    if head is None:
        return None
    if _occ_done(item, occ) or occ.status in ("missed", "cancelled"):
        return head
    pending = _pending(reminders)
    if pending:
        return f"{head} · {_notify_days(pending, tz, now)}"
    running = item.period_end is not None and item.period_end >= today and occ.precision == "period"
    return f"{head} · {'🔕 без напоминания' if running or occ.due_at > now else 'просрочено'}"


def _time_line(
    item: Item, occ: Occurrence | None, reminders: Sequence[Reminder], prefs: Prefs, now: datetime
) -> str | None:
    if item.someday:
        return SOMEDAY_LINE
    if occ is None or occ.due_at is None:
        return None
    fuzzy = _fuzzy_time_line(item, occ, reminders, prefs, now)
    if fuzzy is not None:
        return fuzzy
    tz = prefs.zone
    done = _occ_done(item, occ)
    if occ.precision == "default" and (done or occ.status == "cancelled"):
        return None  # a time the bot chose, no longer planned: nothing to show
    if done or occ.status in ("missed", "cancelled"):
        if occ.precision in ("date", "default"):
            return f"⏰ {fmt_date(_local(occ.due_at, tz).date(), today=_local(now, tz).date())}"
        return f"⏰ {fmt_dt(occ.due_at, tz, now=now)}"
    pending = _pending(reminders)
    if pending:
        return _notify_times(pending, tz, now)
    if occ.due_at <= now:
        return f"⏰ {fmt_dt(occ.due_at, tz, now=now)} · просрочено"
    if occ.precision == "default":  # reminders off, and the time was only the bot's choice
        return "🔕 без напоминания"
    return f"🔕 {fmt_dt(occ.due_at, tz, now=now)}"


def time_text(
    item: Item, occ: Occurrence | None, reminders: Sequence[Reminder], prefs: Prefs, now: datetime
) -> str | None:
    """The card's time line as plain text, for the Mini App: "🔔 сегодня 13:00", "🔔 вс 11 окт 10:00, пн 12 окт
    09:00", "🔔 завтра 09:30 и 10:15", "🗓 на этой неделе (до вс 11 окт) · 🔔 пт 19:00", "⏳ до пт 9 окт · 🔔 чт
    19:00, пт 10:00", "🌙 когда-нибудь"."""
    if item.someday:
        occ = None
    line = _time_line(item, occ, reminders, prefs, now)
    return html.unescape(_TAG_RE.sub("", line)) if line else None


def row_when(row: Row, prefs: Prefs, now: datetime) -> str | None:
    """Due of a list row as plain text, for the Mini App: "сегодня 13:00", "на этой неделе", "когда-нибудь"."""
    return _row_when(row, prefs, now)


def step_when(step: Step, prefs: Prefs, now: datetime) -> str | None:
    """Time of a checklist step as plain text ("чт 8 окт 15:00"), for the Mini App."""
    return _step_when(step, prefs, now)


def repeat_text(rule: str | None) -> str | None:
    """A recurrence rule in Russian words ("каждый пн"); the rule itself when it cannot be described."""
    if not rule:
        return None
    try:
        return describe_rrule_ru(rule)
    except ValueError:
        return rule


def _recurrence_line(item: Item) -> str | None:
    if not item.rrule:
        return None
    if not item.rec_active:
        return "🔁 повтор остановлен"
    return f"🔁 {esc(describe_rrule_ru(item.rrule))}"


def _tags_line(item: Item) -> str:
    tags = [f"#{q(t.lstrip('#'), TAG_MAX)}" for t in (item.tags or [])[:TAGS_SHOWN] if t and t.strip("#")]
    line = f"🏷 {esc(_kind_label(item.kind))}"
    return f"{line} · {' '.join(tags)}" if tags else line


_ORIGIN_TYPES: Final = frozenset({"user", "hidden_user", "chat", "channel"})


def _forward_origin(source: Any) -> dict[str, Any] | None:
    """The normalized forward origin inside ``Item.source`` (top level or under a forward key)."""
    if not isinstance(source, dict):
        return None
    for key in ("forward_origin", "forward", "origin"):
        if isinstance(source.get(key), dict):
            return source[key]
    return source if source.get("type") in _ORIGIN_TYPES else None


def _http_url(url: Any) -> bool:
    if not isinstance(url, str) or len(url) > HREF_MAX:
        return False
    try:
        return urlsplit(url).scheme in ("http", "https")
    except ValueError:
        return False


def _anchor(url: str | None, label_html: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{label_html}</a>' if _http_url(url) else label_html


def _source_line(item: Item) -> str | None:
    """Source of a forwarded post: "↪️ из «Хабр»" (linked to the original when the URL is known)."""
    origin = _forward_origin(item.source)
    if origin is not None:
        name = origin.get("title") or origin.get("sender_name") or origin.get("name")
        if not name and origin.get("username"):
            name = f"@{origin['username']}"
        url = origin.get("url")
    elif isinstance(item.source, dict) and isinstance(item.source.get("forward_from"), str):
        name, url = item.source["forward_from"], None
    else:
        return None
    if not name and not _http_url(url):
        return None
    label = f"«{q(str(name), 60)}»" if name else "оригинала"
    return f"↪️ из {_anchor(url, label)}"


def short_url(url: str) -> str:
    """Display form of a URL: "https://www.habr.com/ru/articles/1/" -> "habr.com/ru/articles/1" (clipped)."""
    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", url.strip())
    s = s.removeprefix("www.").rstrip("/")
    return clip(s, URL_LABEL_MAX)


def _link_titles(item: Item) -> dict[str, str]:
    src = item.source if isinstance(item.source, dict) else {}
    titles = src.get("link_titles")
    return titles if isinstance(titles, dict) else {}


def _link_line(item: Item) -> str | None:
    """Link line with the first URL: "🔗 habr.com/ru/articles/123" (the page title instead, when known)."""
    urls = [u for u in (item.urls or []) if isinstance(u, str) and u.strip()]
    if not urls:
        return None
    url = urls[0].strip()
    title = _link_titles(item).get(url)
    if isinstance(title, str) and title.strip():
        line = f"🔗 {_anchor(url, q(title, 80))} · {esc(short_url(urlsplit(url).netloc or url))}"
    else:
        line = f"🔗 {_anchor(url, esc(short_url(url)))}"
    return f"{line} (+{len(urls) - 1})" if len(urls) > 1 else line


def _warning_lines(item: Item, warnings: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for w in warnings:
        w = clip(w, WARNING_MAX)
        if w and w not in seen:
            seen.append(w)
    if not seen and item.needs_confirm:
        seen.append(NEEDS_CONFIRM_HINT)
    return [f"⚠️ {esc(w)}" for w in seen[:WARNINGS_SHOWN]]


def _parent_line(parent_title: str | None) -> str | None:
    return f"↳ шаг из «{q(parent_title, PARENT_TITLE_MAX)}»" if parent_title else None


def _step_when(step: Step, prefs: Prefs, now: datetime) -> str | None:
    """Time of a checklist step: "чт 8 окт 15:00", the date alone when it has no time of day, a fuzzy span as words
    ("на следующей неделе", like list rows) rather than the date of its reminder (2)."""
    if step.due_at is None:
        return None
    tz = prefs.zone
    today = _local(now, tz).date()
    kind, start, end = step.period_kind, step.period_start, step.period_end
    if step.precision == "period" and kind in CALENDAR_PERIODS and start is not None and end is not None:
        return period_label(kind, start, end, today)
    if step.precision in ("date", "fallback", "period", "soon"):
        return fmt_date(_local(step.due_at, tz).date(), today=today)
    return fmt_dt(step.due_at, tz, now=now)


def checklist_lines(steps: Sequence[Step], prefs: Prefs, now: datetime) -> list[str]:
    """ "📋 Шаги: 1/3", then "☑ 1. Упаковать вещи", "☐ 2. Заказать машину · чт 8 окт 15:00"…"""
    if not steps:
        return []
    lines = [f"📋 Шаги: {sum(s.done for s in steps)}/{len(steps)}"]
    for step in steps:
        when = _step_when(step, prefs, now)
        line = f"{'☑' if step.done else '☐'} {step.position}. {q(step.title, STEP_TITLE_MAX)}"
        lines.append(f"{line} · {when}" if when else line)
    return lines


def _short_day(d: date, today: date) -> str:
    """ "12 сен", with the year when it is another one: "12 сен 2025"."""
    day = f"{d.day} {MONTHS_SHORT[d.month - 1]}"
    return day if d.year == today.year else f"{day} {d.year}"


def related_line(related: Sequence[RelatedRef], prefs: Prefs, now: datetime) -> str | None:
    """ "🔗 похоже на: «Купить наушники Sony» (12 сен), «Чехол» (3 авг) и ещё 1" (the dates the items were saved)."""
    if not related:
        return None
    tz = prefs.zone
    today = _local(now, tz).date()
    names = [
        f"«{q(r.title, 60)}» ({_short_day(_local(r.created_at, tz).date(), today)})" for r in related[:RELATED_SHOWN]
    ]
    more = len(related) - RELATED_SHOWN
    return "🔗 похоже на: " + ", ".join(names) + (f" и ещё {more}" if more > 0 else "")


def _can_split(item: Item, steps: Sequence[Step]) -> bool:
    """[🪄 Разбить на шаги]: an open top-level task without steps that is not a series (``items.can_split``)."""
    return (
        item.kind in SPLIT_KINDS
        and item.status == "open"
        and item.parent_id is None
        and not item.rec_active
        and not steps
    )


def item_card(
    item: Item,
    occ: Occurrence | None,
    reminders: Sequence[Reminder],
    prefs: Prefs,
    now: datetime,
    warnings: Sequence[str] = (),
    *,
    extras: CardExtras | None = None,
) -> View:
    """Card for a saved item: title, its parent (a step), time and reminders, recurrence, the checklist, category/tags,
    source, details, related items, warnings. ``extras`` (``query.card_extras``) carries the checklist, the related
    items and the parent; without it the card shows the item alone."""
    if item.status == "deleted":
        return deleted_card(item)
    if item.someday:  # no date on purpose: an old (cancelled or done) occurrence says nothing about it any more
        occ = None
    steps = extras.steps if extras is not None else ()
    related = extras.related if extras is not None else ()
    done = _occ_done(item, occ)
    lines = [f"{_kind_emoji(item.kind)} <b>{q(item.title)}</b>"]
    lines += filter(
        None,
        [
            _parent_line(extras.parent_title if extras is not None else None),
            _time_line(item, occ, reminders, prefs, now),
            _recurrence_line(item),
        ],
    )
    if done:
        lines.append("✅ сделано")
    lines += checklist_lines(steps, prefs, now)
    lines.append(_tags_line(item))
    lines += filter(None, [heard_line(extras.heard if extras is not None else None)])
    lines += filter(None, [_source_line(item), _link_line(item)])
    if item.details and clip(item.details, DETAILS_MAX) != clip(item.title, DETAILS_MAX):
        lines.append(f"<i>{q(item.details, DETAILS_MAX)}</i>")
    lines += filter(None, [related_line(related, prefs, now)])
    lines += _warning_lines(item, warnings)
    kb = item_card_keyboard(
        item.id,
        occurrence_id=occ.id if occ is not None else None,
        actionable=item.kind in ACTIONABLE,
        done=done,
        has_reminders=bool(_pending(reminders)) and not done,
        recurring=bool(item.rrule and item.rec_active),
        steps=[(s.item_id, s.position, s.done) for s in steps],
        split=extras is not None and _can_split(item, steps),  # unknown steps (no extras): no offer
        related=bool(related),
    )
    return fit("\n".join(lines)), kb


# --- voice notes: what the bot heard (docs/features/iteration-3.md §6) --------------------------------------------


def heard_line(heard: str | None) -> str | None:
    """ "🎤 «Купить наушники в обед»": the transcript of a voice note, one line of at most ``HEARD_MAX`` characters;
    None without one."""
    text = clip(heard, HEARD_MAX)
    return f"🎤 «{esc(text)}»" if text else None


def with_heard(view: View, heard: str | None) -> View:
    """A reply to a voice note (an answer, a question, an edit…) with the "🎤 «…»" line on top."""
    line = heard_line(heard)
    if line is None:
        return view
    text, kb = view
    return fit(f"{line}\n{text}"), kb


def heard_item(heard: str, item: Item) -> MenuView:
    """A voice note that gave only notes / ideas (normally just 👌): "🎤 «…» → 💡 Идея «…»" with [✏️ Изменить]
    [🗑 Не то — удалить] (``ItemCb`` edit / del, as on a card), so a garbled transcript shows at once. One message
    per item: the buttons (which turn the message into that item's menu or deleted card) and a reply to it are about
    that one item."""
    label = f"{_kind_emoji(item.kind)} {esc(_kind_label(item.kind))} «{q(item.title, LIST_TITLE_MAX)}»"
    kb = markup(
        [
            button("✏️ Изменить", ItemCb(a="edit", id=item.id)),
            button("🗑 Не то — удалить", ItemCb(a="del", id=item.id), style="danger"),
        ]
    )
    return fit(f"🎤 «{q(heard, HEARD_MAX)}» → {label}"), kb


UNHEARD_TEXT: Final = "🎤 Не разобрал голосовое — повтори или напиши текстом"
UNHEARD_AGAIN_TEXT: Final = "🎤 Распознаю ещё раз…"


def unheard(raw_id: int) -> MenuView:
    """A voice note whose transcript was not speech, twice (docs/features/iteration-4.md §1): nothing was saved;
    [🔁 Ещё раз] transcribes the same file again (``AgainCb``)."""
    return UNHEARD_TEXT, markup([button("🔁 Ещё раз", AgainCb(raw=raw_id))])


def unheard_again() -> View:
    """The "Не разобрал" message after [🔁 Ещё раз]: the outcome comes as a reply to the voice note itself."""
    return UNHEARD_AGAIN_TEXT, None


def steps_closed_text(title: str) -> str:
    """The separate message when the last step of a checklist is done."""
    return f"🎉 Все шаги выполнены — «{q(title)}» закрыт"


def deleted_card(item: Item) -> MenuView:
    """Card of a deleted item: "🗑 Удалено: …" with [↩️ Вернуть]."""
    text = f"🗑 Удалено: {_kind_emoji(item.kind)} <s>{q(item.title)}</s>"
    return text, markup([button("↩️ Вернуть", ItemCb(a="undel", id=item.id))])


# --- edits applied by description (docs/features/iteration-3.md §3) ----------------------------------------------


def edit_applied(
    item: Item,
    occ: Occurrence | None,
    reminders: Sequence[Reminder],
    patch: EditPatch,
    prefs: Prefs,
    now: datetime,
    *,
    title_before: str,
    moved: bool,
    changed: bool = True,
    undo: tuple[int, int] | None = None,
    steps_closed: int = 0,
    warnings: Sequence[str] = (),
) -> View:
    """Short reply to an edit applied at once by description ("закрой задачу про документацию"), a line per change:
    "🗑 Удалил «…»", "✅ Закрыл «…» и 2 шага" (a series: "· следующий раз завтра 09:00"), "↩️ Снова открыл «…»",
    "✏️ Переименовал «…» → «…»", "🏷 «…» — теперь идея", "#️⃣ Теги «…»: #a #b", "🕒 Перенёс «…» на завтра 10:00" (and
    the card's time line under it when that says more: a span, a deadline, several notifications, no reminder),
    "🔔 Напомню о «…» сегодня 14:00" ("напомни за час"), "🌙 Отложил «…» на когда-нибудь", "🔁 «…» — каждый пн",
    "🔕 Выключил напоминания «…»", "⏹ Остановил повтор «…»"; then the warnings of a new time. ``moved``: the edit
    changed the item's time (otherwise a time it asked for was not understood); ``changed``: it changed anything at
    all. ``undo`` = (raw id, item id) of [↩️ Отменить] (None: no button)."""
    title = q(item.title, LIST_TITLE_MAX)
    timed = patch.when is not None or bool(patch.rrule) or bool(patch.lead)
    lines: list[str] = []
    if patch.action == "delete":
        lines.append(f"🗑 Удалил «{title}»")
    else:
        if item.title != title_before:
            lines.append(f"✏️ Переименовал «{q(title_before, LIST_TITLE_MAX)}» → «{title}»")
        if patch.action == "done" or (patch.action == "stop_recurrence" and not item.rrule):
            done_line = _closed_line(item, occ, title, steps_closed, prefs, now)
            lines.append(done_line if changed else f"✅ «{title}» — уже сделано")
        elif patch.action == "undone":
            lines.append(f"↩️ Снова открыл «{title}»" if changed else f"↩️ «{title}» — и так не сделано")
        if patch.kind:
            lines.append(f"🏷 «{title}» — теперь {esc(_kind_label(item.kind).lower())}")
        if patch.tags_add or patch.tags_remove:
            tags = " ".join(f"#{q(t, TAG_MAX)}" for t in (item.tags or [])[:TAGS_SHOWN]) or "нет"
            lines.append(f"#️⃣ Теги «{title}»: {tags}")
        if timed:
            lines += _time_change_lines(item, occ, reminders, patch, title, prefs, now, moved=moved)
        if patch.action == "no_reminder":
            lines.append(f"🔕 Выключил напоминания «{title}»")
        elif patch.action == "stop_recurrence" and item.rrule:
            closed = occ is not None and occ.status == "done" and occ.done_at == item.done_at  # closed with it
            lines.append(f"⏹ Остановил повтор «{title}»" + (" и закрыл этот раз" if closed else ""))
    if not lines:
        lines.append(f"✏️ Изменил «{title}»" if changed else f"🤷 В «{title}» ничего не изменилось")
    if timed and patch.action != "delete":
        lines += _warning_lines(item, warnings)
    return fit("\n".join(lines)), undo_keyboard(*undo) if undo is not None else None


def _closed_line(item: Item, occ: Occurrence | None, title: str, steps: int, prefs: Prefs, now: datetime) -> str:
    """ "✅ Закрыл «…»" (+ " и 2 шага" closed with a parent, " · следующий раз завтра 09:00" of a series)."""
    line = f"✅ Закрыл «{title}»"
    if steps:
        line += f" и {steps} {plural_ru(steps, 'шаг', 'шага', 'шагов')}"
    if item.rec_active and item.rrule and occ is not None and occ.status == "open":
        line += f" · следующий раз {fmt_dt(occ.due_at, prefs.zone, now=now)}"
    return line


def _time_change_lines(
    item: Item,
    occ: Occurrence | None,
    reminders: Sequence[Reminder],
    patch: EditPatch,
    title: str,
    prefs: Prefs,
    now: datetime,
    *,
    moved: bool,
) -> list[str]:
    """What a new time / repetition / "за N" of an edit did, in a line or two (see ``edit_applied``)."""
    line = _time_line(item, occ, reminders, prefs, now)
    if patch.rrule and item.rrule and item.rec_active:
        rule = repeat_text(item.rrule) or item.rrule
        return [f"🔁 «{title}» — {esc(rule)}", *filter(None, [line])]
    if not moved:
        return [f"🤷 Время «{title}» не изменилось — не понял, на когда"] if patch.when is not None else []
    if item.someday:
        return [f"🌙 Отложил «{title}» на когда-нибудь"]
    if occ is None or occ.status != "open":
        return [f"🕒 Перенёс «{title}»"]
    if occ.precision in ("period", "soon") or _is_deadline(item, occ):
        return [f"🕒 Перенёс «{title}»", *filter(None, [line])]
    when = fmt_dt(occ.due_at, prefs.zone, now=now)
    head = f"🕒 Перенёс «{title}» на {when}" if patch.when is not None else f"🔔 Напомню о «{title}» {when}"
    return [head] if line in (None, f"🔔 {when}") else [head, line]


def edit_menu(item: Item) -> MenuView:
    """Field menu; open actionable items also get [✅ Готово] here (the card itself has no done button)."""
    text = (
        f"✏️ <b>{q(item.title)}</b>\n"
        "Что изменить?\n"
        "<i>Можно просто ответить на карточку текстом, например «перенеси на завтра 15:00», «напомни за час»"
        " или «это идея».</i>"
    )
    done_row = (
        [button("✅ Готово", ItemCb(a="done", id=item.id), style="success")]
        if item.kind in ACTIONABLE and item.status == "open"
        else []
    )
    kb = markup(
        done_row,
        [button("🕒 Время", ItemCb(a="time", id=item.id)), button("🏷 Категория", ItemCb(a="kind", id=item.id))],
        [button("#️⃣ Теги", ItemCb(a="tags", id=item.id)), button("✏️ Название", ItemCb(a="title", id=item.id))],
        back_to_card_row(item.id),
    )
    return text, kb


def kind_menu(item: Item) -> MenuView:
    """The 9 categories (current one marked ✓)."""
    buttons = [
        button(f"{'✓ ' if k == item.kind else ''}{KIND_EMOJI[k]} {KIND_LABELS[k]}", KindCb(id=item.id, k=k))
        for k in KINDS
    ]
    text = f"🏷 Категория для <b>{q(item.title)}</b>:"
    return text, markup(*chunked(buttons, 3), back_to_card_row(item.id))


# --- reminders ---------------------------------------------------------------------------------------------------


def _details_line(item: Item) -> str | None:
    return f"<i>{q(item.details, DETAILS_MAX)}</i>" if item.details else None


def reminder_message(
    item: Item,
    occ: Occurrence,
    reminder: Reminder | None,
    prefs: Prefs,
    now: datetime,
    *,
    parent_title: str | None = None,
) -> MenuView:
    """Reminder: "🔔 <b>Купить наушники</b>" (the item's time is this notification, so no time line), then for a span
    "🗓 на этой неделе (до вс 11 окт)", for a deadline "⏳ срок: завтра" / "⏳ срок: сегодня в 15:00"; a reminder
    that comes noticeably late (``_late_line``) says when it was for. A step of a checklist names its parent
    (``parent_title``): "↳ шаг из «Переезд»". The buttons follow the item's type (``reminder_kind``,
    ``keyboards.reminder_keyboard``, docs/features/iteration-4.md §3): [✅ Готово] / [⏰ 1 час] [⏰ 3 часа]
    [📅 Завтра] for a one-off at a time, [➡️ След. неделя] (or «След. месяц») [📅 Выбрать день] [💤 Когда-нибудь] for
    a span, "на днях" or a deadline, [⏰ 1 час] [⏹ Хватит повторять] for a series.

    ``reminder`` is the one being sent (None when the message is re-rendered, e.g. after "↩️ Не сделано").
    """
    text = _reminder_text(item, occ, reminder, prefs, now, parent_title=parent_title)
    kind = reminder_kind(item, occ)
    return text, reminder_keyboard(occ.id, kind=kind, month=kind == "span" and item.period_kind == "month")


def _reminder_text(
    item: Item,
    occ: Occurrence,
    reminder: Reminder | None,
    prefs: Prefs,
    now: datetime,
    *,
    parent_title: str | None = None,
) -> str:
    lines = [f"🔔 <b>{q(item.title)}</b>"]
    lines += filter(
        None,
        [
            _reminder_context(item, occ, prefs, now),
            _late_line(reminder, prefs, now),
            _parent_line(parent_title),
            _recurrence_line(item),
            _details_line(item),
        ],
    )
    return fit("\n".join(lines))


def reminder_kind(item: Item, occ: Occurrence) -> ReminderKind:
    """Which buttons a reminder of this occurrence gets (docs/features/iteration-4.md §3): ``series`` (an active
    repetition), ``span`` (a week / month / season, "на днях", a deadline), ``timed`` (any other one-off: an exact time,
    a slot, "через N минут", a bare date or the bot's default time)."""
    if item.rec_active and item.rrule:
        return "series"
    if occ.precision in ("period", "soon") or _is_deadline(item, occ):
        return "span"
    return "timed"


def day_picker(
    item: Item, occ: Occurrence, days: Sequence[date], prefs: Prefs, now: datetime, *, parent_title: str | None = None
) -> MenuView:
    """[📅 Выбрать день] pressed: the reminder with "📅 На какой день перенести?" and the days (``items.picker_days``),
    [Другая дата…] (asked as text, like "🕒 Время") and [← Назад]."""
    text = _reminder_text(item, occ, None, prefs, now, parent_title=parent_title)
    return fit(f"{text}\n\n📅 На какой день перенести?"), day_picker_keyboard(occ.id, item.id, days)


def reminder_moved(
    item: Item, occ: Occurrence | None, reminders: Sequence[Reminder], prefs: Prefs, now: datetime
) -> View:
    """A reminder after [➡️ След. неделя] / [➡️ След. месяц] or a day of the picker: "🔔 <b>…</b>" and "➡️ перенёс ·
    🗓 на следующей неделе (до вс 1 ноя) · 🔔 пн 10:00, пт 19:00" (the card's time line), without buttons: the moved
    item reminds again then."""
    line = _time_line(item, occ, reminders, prefs, now) if occ is not None else None
    lines = [f"🔔 <b>{q(item.title)}</b>", f"➡️ перенёс · {line}" if line else "➡️ перенёс"]
    return fit("\n".join(lines)), None


def reminder_someday(item: Item) -> View:
    """A reminder after [💤 Когда-нибудь]: no date, no reminders any more; the item waits in /someday."""
    return fit(f"🔔 <b>{q(item.title)}</b>\n💤 отложил на когда-нибудь — запись в /someday"), None


def reminder_stopped(item: Item) -> View:
    """A series' reminder after [⏹ Хватит повторять] when its instance could not be closed (no longer open): the
    repetition is stopped, nothing more comes."""
    return fit(f"🔔 <b>{q(item.title)}</b>\n⏹ повтор остановлен — больше не напомню"), None


def _reminder_context(item: Item, occ: Occurrence, prefs: Prefs, now: datetime) -> str | None:
    """What a reminder says besides the title: the span ("🗓 на этой неделе (до вс 11 окт)") or the deadline
    ("⏳ срок: завтра", "⏳ срок: сегодня в 15:00"); None for an ordinary time."""
    tz = prefs.zone
    today = _local(now, tz).date()
    head = _period_head(item, occ, today)
    if head is not None and occ.precision == "period":
        return head
    if _is_deadline(item, occ):
        local = _local(occ.due_at, tz)
        day = fmt_date(local.date(), today=today)
        if occ.precision in ("date", "fallback"):
            return f"⏳ срок: {day}"
        return f"⏳ срок: {day} в {fmt_time(local.time())}"
    return None


def _late_line(reminder: Reminder | None, prefs: Prefs, now: datetime) -> str | None:
    """ "⏰ сегодня 13:00 · опоздал на 5 ч" for a reminder sent more than ``LATE_AFTER`` after its time (the PC slept,
    the bot was down: the scheduler still sends it within ``missed_grace_minutes``; one held over quiet hours counts
    from its own time, ``held_from``) — without it the owner would read the bare title as "now". None for one on
    time."""
    if reminder is None:
        return None
    at = reminder.held_from or reminder.fire_at
    if now - at <= LATE_AFTER:
        return None
    minutes = round((now - at).total_seconds() / 60)
    return f"⏰ {fmt_dt(at, prefs.zone, now=now)} · опоздал на {_rough_duration(minutes)}"


def _rough_duration(minutes: int) -> str:
    """ "5 мин", "2 ч 30 мин"; a day or more is rounded to hours ("1 дн 2 ч")."""
    if minutes >= 1440:
        minutes = round(minutes / 60) * 60
    return fmt_duration(minutes)


def reminder_done(item: Item, occ: Occurrence, prefs: Prefs, now: datetime) -> MenuView:
    """Reminder edited after "Готово": "✅ <s>Купить наушники</s>" [↩️ Не сделано]; an instance of a series that still
    repeats also gets [⏹ Больше не повторять] (docs/features/iteration-4.md §2)."""
    lines = [f"✅ <s>{q(item.title)}</s>"]
    head = _period_head(item, occ, _local(now, prefs.zone).date()) if occ.due_at is not None else None
    if head is not None and occ.precision == "period":
        lines.append(f"<i>{head.removeprefix('🗓 ')} · сделано</i>")
    elif occ.due_at is not None:
        lines.append(f"<i>{fmt_dt(occ.due_at, prefs.zone, now=now)} · сделано</i>")
    rec = _recurrence_line(item)
    if rec:
        lines.append(rec)
    return fit("\n".join(lines)), undone_keyboard(occ.id, stop=bool(item.rec_active and item.rrule))


done_text = reminder_done  # contract name (architecture §4.18)


def reminder_snoozed(item: Item, reminder: Reminder, prefs: Prefs, now: datetime) -> View:
    """Reminder edited after a snooze button: "💤 отложено · напомню сегодня в 13:15" (the item's time moved there),
    without buttons (the snoozed reminder comes again with its own [✅ Готово] / [⏰ …])."""
    tz = prefs.zone
    fire = _local(reminder.fire_at, tz)
    when = f"{fmt_date(fire.date(), today=_local(now, tz).date())} в {fmt_time(fire.time())}"
    lines = [f"🔔 <b>{q(item.title)}</b>", f"💤 отложено · напомню {when}"]
    return fit("\n".join(lines)), None


def downtime_notice(
    start: datetime,
    end: datetime,
    prefs: Prefs,
    *,
    late: int = 0,
    held: int = 0,
    held_until: datetime | None = None,
    missed: int = 0,
    folded: date | None = None,
    morning: bool = False,
) -> str:
    """The one (silent) message after the bot was down (docs/features/iteration-3.md §5): "😴 Меня не было с 19:00 до
    23:19 (компьютер спал?). Опоздало напоминаний: 1. Итог дня — в утреннем дайджесте." ``late`` counts the reminders
    of the gap that come late, ``held`` of them wait for the end of quiet hours (``held_until``): "… 1 — пришлю в
    08:00"; ``missed``: too old to send ("Не прислал устаревших напоминаний: 2 — эти дела в /today"); ``folded``: the
    day whose evening digest goes into the morning digest (``morning``: it is still to come, else /today has them).
    Times are local; the start gets its day when it is not the day the bot came back ("с вчера 19:00 до 08:30")."""
    tz = prefs.zone
    back = _local(end, tz)
    since = _local(start, tz)
    since_text = fmt_time(since.time()) if since.date() == back.date() else fmt_dt(start, tz, now=end)
    parts = [f"😴 Меня не было с {since_text} до {fmt_time(back.time())} (компьютер спал?)."]
    if late:
        line = f"Опоздало напоминаний: {late}"
        if held and held_until is not None:
            at = fmt_time(_local(held_until, tz).time())
            line += f" — пришлю в {at}" if held >= late else f" ({held} пришлю в {at})"
        parts.append(f"{line}.")
    if missed:
        parts.append(f"Не прислал устаревших напоминаний: {missed} — эти дела в /today.")
    if folded is not None:
        whose = "Итог дня" if folded == back.date() else "Вчерашний итог"
        parts.append(f"{whose} — в утреннем дайджесте." if morning else f"{whose} не прислал — несделанное в /today.")
    return " ".join(parts)


# --- lists and digests -------------------------------------------------------------------------------------------


def row_done(row: Row) -> bool:
    """A list row is done: its occurrence (the item itself when it has none)."""
    return row.occ_status == "done" if row.occ_status is not None else row.status == "done"


def _row_title(row: Row) -> str:
    """ "📌 Купить наушники" (struck through when done); a step "(из «Переезд»)", a parent its progress "(2/5)",
    a series "🔁"."""
    title = q(row.title, LIST_TITLE_MAX)
    body = f"{_kind_emoji(row.kind)} {f'<s>{title}</s>' if row_done(row) else title}"
    if row.parent_title:
        body += f" (из «{q(row.parent_title, PARENT_TITLE_MAX)}»)"
    elif row.steps_total:
        body += f" ({row.steps_done}/{row.steps_total})"
    return f"{body} 🔁" if row.rrule else body


def _row_when(row: Row, prefs: Prefs, now: datetime, day: date | None = None) -> str | None:
    """Due time of a row: "13:00" when it is due on ``day``, otherwise "сегодня 13:00" / "пт 9 окт 15:00"; a fuzzy
    span as words ("на этой неделе"), "когда-нибудь"."""
    if row.someday:
        return "когда-нибудь"
    if row.period_kind in CALENDAR_PERIODS and row.period_start is not None and row.period_end is not None:
        return period_label(row.period_kind, row.period_start, row.period_end, _local(now, prefs.zone).date())
    if row.due_at is None:
        return None
    if day is not None and _local(row.due_at, prefs.zone).date() == day:
        return fmt_time(_local(row.due_at, prefs.zone).time())
    return fmt_dt(row.due_at, prefs.zone, now=now)


def row_line(row: Row, prefs: Prefs, now: datetime) -> str:
    """Compact row: "сегодня 13:00 · 🛒 Купить наушники" (done rows struck through, recurring marked 🔁)."""
    when = _row_when(row, prefs, now)
    return f"{when} · {_row_title(row)}" if when else _row_title(row)


def _block(title: str, lines: Sequence[str], empty: str = "Пусто") -> str:
    """Header (plain text, escaped here) + ``lines`` (as many as fit), or ``empty`` under it."""
    header = f"<b>{q(title)}</b>"
    if not lines:
        return f"{header}\n{q(empty, 500)}"
    return "\n".join([header, *fit_lines(lines, MAX_TEXT - tg_len(header) - 1)])


def rows_list(title: str, rows: Sequence[Row], prefs: Prefs, now: datetime, *, empty: str = "Пусто") -> View:
    """Header (plain text, escaped here) + compact lines "• сегодня 13:00 · 🛒 Купить наушники"."""
    return fit(_block(title, [f"• {row_line(r, prefs, now)}" for r in rows], empty)), None


def edit_candidates(rows: Sequence[Row], raw_id: int, prefs: Prefs, now: datetime) -> MenuView:
    """Numbered list of up to 5 items + buttons "1".."5" (PickCb)."""
    rows = list(rows)[:CANDIDATES_MAX]
    lines = ["✏️ Какую запись изменить?"]
    lines += [f"{i}. {row_line(r, prefs, now)}" for i, r in enumerate(rows, 1)]
    buttons = [button(str(i), PickCb(raw=raw_id, item=r.item_id)) for i, r in enumerate(rows, 1)]
    return fit("\n".join(lines)), markup(buttons)


def ask_message(ask: Ask, raw_id: int) -> MenuView:
    """Clarifying question with one button per option (AskCb with the option index)."""
    buttons = [[button(clip_label(opt), AskCb(raw=raw_id, n=i))] for i, opt in enumerate(ask.options)]
    return f"🤔 {q(ask.q, QUESTION_MAX)}", markup(*buttons)


# --- numbered lists: [✅ N] / [↩️ N] (docs/features/iteration-3.md §2) -------------------------------------------
#
# /today, /tasks and the digests number their open lines "1. 13:00 · 📌 Купить хлеб" and put [✅ 1] [✅ 2] … under
# them (``LIST_BUTTONS_PER_ROW`` a row, at most ``LIST_BUTTONS_MAX``; the lines past them keep "•" and get a hint).
# A pressed line turns "1. ✅ 13:00 · 📌 <s>Купить хлеб</s>" with [↩️ 1]. A list is a few sections (``ListCb.s``):
# ``*_sections`` split the fresh data into them, ``number_lines`` numbers a first render, ``pin_lines`` keeps the
# numbers of a re-render (``bot.lists``), and the renderers take the result as ``lines``.

LIST_BUTTONS_MAX: Final = 20
SEC_OPEN: Final = "o"  # due today (/today, morning) / the open tasks (/tasks)
SEC_OVERDUE: Final = "v"  # overdue
SEC_PERIOD: Final = "p"  # the running spans ("🗓 На этой неделе")
SEC_DONE: Final = "d"  # done that day (never numbered)
SEC_MISSED: Final = "m"  # evening: not done, already due
SEC_AHEAD: Final = "a"  # evening: still ahead tonight
SEC_YESTERDAY: Final = "y"  # morning: not done yesterday, whose evening digest could not be sent (§5)
# Per list, the sections whose open lines are numbered, in numbering order.
NUMBERED_SECTIONS: Final[dict[str, tuple[str, ...]]] = {
    LIST_TODAY: (SEC_OPEN, SEC_OVERDUE, SEC_PERIOD),
    LIST_TASKS: (SEC_OPEN,),
    LIST_MORNING: (SEC_OPEN, SEC_YESTERDAY, SEC_OVERDUE, SEC_PERIOD),
    LIST_EVENING: (SEC_MISSED, SEC_AHEAD, SEC_OVERDUE),
}
TODAY_EMPTY: Final = "На сегодня ничего не запланировано."
YESTERDAY_TITLE: Final = "Вчерашний итог"
YESTERDAY_NOTE: Final = "не успел прислать"
TASKS_TITLE: Final = "📋 Открытые задачи"
TASKS_EMPTY: Final = "Открытых задач нет 🎉"
LIST_MORE_HINT: Final = "<i>Кнопки — у первых 20 дел; остальные — в /tasks или в Mini App</i>"
TASKS_MORE_HINT: Final = "<i>Кнопки — у первых 20 дел; остальные — в Mini App</i>"

type ListLine = tuple[Row, int | None]  # a row and the number of its [✅ N] / [↩️ N] (None: "•", no button)
type ListLines = dict[str, list[ListLine]]  # section -> its lines, in order


def today_sections(agenda: Agenda) -> dict[str, list[Row]]:
    """/today and the morning digest (which leaves the done ones out)."""
    return {SEC_OPEN: agenda.open, SEC_OVERDUE: agenda.overdue, SEC_PERIOD: agenda.period_items, SEC_DONE: agenda.done}


def morning_sections(data: DigestData) -> dict[str, list[Row]]:
    """The morning digest: ``today_sections`` and, when yesterday's evening digest went into it (``data.yesterday``,
    §5), yesterday's not-done rows in a section of their own — and not once more among the overdue."""
    sections = today_sections(data.agenda)
    if data.yesterday is not None:
        rows = list(data.yesterday.open)
        shown = {r.occurrence_id for r in rows}
        sections[SEC_YESTERDAY] = rows
        sections[SEC_OVERDUE] = [r for r in sections[SEC_OVERDUE] if r.occurrence_id not in shown]
    return sections


def evening_sections(agenda: Agenda, now: datetime) -> dict[str, list[Row]]:
    """Done; not done and already due (or without a time); still ahead tonight; overdue."""
    return {
        SEC_DONE: agenda.done,
        SEC_MISSED: [r for r in agenda.open if r.due_at is None or r.due_at <= now],
        SEC_AHEAD: [r for r in agenda.open if r.due_at is not None and r.due_at > now],
        SEC_OVERDUE: agenda.overdue,
    }


def tasks_sections(rows: Sequence[Row]) -> dict[str, list[Row]]:
    return {SEC_OPEN: list(rows)}


def number_lines(kind: str, sections: Mapping[str, Sequence[Row]]) -> ListLines:
    """A first render: the open rows of the list's numbered sections get 1, 2, … in order, up to
    ``LIST_BUTTONS_MAX``; every other row none."""
    lines: ListLines = {s: [(r, None) for r in rows] for s, rows in sections.items()}
    n = 0
    for section in NUMBERED_SECTIONS[kind]:
        entries = lines.get(section, [])
        for i, (row, _) in enumerate(entries):
            if n < LIST_BUTTONS_MAX and not row_done(row):
                n += 1
                entries[i] = (row, n)
    return lines


def pin_lines(kind: str, sections: Mapping[str, Sequence[Row]], pinned: Sequence[tuple[str, int, Row]]) -> ListLines:
    """A re-render after a press: the numbered lines of the message (``pinned``: section, number, the row as it is
    now) keep their sections and numbers, in number order; the rows of the fresh ``sections`` that are not among them
    (added since, past the first 20) follow without a number. /tasks tells rows apart by item (a series' next
    instance is the same line), the day lists by occurrence."""

    def key(row: Row) -> tuple[int, int | None]:
        return (row.item_id, None) if kind == LIST_TASKS else (row.item_id, row.occurrence_id)

    taken = {key(row) for _, _, row in pinned}
    lines: ListLines = {s: [] for s in sections}
    for section, n, row in sorted(pinned, key=lambda p: p[1]):
        lines.setdefault(section, []).append((row, n))
    for section, rows in sections.items():
        lines[section] += [(r, None) for r in rows if key(r) not in taken]
    return lines


class _Numbered:
    """Collects the numbered lines of a list while it is rendered; the keyboard gets a button for each one that made
    it into the final text (``fit`` may cut a long list)."""

    def __init__(self, kind: str, day: date | None, lines: ListLines) -> None:
        self.kind = kind
        self.day = day
        self.lines: list[tuple[str, int, Row, str]] = []
        self.unnumbered_open = False
        # The buttons ran out (more open lines than LIST_BUTTONS_MAX): lines without a number are those past the
        # first 20. Without that they are only rows added since the list was sent, and get no hint.
        self.full = any(n is not None and n >= LIST_BUTTONS_MAX for entries in lines.values() for _, n in entries)

    def line(self, section: str, entry: ListLine, body: str, glyph: str = "") -> str:
        """ "3. 13:00 · 📌 X", done "3. ✅ 13:00 · 📌 <s>X</s>", without a number "• 13:00 · 📌 X" (``glyph``, e.g.
        "⬜", stands for "•" and marks an open numbered line)."""
        row, n = entry
        if n is None:
            self.unnumbered_open |= not row_done(row)
            return f"{glyph or '•'} {body}"
        mark = "✅ " if row_done(row) else f"{glyph} " if glyph else ""
        text = f"{n}. {mark}{body}"
        self.lines.append((section, n, row, text))
        return text

    def hint(self) -> list[str]:
        """ "Кнопки — у первых 20 дел…" when open lines were left without a button because the buttons ran out."""
        if not self.unnumbered_open or not self.full:
            return []
        return [TASKS_MORE_HINT if self.kind == LIST_TASKS else LIST_MORE_HINT]

    def rows(self, text: str) -> list[list[InlineKeyboardButton]]:
        shown = set(text.split("\n"))
        return list_buttons(
            self.kind,
            self.day,
            [(s, n, r.occurrence_id, r.item_id, row_done(r)) for s, n, r, line in self.lines if line in shown],
        )


def today_list(agenda: Agenda, prefs: Prefs, now: datetime, *, lines: ListLines | None = None) -> View:
    """``/today``: today's not-done occurrences, then overdue ones, the running spans ("🗓 На этой неделе") and done
    ones (sections only when non-empty); the open lines are numbered, with [✅ N] buttons. ``lines``: a re-render
    (``pin_lines``)."""
    if lines is None:
        lines = number_lines(LIST_TODAY, today_sections(agenda))
    nb = _Numbered(LIST_TODAY, agenda.day, lines)
    title = f"📅 Сегодня, {fmt_date(agenda.day)}"
    open_lines = [nb.line(SEC_OPEN, e, row_line(e[0], prefs, now)) for e in lines.get(SEC_OPEN, [])]
    parts = [_block(title, open_lines, TODAY_EMPTY)]
    if overdue := lines.get(SEC_OVERDUE):
        parts.append(_block("⚠️ Просрочено", [nb.line(SEC_OVERDUE, e, row_line(e[0], prefs, now)) for e in overdue]))
    if period_lines := _period_lines(lines.get(SEC_PERIOD, []), prefs, now, nb):
        parts.append("\n".join(period_lines))
    if done := lines.get(SEC_DONE):
        parts.append(_block("✅ Сделано", [f"• {row_line(r, prefs, now)}" for r, _ in done]))
    parts += nb.hint()
    text = fit("\n\n".join(parts))
    return text, _list_markup(nb, text)


def tasks_list(rows: Sequence[Row], prefs: Prefs, now: datetime, *, lines: ListLines | None = None) -> View:
    """``/tasks``: the open tasks "1. сегодня 13:00 · 📌 Купить хлеб" with [✅ N] buttons. ``lines``: a re-render."""
    if lines is None:
        lines = number_lines(LIST_TASKS, tasks_sections(rows))
    nb = _Numbered(LIST_TASKS, None, lines)
    body = [nb.line(SEC_OPEN, e, row_line(e[0], prefs, now)) for e in lines.get(SEC_OPEN, [])]
    text = fit("\n\n".join([_block(TASKS_TITLE, body, TASKS_EMPTY), *nb.hint()]))
    return text, _list_markup(nb, text)


def _list_markup(nb: _Numbered, text: str, *extra: Sequence[InlineKeyboardButton]) -> InlineKeyboardMarkup | None:
    rows = [*nb.rows(text), *extra]
    return markup(*rows) if any(rows) else None


def period_sections(rows: Sequence[Row], prefs: Prefs, now: datetime) -> list[str]:
    """Items of the running spans, one section per span: "🗓 <b>На этой неделе</b> (до вс 11 окт)" then
    "• 📌 Посмотреть Raspberry Pi" lines; sections are separated by an empty line."""
    return _period_lines([(r, None) for r in rows], prefs, now, None)


def _period_lines(entries: Sequence[ListLine], prefs: Prefs, now: datetime, nb: _Numbered | None) -> list[str]:
    """``period_sections`` of list lines, numbered through ``nb``. A numbered line whose item has no span any more
    (snoozed past its time or given an exact one since the list was sent) keeps its number and button: it follows the
    spans with its time ("2. сегодня 20:00 · 📌 X")."""
    today = _local(now, prefs.zone).date()
    groups: dict[tuple[str, date], list[ListLine]] = {}
    loose: list[ListLine] = []
    for entry in entries:
        row = entry[0]
        if row.period_kind is None or row.period_start is None or row.period_end is None:
            if nb is not None and entry[1] is not None:
                loose.append(entry)
            continue
        label = period_label(row.period_kind, row.period_start, row.period_end, today)
        groups.setdefault((label, row.period_end), []).append(entry)
    lines: list[str] = []
    for (label, end), group in groups.items():
        if lines:
            lines.append("")
        lines.append(f"🗓 <b>{label[:1].upper()}{label[1:]}</b> (до {_span_end(end, today)})")
        for entry in group:
            title = _row_title(entry[0])
            lines.append(nb.line(SEC_PERIOD, entry, title) if nb is not None else f"• {title}")
    if nb is not None:
        lines += [nb.line(SEC_PERIOD, entry, row_line(entry[0], prefs, now)) for entry in loose]
    return lines


def morning_digest(data: DigestData, prefs: Prefs, now: datetime, *, lines: ListLines | None = None) -> View:
    """Morning plan "☀️ Доброе утро! План на сегодня, пт 9 окт": numbered open items, yesterday's summary when its
    evening digest could not be sent ("🌙 Вчерашний итог (не успел прислать)", §5), overdue, the running spans
    ("🗓 На этой неделе"), recurring count; [✅ N] under them. ``lines``: a re-render."""
    agenda = data.agenda
    if lines is None:
        lines = number_lines(LIST_MORNING, morning_sections(data))
    nb = _Numbered(LIST_MORNING, agenda.day, lines)
    head = [f"☀️ <b>Доброе утро!</b> План на сегодня, {fmt_date(agenda.day)}", ""]
    open_lines = [
        nb.line(SEC_OPEN, e, f"{_row_when(e[0], prefs, now, agenda.day) or '—'} · {_row_title(e[0])}")
        for e in lines.get(SEC_OPEN, [])
    ] or [TODAY_EMPTY]
    budget = MAX_TEXT - _TAIL_RESERVE - tg_len("\n".join(head))
    body = fit_lines(open_lines, budget)
    if data.yesterday is not None and (summary := _yesterday_lines(data.yesterday, lines, prefs, now, nb)):
        used = tg_len("\n".join(body))
        body += ["", *summary[:2], *fit_lines(summary[2:], max(budget - used - 60, 60))]
    if overdue := lines.get(SEC_OVERDUE):
        used = tg_len("\n".join(body))
        overdue_lines = [
            nb.line(SEC_OVERDUE, e, f"{_row_when(e[0], prefs, now) or '—'} · {_row_title(e[0])}") for e in overdue
        ]
        body += ["", "⚠️ <b>Просрочено</b>", *fit_lines(overdue_lines, budget - used - 30)]
    if period_lines := _period_lines(lines.get(SEC_PERIOD, []), prefs, now, nb):
        used = tg_len("\n".join(body))
        body += ["", *fit_lines(period_lines, max(budget - used - 30, 60))]
    if hint := nb.hint():
        body += ["", *hint]
    if data.recurring_today:
        body += ["", f"🔁 Повторяющихся сегодня: {data.recurring_today}"]
    text = fit("\n".join(head + body))
    return text, _list_markup(nb, text)


def _yesterday_lines(yesterday: Agenda, lines: ListLines, prefs: Prefs, now: datetime, nb: _Numbered) -> list[str]:
    """ "🌙 <b>Вчерашний итог</b> (не успел прислать)", "Сделано 1 из 3", then yesterday's not-done lines numbered
    like the rest of the morning digest ("4. 19:00 · 📌 Купить хлеб"); a line closed by its button since then counts
    as done (it is among ``yesterday.done`` then too: counted once). [] when yesterday had nothing to report."""
    entries = lines.get(SEC_YESTERDAY, [])
    listed = {r.occurrence_id for r, _ in entries}
    done = sum(r.occurrence_id not in listed for r in yesterday.done) + sum(row_done(r) for r, _ in entries)
    left = sum(not row_done(r) for r, _ in entries)
    if not done and not left:
        return []
    score = "Всё сделано 🎉" if not left else f"Сделано {done} из {done + left}"
    body = [
        nb.line(SEC_YESTERDAY, e, f"{_row_when(e[0], prefs, now, yesterday.day) or '—'} · {_row_title(e[0])}")
        for e in entries
    ]
    return [f"🌙 <b>{YESTERDAY_TITLE}</b> ({YESTERDAY_NOTE})", score, *body]


def evening_digest(data: DigestData, prefs: Prefs, now: datetime, *, lines: ListLines | None = None) -> View:
    """Evening "🌙 Итоги дня": done (struck through), not done (already due), still ahead tonight, overdue — the not
    done ones numbered, with [✅ N]; [📅 Перенести…] only when something already due is open (the button leaves
    tonight's later items alone). ``lines``: a re-render (a line closed by its button counts as done)."""
    agenda = data.agenda
    if lines is None:
        lines = number_lines(LIST_EVENING, evening_sections(agenda, now))
    nb = _Numbered(LIST_EVENING, agenda.day, lines)
    head = [f"🌙 <b>Итоги дня</b>, {fmt_date(agenda.day)}"]
    done = [r for r, _ in lines.get(SEC_DONE, [])]
    missed, ahead, overdue = (lines.get(s, []) for s in (SEC_MISSED, SEC_AHEAD, SEC_OVERDUE))
    if not done and not missed and not ahead and not overdue:
        return "\n".join([*head, "Сегодня задач не было."]), None
    missed_open = [r for r, _ in missed if not row_done(r)]
    ahead_open = [r for r, _ in ahead if not row_done(r)]
    done_count = len(done) + sum(row_done(r) for r, _ in (*missed, *ahead, *overdue))
    total = done_count + len(missed_open)
    if total:
        head.append("Всё сделано 🎉" if not missed_open and not ahead_open else f"Сделано {done_count} из {total}")
    budget = MAX_TEXT - _TAIL_RESERVE - tg_len("\n".join(head))
    day = agenda.day

    def body_of(row: Row, of_day: date | None = day) -> str:
        return f"{_row_when(row, prefs, now, of_day) or '—'} · {_row_title(row)}"

    sections = [
        ("<b>Сделано</b>", [f"✅ {_row_title(r)}" for r in done]),
        ("<b>Не сделано</b>", [nb.line(SEC_MISSED, e, body_of(e[0]), "⬜") for e in missed]),
        ("<b>Ещё сегодня</b>", [nb.line(SEC_AHEAD, e, body_of(e[0]), "🕒") for e in ahead]),
        ("<b>Просрочено</b>", [nb.line(SEC_OVERDUE, e, body_of(e[0], None), "⬜") for e in overdue]),
    ]
    body: list[str] = []
    for header, section_lines in sections:
        if section_lines:
            room = budget - tg_len("\n".join(body)) - 40
            body += ["", header, *fit_lines(section_lines, max(room, 60))]
    if hint := nb.hint():
        body += ["", *hint]
    text = fit("\n".join(head + body))
    move = bool(missed_open) or any(not row_done(r) for r, _ in overdue)
    return text, _list_markup(nb, text, digest_move_row(day) if move else [])


# --- usage / help ------------------------------------------------------------------------------------------------

PURPOSE_LABELS: Final[dict[str, str]] = {
    "extract": "разбор сообщений",
    "vision": "картинки",
    "stt": "голосовые",
    "answer": "ответы на вопросы",
    "split": "разбивка на шаги",
    "models": "прайс-лист",
}


def _bar(ratio: float, width: int = 10) -> str:
    filled = max(0, min(width, round(ratio * width)))
    return "▓" * filled + "░" * (width - filled)


def usage_text(summary: UsageSummary) -> str:
    """Monthly AI spend: total vs budget, calls, breakdown by purpose."""
    lines = [f"💰 <b>Расходы на ИИ</b> · {esc(summary.month)}"]
    if summary.limit_usd > 0:
        ratio = summary.spent_usd / summary.limit_usd
        lines.append(f"{fmt_money(summary.spent_usd)} из {fmt_money(summary.limit_usd)} ({ratio:.0%})")
        lines.append(_bar(ratio))
        if ratio >= 1:
            lines.append("⛔ Бюджет исчерпан: ИИ выключен до конца месяца, сообщения обработаются позже.")
        elif ratio >= 0.8:
            lines.append("⚠️ Израсходовано больше 80% бюджета.")
    else:
        lines.append(f"Потрачено {fmt_money(summary.spent_usd)}; бюджет $0 — ИИ выключен.")
    lines.append(f"Вызовов: {summary.calls}")
    for purpose, (calls, usd) in sorted(summary.by_purpose.items(), key=lambda kv: -kv[1][1]):
        lines.append(f"• {esc(PURPOSE_LABELS.get(purpose, purpose))} — {calls} · {fmt_money(usd)}")
    lines.append("<i>Лимит меняется в /settings → Бюджет.</i>")
    return fit("\n".join(lines))


def help_text() -> str:
    return "\n".join(
        [
            "📥 <b>Dump Bot</b> — личная «свалка»: присылай что угодно, я разберу, сохраню и напомню.",
            "",
            "<b>Что присылать</b>",
            "• текст, голосовые и аудиофайлы (до 5 мин), фото и скриншоты (можно файлом), ссылки, пересланные посты",
            "• кружочки сохраняю без распознавания (с подписью — разберу подпись)",
            "• в ответе на голосовое — 🎤 «…»: что я расслышал (заметку — с кнопкой «🗑 Не то — удалить»);"
            " шум без речи не сохраняю — «🔁 Ещё раз»",
            "• в одном сообщении может быть несколько дел — разберу на отдельные записи",
            "• «завтра в 15 созвон», «купить молоко», «каждый понедельник в 9 планёрка»",
            "• заметки, идеи, мемы и ссылки отмечаю реакцией 👌, задачи и события — карточкой с кнопками",
            "",
            "<b>Вопросы</b>",
            "• «что у меня завтра?», «какие идеи про отпуск я сохранял?»",
            "• начни сообщение с «?», чтобы оно точно считалось вопросом",
            "",
            "<b>Правка</b>",
            "• ответь (reply) на карточку текстом: «перенеси на пятницу 18:00», «напомни за час», «это идея», «удали»",
            "• запись без карточки (заметка, идея) — ответом на своё сообщение: «это задача, напомни вечером»",
            "• или нажми «✏️ Изменить» в карточке",
            "• или опиши: «закрой задачу про документацию», «купил молоко» — одну подходящую запись меняю сразу"
            " (↩️ Отменить вернёт как было), из нескольких — спрошу какую",
            "• правка исходного сообщения в Telegram запись не меняет",
            "",
            "<b>Напоминания</b>",
            "• время записи — это время напоминания: «встреча в 15» — напомню в 15:00, «в обед» — в 13:00;"
            " задачи без времени — вечером (19:00)",
            "• «напомни за 30 минут до встречи в 15» — напомню в 14:30, а 15:00 останется в названии;"
            " «за день и за час» — оба раза",
            "• «на этой неделе», «в октябре», «летом» — в последний рабочий день срока в 19:00;"
            " такие дела каждое утро в плане, пока срок идёт",
            "• «до пятницы», «к лету» (срок — 1 июня) — накануне в 19:00 и в сам день в 10:00,"
            " «на днях» — через 2 дня в 19:00",
            "• «когда-нибудь» — без напоминаний, такие записи в /someday",
            "• в напоминании: ✅ Готово, ⏰ на 1 или 3 часа, 📅 на завтра (запись переезжает);"
            " у дел «на неделе», «до пятницы» — ➡️ на след. неделю, 📅 выбрать день, 💤 когда-нибудь",
            "• не нажал «Готово» — повторно не напомню, задача будет в вечернем итоге",
            "• повторы: «каждый день в 9 и 18» — один повтор; «Готово» закрывает текущий раз;"
            " «⏹ Хватит повторять» или «больше не напоминай» — стоп",
            "• в тихие часы приходят только напоминания с явно заданным временем",
            "• компьютер спал: опоздавшее ночью напоминание придёт утром без звука, итог дня — в утреннем"
            " дайджесте, и я коротко напишу, что меня не было",
            "• по утрам — план на день, вечером — итоги",
            "",
            "<b>Списки</b>",
            "• в /today, /tasks и дайджестах дела пронумерованы, под списком кнопки ✅ 1 ✅ 2 … (до 20):"
            " нажал — дело закрыто, кнопка стала ↩️ — отменить; у повторяющихся закрывается"
            " текущий раз, у задачи с шагами — и шаги",
            "",
            "<b>Шаги и похожее</b>",
            "• «переезд в субботу, распиши по шагам» — задача с чек-листом; или кнопка «🪄 Разбить на шаги» в карточке,"
            " или ответ на карточку «разбей на шаги»",
            "• отмечай шаги кнопками ☐/☑ под карточкой; когда все отмечены, задача закроется сама 🎉",
            "• «Готово» у задачи отмечает все её шаги, «↩️ Не сделано» сразу после — снимает эти отметки;"
            " у задачи с шагами нет повтора",
            "• шаг без времени не напоминает и виден только в карточке; шаг со временем — как обычная задача"
            " с пометкой «(из «Переезд»)», в /tasks у задачи прогресс 2/5",
            "• «🔗 похоже на …» — подсказка о похожих старых записях (кнопка «🔗 Похожее»); дубли не объединяю",
            "",
            "<b>Приложение</b>",
            "• кнопка «📱 Свалка» слева от поля ввода открывает Mini App: сегодня, лента с поиском и картинками,"
            " задачи, карточки со всеми правками, «закинуть в свалку», вопросы, настройки и расходы",
            "• кнопка есть, когда приложение включено (README, «Mini App»); записи из приложения в чат не дублируются",
            "",
            "<b>Команды</b>",
            "/today — план на сегодня",
            "/tasks — открытые задачи",
            "/someday — когда-нибудь (без срока)",
            "/recent — последние записи",
            "/search <i>текст</i> — поиск",
            "/ask <i>вопрос</i> — вопрос по сохранённому",
            "/recurring — повторяющиеся задачи",
            "/settings — часовой пояс, время, тихие часы, дайджесты, бюджет, голос",
            "/usage — расходы на ИИ за месяц",
            "/cancel — отменить ввод",
            "/help — эта справка",
        ]
    )


# --- settings ----------------------------------------------------------------------------------------------------

TZ_CHOICES: Final[tuple[str, ...]] = ("Europe/Warsaw", "Europe/Berlin", "Europe/Minsk", "Europe/Kyiv")
BUDGET_CHOICES: Final[tuple[int, ...]] = (1, 3, 5, 10)
STT_CHOICES: Final[tuple[tuple[str, str], ...]] = (("auto", "Авто"), ("ru", "Русский"), ("en", "English"))
# Prefs.stt_mode -> (short label, button text).
STT_MODE_CHOICES: Final[dict[str, tuple[str, str]]] = {
    "auto": ("авто", "🔀 Авто: chutes, при сбое — локальный"),
    "local": ("локальный", "💻 Локальный, при сбое — chutes"),
    "chutes": ("только chutes", "☁️ Только chutes"),
}
NOT_CONFIGURED: Final = " (не настроен)"
UNREACHABLE: Final = " (недоступен)"  # the local Whisper could not be connected to: paused (iteration 4, §1)
SETTINGS_NOTE: Final = "<i>Изменения действуют для новых напоминаний — уже созданные не перепланируются.</i>"


class AskField(NamedTuple):
    label: str  # shown in the prompt: «Введи … для «утро»»
    kind: str  # time | tz | money
    menu: str  # submenu to return to


ASK_FIELDS: Final[dict[str, AskField]] = {
    "tz": AskField("часовой пояс", "tz", "tz"),
    "task_default_time": AskField("задача без времени", "time", "main"),
    **{f"slot_{s}": AskField(SLOT_LABELS[s], "time", "slots") for s in SLOT_NAMES},
    "weekend_time": AskField("на выходных (суббота)", "time", "slots"),
    "bare_date_time": AskField("дата без времени", "time", "slots"),
    "quiet_start": AskField("начало тихих часов", "time", "quiet"),
    "quiet_end": AskField("конец тихих часов", "time", "quiet"),
    "digest_morning_time": AskField("утренний дайджест", "time", "digest"),
    "digest_evening_time": AskField("вечерний дайджест", "time", "digest"),
    "budget_usd": AskField("бюджет на ИИ", "money", "budget"),
}

# SetCb key -> submenu to re-render after the change ("ask" -> ASK_FIELDS[v].menu).
SETTINGS_PARENT: Final[dict[str, str]] = {
    "tz": "tz",
    "quiet_toggle": "quiet",
    "digest_toggle": "digest",
    "stt": "stt",
    "stt_mode": "stt",
    "budget": "budget",
}

_SLOT_FIELDS: Final[tuple[str, ...]] = (
    *(f"slot_{s}" for s in SLOT_NAMES),
    "weekend_time",
    "bare_date_time",
    "task_default_time",
)


def _on_off(flag: bool) -> str:
    return "вкл" if flag else "выкл"


def _check(flag: bool) -> str:
    return "✓ " if flag else ""


def _stt_label(lang: str | None) -> str:
    return dict(STT_CHOICES).get(lang or "auto", lang or "Авто")


def _stt_mode_label(mode: str) -> str:
    return STT_MODE_CHOICES.get(mode, STT_MODE_CHOICES["auto"])[0]


def _back_row(menu: str = "main") -> list[InlineKeyboardButton]:
    return [button(BACK, SetCb(key="menu", v=menu))]


def _ask_button(text: str, field: str) -> InlineKeyboardButton:
    return button(text, SetCb(key="ask", v=field))


def _field_value(prefs: Prefs, field: str) -> str:
    value = getattr(prefs, field)
    if field == "budget_usd":
        return fmt_money(value)
    return fmt_time(value) if hasattr(value, "hour") else esc(value)


def settings_menu(prefs: Prefs) -> MenuView:
    """All current values + a button per group (the "🎙 Голос" submenu is ``settings_stt_menu``)."""
    slots = " · ".join(f"{SLOT_LABELS[s]} {fmt_time(prefs.slot(s))}" for s in SLOT_NAMES)
    lines = [
        "⚙️ <b>Настройки</b>",
        "",
        f"🌍 Часовой пояс: {esc(prefs.tz)}",
        f"📌 Задача без времени: {fmt_time(prefs.task_default_time)}",
        f"🕘 Время суток: {slots}",
        f"   выходные — сб {fmt_time(prefs.weekend_time)}, дата без времени — {fmt_time(prefs.bare_date_time)}",
        f"🌙 Тихие часы: {fmt_time(prefs.quiet_start)}–{fmt_time(prefs.quiet_end)} ({_on_off(prefs.quiet_enabled)})",
        f"📰 Дайджесты: утро {fmt_time(prefs.digest_morning_time)} ({_on_off(prefs.digest_morning_enabled)}),"
        f" вечер {fmt_time(prefs.digest_evening_time)} ({_on_off(prefs.digest_evening_enabled)})",
        f"💰 Бюджет: {fmt_money(prefs.budget_usd)} в месяц",
        f"🎙 Голос: язык {_stt_label(prefs.stt_language)}, распознавание {_stt_mode_label(prefs.stt_mode)}",
        "",
        SETTINGS_NOTE,
    ]
    kb = markup(
        [
            button("🌍 Часовой пояс", SetCb(key="menu", v="tz")),
            _ask_button("📌 Задача без времени", "task_default_time"),
        ],
        [button("🕘 Время суток", SetCb(key="menu", v="slots")), button("🌙 Тихие часы", SetCb(key="menu", v="quiet"))],
        [button("📰 Дайджесты", SetCb(key="menu", v="digest")), button("💰 Бюджет", SetCb(key="menu", v="budget"))],
        [button("🎙 Голос", SetCb(key="menu", v="stt"))],
        [button("✖️ Закрыть", SetCb(key="close"))],
    )
    return "\n".join(lines), kb


def settings_tz_menu(prefs: Prefs) -> MenuView:
    text = "\n".join(
        [
            "🌍 <b>Часовой пояс</b>",
            f"Сейчас: {esc(prefs.tz)}",
            "Выбери из списка или введи свой (IANA, например Europe/Warsaw).",
            "",
            SETTINGS_NOTE,
        ]
    )
    buttons = [button(f"{_check(tz == prefs.tz)}{tz}", SetCb(key="tz", v=tz)) for tz in TZ_CHOICES]
    return text, markup(*chunked(buttons, 2), [_ask_button("✍️ Другой…", "tz")], _back_row())


def settings_slots_menu(prefs: Prefs) -> MenuView:
    lines = ["🕘 <b>Время суток</b>", "Во сколько напоминать, когда сказано словами:", ""]
    lines += [f"{ASK_FIELDS[f].label} — {_field_value(prefs, f)}" for f in _SLOT_FIELDS]
    lines += ["", "Нажми, чтобы изменить.", SETTINGS_NOTE]
    buttons = [_ask_button(f"{ASK_FIELDS[f].label} {_field_value(prefs, f)}", f) for f in _SLOT_FIELDS]
    return "\n".join(lines), markup(*chunked(buttons, 2), _back_row())


def settings_quiet_menu(prefs: Prefs) -> MenuView:
    state = "включены" if prefs.quiet_enabled else "выключены"
    text = "\n".join(
        [
            f"🌙 <b>Тихие часы</b>: {fmt_time(prefs.quiet_start)}–{fmt_time(prefs.quiet_end)}, {state}",
            "В тихие часы не приходят автоматические сообщения (дайджесты, задачи без времени) — они придут"
            " в конце тихих часов. Явно заданное время («в 23:30») присылается как обычно.",
            "",
            SETTINGS_NOTE,
        ]
    )
    toggle = "🔔 Выключить тихие часы" if prefs.quiet_enabled else "🔕 Включить тихие часы"
    kb = markup(
        [button(toggle, SetCb(key="quiet_toggle"))],
        [
            _ask_button(f"Начало {fmt_time(prefs.quiet_start)}", "quiet_start"),
            _ask_button(f"Конец {fmt_time(prefs.quiet_end)}", "quiet_end"),
        ],
        _back_row(),
    )
    return text, kb


def settings_digest_menu(prefs: Prefs) -> MenuView:
    m_on, e_on = prefs.digest_morning_enabled, prefs.digest_evening_enabled
    text = "\n".join(
        [
            "📰 <b>Дайджесты</b>",
            f"☀️ Утренний (план на день): {fmt_time(prefs.digest_morning_time)}, {_on_off(m_on)}",
            f"🌙 Вечерний (итоги дня): {fmt_time(prefs.digest_evening_time)}, {_on_off(e_on)}",
            "В тихие часы дайджест придёт после их окончания.",
        ]
    )
    kb = markup(
        [
            button(f"☀️ {'Выключить' if m_on else 'Включить'} утренний", SetCb(key="digest_toggle", v="morning")),
            button(f"🌙 {'Выключить' if e_on else 'Включить'} вечерний", SetCb(key="digest_toggle", v="evening")),
        ],
        [
            _ask_button(f"☀️ Время {fmt_time(prefs.digest_morning_time)}", "digest_morning_time"),
            _ask_button(f"🌙 Время {fmt_time(prefs.digest_evening_time)}", "digest_evening_time"),
        ],
        _back_row(),
    )
    return text, kb


def settings_stt_menu(prefs: Prefs, *, local_stt: bool = False, local_down: bool = False) -> MenuView:
    """Voice: the language and the speech-to-text mode. ``local_stt``: the local Whisper is configured
    (``LOCAL_STT_URL``); without it "local" is marked «(не настроен)» and every mode works as chutes. ``local_down``:
    it could not be connected to and is paused (``RoutedSTT.local_paused``): "local" is marked «(недоступен)»."""
    language = prefs.stt_language or "auto"
    mode = prefs.stt_mode if prefs.stt_mode in STT_MODE_CHOICES else "auto"
    lines = [
        "🎙 <b>Голос</b>",
        f"Язык: {_stt_label(prefs.stt_language)}",
        f"Распознавание: {_stt_mode_label(mode)}",
        "",
        "Язык «Авто» — распознавание само определит язык; укажи язык, если часто ошибается.",
        "Распознаю голосовые и аудиофайлы (пересланные тоже) до 5 минут; кружочки — нет.",
        "Локальный Whisper бесплатный и работает на твоём компьютере; chutes.ai — платный (из бюджета).",
    ]
    if not local_stt:
        lines.append(
            "<i>Локальный Whisper не настроен (LOCAL_STT_URL пуст): все режимы работают как «только chutes».</i>"
        )
    elif local_down:
        lines.append("<i>Локальный Whisper сейчас недоступен — пока распознаю через chutes, проверю снова позже.</i>")
    languages = [button(f"{_check(code == language)}{label}", SetCb(key="stt", v=code)) for code, label in STT_CHOICES]
    modes = [
        [
            button(
                f"{_check(code == mode)}{label}{_stt_mode_note(code, local_stt, local_down)}",
                SetCb(key="stt_mode", v=code),
            )
        ]
        for code, (_, label) in STT_MODE_CHOICES.items()
    ]
    return "\n".join(lines), markup(languages, *modes, _back_row())


def _stt_mode_note(code: str, local_stt: bool, local_down: bool = False) -> str:
    if code != "local":
        return ""
    if not local_stt:
        return NOT_CONFIGURED
    return UNREACHABLE if local_down else ""


def settings_budget_menu(prefs: Prefs) -> MenuView:
    text = "\n".join(
        [
            "💰 <b>Бюджет на ИИ</b>",
            f"Сейчас: {fmt_money(prefs.budget_usd)} в месяц",
            "При 80% придёт предупреждение, при 100% ИИ выключится до конца месяца — сообщения сохранятся"
            " и обработаются позже. $0 — ИИ выключен. Расходы: /usage.",
        ]
    )
    buttons = [
        button(f"{_check(abs(prefs.budget_usd - usd) < 1e-9)}${usd}", SetCb(key="budget", v=str(usd)))
        for usd in BUDGET_CHOICES
    ]
    return text, markup(buttons, [_ask_button("✍️ Другая сумма", "budget_usd")], _back_row())


SETTINGS_MENUS: Final[dict[str, Callable[[Prefs], MenuView]]] = {
    "main": settings_menu,
    "tz": settings_tz_menu,
    "slots": settings_slots_menu,
    "quiet": settings_quiet_menu,
    "digest": settings_digest_menu,
    "stt": settings_stt_menu,
    "budget": settings_budget_menu,
}


def settings_view(name: str, prefs: Prefs, *, local_stt: bool = False, local_down: bool = False) -> MenuView:
    """Menu for ``SetCb(key="menu", v=name)``; unknown names fall back to the main menu. ``local_stt``: the local
    Whisper is configured, ``local_down``: it is paused as unreachable (the voice menu shows both)."""
    if name == "stt":
        return settings_stt_menu(prefs, local_stt=local_stt, local_down=local_down)
    return SETTINGS_MENUS.get(name, settings_menu)(prefs)


_ASK_HINTS: Final[dict[str, str]] = {
    "time": "в формате ЧЧ:ММ, например 09:30",
    "tz": "— название IANA, например Europe/Warsaw или Asia/Tbilisi",
    "money": "— сумма в долларах в месяц, например 5 или 7.5",
}


def settings_ask(field: str, prefs: Prefs) -> MenuView:
    """Prompt for ``SetCb(key="ask", v=field)`` with [✖️ Отмена] back to the field's submenu. KeyError if unknown."""
    spec = ASK_FIELDS[field]
    text = (
        f"✍️ Введи значение для «{esc(spec.label)}» {_ASK_HINTS[spec.kind]}.\n"
        f"Сейчас: {_field_value(prefs, field)}\n"
        "<i>/cancel — отмена</i>"
    )
    return text, markup([button("✖️ Отмена", SetCb(key="menu", v=spec.menu))])
