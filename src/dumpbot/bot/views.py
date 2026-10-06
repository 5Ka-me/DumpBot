"""Bot message texts and keyboards (HTML parse mode). Pure functions, no I/O.

Builders return ``(text_html, markup | None)``; ``usage_text`` and ``help_text`` return text only.

Rules:
- every user/LLM string is passed through ``html.escape(s, quote=False)`` (``href`` values with ``quote=True``);
- user strings are clipped *before* they are escaped and wrapped in tags, and each line keeps its tags balanced,
  so ``fit`` can drop whole lines to stay within Telegram's 4096-character limit (counted in UTF-16 units);
- dates are rendered in Russian by ``domain.fmt`` relative to the ``now`` argument (never the wall clock).

Re-rendering in place (for the callbacks router):
- card after done / undone / kind / lead / norem / stoprec / back -> ``item_card`` with fresh item, occurrence and
  pending reminders (a done occurrence shows "✅ сделано" and [↩️ Не сделано]);
- reminder message after ``OccCb done`` -> ``reminder_done`` (alias ``done_text``); after ``undone`` ->
  ``reminder_message(item, occ, None, ...)``; after ``s15``/``s60``/``tmr`` -> ``reminder_snoozed`` with the new
  reminder returned by ``items.snooze``;
- after ``ItemCb del`` -> ``deleted_card``; after ``undel`` -> ``item_card``.

Settings callbacks (``SetCb(key, v)``) emitted by the settings menus:

=================  =========================================  ==================================================
key                v                                          action
=================  =========================================  ==================================================
``menu``           ``main`` | ``tz`` | ``lead`` | ``slots``   show that (sub)menu in place: ``settings_view(v)``
                   | ``quiet`` | ``digest`` | ``stt``
                   | ``budget``
``close``          ``""``                                     close the menu (remove the keyboard)
``tz``             IANA name, e.g. ``Europe/Warsaw``          set ``Prefs.tz``
``lead``           minutes: ``0`` ``5`` ``10`` ``15`` ``30``  set ``Prefs.lead_minutes``
``quiet_toggle``   ``""``                                     flip ``Prefs.quiet_enabled``
``digest_toggle``  ``morning`` | ``evening``                  flip ``Prefs.digest_<v>_enabled``
``stt``            ``auto`` | ``ru`` | ``en``                 set ``Prefs.stt_language`` (``auto`` -> None)
``budget``         dollars: ``1`` ``3`` ``5`` ``10``          set ``Prefs.budget_usd``
``ask``            a ``Prefs`` field name from ``ASK_FIELDS``  ask the user to type a value for that field: show
                                                              ``settings_ask(v, prefs)``, wait for text (FSM),
                                                              validate it (``HH:MM`` for times, IANA name for
                                                              ``tz``, int minutes 0..10080 for ``lead_minutes``,
                                                              number >= 0 for ``budget_usd``), save, then show
                                                              ``settings_view(ASK_FIELDS[v].menu)``
=================  =========================================  ==================================================

After any change the router saves prefs, wakes the scheduler and re-renders the submenu the button came from
(``SETTINGS_PARENT`` maps keys to it). Changes apply to new reminders only; existing ones are not re-planned.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dumpbot.bot.cbdata import AskCb, ItemCb, KindCb, LeadCb, PickCb, SetCb
from dumpbot.bot.keyboards import (
    BACK,
    back_to_card_row,
    button,
    chunked,
    clip_label,
    digest_move_keyboard,
    done_only_keyboard,
    item_card_keyboard,
    markup,
    reminder_keyboard,
    undone_keyboard,
)
from dumpbot.domain.fmt import fmt_date, fmt_dt, fmt_duration, fmt_time
from dumpbot.domain.kinds import ACTIONABLE, KIND_EMOJI, KIND_LABELS, KINDS
from dumpbot.domain.prefs import SLOT_LABELS, SLOT_NAMES, Prefs
from dumpbot.domain.recurrence import describe_rrule_ru

if TYPE_CHECKING:
    from dumpbot.ai.schemas import Ask
    from dumpbot.db.models import Item, Occurrence, Reminder
    from dumpbot.dto import DigestData, Row, UsageSummary

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
# Room kept free for lines that follow a long list (section headers, counters).
_TAIL_RESERVE: Final = 300

PENDING: Final[frozenset[str | None]] = frozenset({None, "pending", "sending"})
LEAD_CHOICES: Final[tuple[int, ...]] = (0, 5, 15, 30, 60, 1440)  # item "remind before" menu
NEEDS_CONFIRM_HINT: Final = "проверь, правильно ли я понял"

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


def _rough_duration(minutes: int) -> str:
    """Duration for relative times: "5 мин", "2 ч 30 мин"; a day or more is rounded to hours ("1 дн 2 ч")."""
    if minutes >= 1440:
        minutes = round(minutes / 60) * 60
    return fmt_duration(minutes)


def _relative(due: datetime, now: datetime) -> str:
    minutes = round((due - now).total_seconds() / 60)
    if minutes == 0:
        return "сейчас"
    if minutes > 0:
        return f"через {_rough_duration(minutes)}"
    return f"просрочено на {_rough_duration(-minutes)}"


def fmt_money(usd: float) -> str:
    if 0 < usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"


def _kind_emoji(kind: str | None) -> str:
    return KIND_EMOJI.get(kind or "", "📦")


def _kind_label(kind: str | None) -> str:
    return KIND_LABELS.get(kind or "", kind or "Другое")


def _lead_setting(minutes: int) -> str:
    return "в момент события" if minutes == 0 else f"за {fmt_duration(minutes)}"


def _join_ru(parts: Sequence[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " и " + parts[-1]


# --- item card ---------------------------------------------------------------------------------------------------


def _pending(reminders: Sequence[Reminder]) -> list[Reminder]:
    return sorted((r for r in reminders if r.status in PENDING), key=lambda r: r.fire_at)


def _occ_done(item: Item, occ: Occurrence | None) -> bool:
    if occ is not None:
        return occ.status == "done"
    return item.status == "done"


def _reminder_phrase(due: datetime, pending: Sequence[Reminder], tz: ZoneInfo, now: datetime, *, with_day: bool) -> str:
    """Reminder part of the time line: "напомню в 12:55", "напомню завтра в 10:00", "напомню за 1 дн и за 1 ч"."""
    if not pending:
        return "просрочено" if due <= now else "без напоминания"
    today = _local(now, tz).date()
    if len(pending) == 1:
        fire = pending[0].fire_at
        if fire <= now + timedelta(minutes=1):
            return "напомню сейчас"
        fire_local = _local(fire, tz)
        if not with_day and fire_local.date() == _local(due, tz).date():
            return f"напомню в {fmt_time(fire_local.time())}"
        return f"напомню {fmt_date(fire_local.date(), today=today)} в {fmt_time(fire_local.time())}"
    leads = sorted({r.lead_minutes or 0 for r in pending}, reverse=True)
    due_time = fmt_time(_local(due, tz).time())
    return "напомню " + _join_ru([f"за {fmt_duration(m)}" if m > 0 else f"в {due_time}" for m in leads])


def _time_line(
    item: Item, occ: Occurrence | None, reminders: Sequence[Reminder], prefs: Prefs, now: datetime
) -> str | None:
    if occ is None or occ.due_at is None:
        return None
    tz = prefs.zone
    done = _occ_done(item, occ)
    if occ.precision == "default":
        if done:
            return None
        head = "без срока"
    elif occ.precision == "date":
        head = fmt_date(_local(occ.due_at, tz).date(), today=_local(now, tz).date())
    else:
        head = fmt_dt(occ.due_at, tz, now=now)
    if done or occ.status in ("missed", "cancelled"):
        return f"⏰ {head}"
    phrase = _reminder_phrase(occ.due_at, _pending(reminders), tz, now, with_day=occ.precision == "default")
    return f"⏰ {head} · {phrase}"


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


def item_card(
    item: Item,
    occ: Occurrence | None,
    reminders: Sequence[Reminder],
    prefs: Prefs,
    now: datetime,
    warnings: Sequence[str] = (),
) -> View:
    """Card for a saved item: title, time and reminders, recurrence, category/tags, source, details, warnings."""
    if item.status == "deleted":
        return deleted_card(item)
    done = _occ_done(item, occ)
    lines = [f"{_kind_emoji(item.kind)} <b>{q(item.title)}</b>"]
    lines += filter(None, [_time_line(item, occ, reminders, prefs, now), _recurrence_line(item)])
    if done:
        lines.append("✅ сделано")
    lines.append(_tags_line(item))
    lines += filter(None, [_source_line(item), _link_line(item)])
    if item.details and clip(item.details, DETAILS_MAX) != clip(item.title, DETAILS_MAX):
        lines.append(f"<i>{q(item.details, DETAILS_MAX)}</i>")
    lines += _warning_lines(item, warnings)
    kb = item_card_keyboard(
        item.id,
        occurrence_id=occ.id if occ is not None else None,
        actionable=item.kind in ACTIONABLE,
        done=done,
        has_reminders=bool(_pending(reminders)) and not done,
        recurring=bool(item.rrule and item.rec_active),
    )
    return fit("\n".join(lines)), kb


def deleted_card(item: Item) -> MenuView:
    """Card of a deleted item: "🗑 Удалено: …" with [↩️ Вернуть]."""
    text = f"🗑 Удалено: {_kind_emoji(item.kind)} <s>{q(item.title)}</s>"
    return text, markup([button("↩️ Вернуть", ItemCb(a="undel", id=item.id))])


def edit_menu(item: Item) -> MenuView:
    text = (
        f"✏️ <b>{q(item.title)}</b>\n"
        "Что изменить?\n"
        "<i>Можно просто ответить на карточку текстом, например «перенеси на завтра 15:00» или «это идея».</i>"
    )
    kb = markup(
        [button("🕒 Время", ItemCb(a="time", id=item.id)), button("🏷 Категория", ItemCb(a="kind", id=item.id))],
        [button("#️⃣ Теги", ItemCb(a="tags", id=item.id)), button("✏️ Название", ItemCb(a="title", id=item.id))],
        [button("⏱ Напомнить за…", ItemCb(a="lead", id=item.id)), *back_to_card_row(item.id)],
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


def lead_menu(item: Item) -> MenuView:
    """0/5/15/30/60/1440 minutes before (the item's own override marked ✓)."""
    current = set(item.lead_minutes or [])
    buttons = [
        button(f"{'✓ ' if m in current else ''}{'В момент' if m == 0 else fmt_duration(m)}", LeadCb(id=item.id, m=m))
        for m in LEAD_CHOICES
    ]
    now_text = _join_ru([_lead_setting(m) for m in sorted(current, reverse=True)])
    lines = [f"⏱ За сколько напомнить о <b>{q(item.title)}</b>?"]
    lines.append(f"Сейчас: {now_text}" if current else "Сейчас: как в настройках")
    return "\n".join(lines), markup(*chunked(buttons, 3), back_to_card_row(item.id))


# --- reminders ---------------------------------------------------------------------------------------------------


def _details_line(item: Item) -> str | None:
    return f"<i>{q(item.details, DETAILS_MAX)}</i>" if item.details else None


def reminder_message(item: Item, occ: Occurrence, reminder: Reminder | None, prefs: Prefs, now: datetime) -> MenuView:
    """Reminder: "🔔 <b>Купить наушники</b>", "через 5 мин · сегодня 13:00", then [✅ Готово] / [⏰ 15 мин]
    [⏰ 1 час] [📅 Завтра].

    ``reminder`` is accepted for the contract; the text depends only on the occurrence and ``now``.
    """
    lines = [f"🔔 <b>{q(item.title)}</b>", f"{_relative(occ.due_at, now)} · {fmt_dt(occ.due_at, prefs.zone, now=now)}"]
    lines += filter(None, [_recurrence_line(item), _details_line(item)])
    return fit("\n".join(lines)), reminder_keyboard(occ.id)


def reminder_done(item: Item, occ: Occurrence, prefs: Prefs, now: datetime) -> MenuView:
    """Reminder edited after "Готово": "✅ <s>Купить наушники</s>" [↩️ Не сделано]."""
    lines = [f"✅ <s>{q(item.title)}</s>"]
    if occ.due_at is not None:
        lines.append(f"<i>{fmt_dt(occ.due_at, prefs.zone, now=now)} · сделано</i>")
    rec = _recurrence_line(item)
    if rec:
        lines.append(rec)
    return fit("\n".join(lines)), undone_keyboard(occ.id)


done_text = reminder_done  # contract name (architecture §4.18)


def reminder_snoozed(item: Item, occ: Occurrence, reminder: Reminder, prefs: Prefs, now: datetime) -> MenuView:
    """Reminder edited after a snooze button: "💤 отложено · напомню сегодня в 13:15" [✅ Готово]."""
    tz = prefs.zone
    fire = _local(reminder.fire_at, tz)
    when = f"{fmt_date(fire.date(), today=_local(now, tz).date())} в {fmt_time(fire.time())}"
    lines = [f"🔔 <b>{q(item.title)}</b>", f"💤 отложено · напомню {when}"]
    return fit("\n".join(lines)), done_only_keyboard(occ.id)


# --- lists and digests -------------------------------------------------------------------------------------------


def _row_done(row: Row) -> bool:
    return row.occ_status == "done" if row.occ_status is not None else row.status == "done"


def _row_title(row: Row) -> str:
    title = q(row.title, LIST_TITLE_MAX)
    body = f"{_kind_emoji(row.kind)} {f'<s>{title}</s>' if _row_done(row) else title}"
    return f"{body} 🔁" if row.rrule else body


def _row_when(row: Row, prefs: Prefs, now: datetime, day: date | None = None) -> str | None:
    """Due time of a row: "13:00" when it is due on ``day``, otherwise "сегодня 13:00" / "пт 9 окт 15:00"."""
    if row.due_at is None:
        return None
    if day is not None and _local(row.due_at, prefs.zone).date() == day:
        return fmt_time(_local(row.due_at, prefs.zone).time())
    return fmt_dt(row.due_at, prefs.zone, now=now)


def row_line(row: Row, prefs: Prefs, now: datetime) -> str:
    """Compact row: "сегодня 13:00 · 🛒 Купить наушники" (done rows struck through, recurring marked 🔁)."""
    when = _row_when(row, prefs, now)
    return f"{when} · {_row_title(row)}" if when else _row_title(row)


def rows_list(title: str, rows: Sequence[Row], prefs: Prefs, now: datetime, *, empty: str = "Пусто") -> View:
    """Header (plain text, escaped here) + compact lines "• сегодня 13:00 · 🛒 Купить наушники"."""
    header = f"<b>{q(title)}</b>"
    if not rows:
        return f"{header}\n{q(empty, 500)}", None
    lines = fit_lines([f"• {row_line(r, prefs, now)}" for r in rows], MAX_TEXT - tg_len(header) - 1)
    return fit("\n".join([header, *lines])), None


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


def morning_digest(data: DigestData, prefs: Prefs, now: datetime) -> View:
    """Morning plan "☀️ Доброе утро! План на сегодня, пт 9 окт": numbered open items, overdue, recurring count."""
    agenda = data.agenda
    head = [f"☀️ <b>Доброе утро!</b> План на сегодня, {fmt_date(agenda.day)}", ""]
    if agenda.open:
        open_lines = [
            f"{i}. {_row_when(r, prefs, now, agenda.day) or '—'} · {_row_title(r)}"
            for i, r in enumerate(agenda.open, 1)
        ]
    else:
        open_lines = ["На сегодня ничего не запланировано."]
    budget = MAX_TEXT - _TAIL_RESERVE - tg_len("\n".join(head))
    body = fit_lines(open_lines, budget)
    if agenda.overdue:
        used = tg_len("\n".join(body))
        overdue = [f"• {_row_when(r, prefs, now) or '—'} · {_row_title(r)}" for r in agenda.overdue]
        body += ["", "⚠️ <b>Просрочено</b>", *fit_lines(overdue, budget - used - 30)]
    if data.recurring_today:
        body += ["", f"🔁 Повторяющихся сегодня: {data.recurring_today}"]
    return fit("\n".join(head + body)), None


def evening_digest(data: DigestData, prefs: Prefs, now: datetime) -> View:
    """Evening "🌙 Итоги дня": done (struck through) and not done; [📅 Перенести…] only when something is open."""
    agenda = data.agenda
    head = [f"🌙 <b>Итоги дня</b>, {fmt_date(agenda.day)}"]
    total = len(agenda.done) + len(agenda.open)
    if not total and not agenda.overdue:
        return "\n".join([*head, "Сегодня задач не было."]), None
    if total:
        head.append("Всё сделано 🎉" if not agenda.open else f"Сделано {len(agenda.done)} из {total}")
    budget = MAX_TEXT - _TAIL_RESERVE - tg_len("\n".join(head))
    body: list[str] = []
    sections = [
        ("<b>Сделано</b>", [f"✅ {_row_title(r)}" for r in agenda.done]),
        (
            "<b>Не сделано</b>",
            [f"⬜ {_row_when(r, prefs, now, agenda.day) or '—'} · {_row_title(r)}" for r in agenda.open],
        ),
        ("<b>Просрочено</b>", [f"⬜ {_row_when(r, prefs, now) or '—'} · {_row_title(r)}" for r in agenda.overdue]),
    ]
    for header, lines in sections:
        if lines:
            room = budget - tg_len("\n".join(body)) - 40
            body += ["", header, *fit_lines(lines, max(room, 60))]
    kb = digest_move_keyboard() if agenda.open or agenda.overdue else None
    return fit("\n".join(head + body)), kb


# --- usage / help ------------------------------------------------------------------------------------------------

PURPOSE_LABELS: Final[dict[str, str]] = {
    "extract": "разбор сообщений",
    "vision": "картинки",
    "stt": "голосовые",
    "answer": "ответы на вопросы",
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
            "• текст, голосовые, фото и скриншоты (можно файлом), ссылки, пересланные посты",
            "• в одном сообщении может быть несколько дел — разберу на отдельные записи",
            "• «завтра в 15 созвон», «купить молоко», «каждый понедельник в 9 планёрка, напомни за 10 минут»",
            "• заметки, идеи, мемы и ссылки отмечаю реакцией 👌, задачи и события — карточкой с кнопками",
            "",
            "<b>Вопросы</b>",
            "• «что у меня завтра?», «какие идеи про отпуск я сохранял?»",
            "• начни сообщение с «?», чтобы оно точно считалось вопросом",
            "",
            "<b>Правка</b>",
            "• ответь (reply) на карточку текстом: «перенеси на пятницу 18:00», «это идея», «удали»",
            "• или нажми «✏️ Изменить» в карточке",
            "• правка исходного сообщения в Telegram запись не меняет",
            "",
            "<b>Напоминания</b>",
            "• о событиях со временем — заранее (по умолчанию за 5 мин), задачи без времени — вечером (19:00)",
            "• в напоминании: ✅ Готово, ⏰ отложить на 15 мин / 1 час, 📅 на завтра",
            "• если не нажать «Готово», повторно не напомню — задача попадёт в вечерний итог",
            "• повторяющиеся задачи: «Готово» закрывает текущий раз, следующий создаётся сам",
            "• в тихие часы приходят только напоминания с явно заданным временем",
            "• по утрам — план на день, вечером — итоги",
            "",
            "<b>Команды</b>",
            "/today — план на сегодня",
            "/tasks — открытые задачи",
            "/recent — последние записи",
            "/search <i>текст</i> — поиск",
            "/ask <i>вопрос</i> — вопрос по сохранённому",
            "/recurring — повторяющиеся задачи",
            "/settings — часовой пояс, время, тихие часы, дайджесты, бюджет",
            "/usage — расходы на ИИ за месяц",
            "/cancel — отменить ввод",
            "/help — эта справка",
        ]
    )


# --- settings ----------------------------------------------------------------------------------------------------

TZ_CHOICES: Final[tuple[str, ...]] = ("Europe/Warsaw", "Europe/Berlin", "Europe/Minsk", "Europe/Kyiv")
SETTINGS_LEAD_CHOICES: Final[tuple[int, ...]] = (0, 5, 10, 15, 30)
BUDGET_CHOICES: Final[tuple[int, ...]] = (1, 3, 5, 10)
STT_CHOICES: Final[tuple[tuple[str, str], ...]] = (("auto", "Авто"), ("ru", "Русский"), ("en", "English"))
SETTINGS_NOTE: Final = "<i>Изменения действуют для новых напоминаний — уже созданные не перепланируются.</i>"


class AskField(NamedTuple):
    label: str  # shown in the prompt: «Введи … для «утро»»
    kind: str  # time | tz | minutes | money
    menu: str  # submenu to return to


ASK_FIELDS: Final[dict[str, AskField]] = {
    "tz": AskField("часовой пояс", "tz", "tz"),
    "task_default_time": AskField("задача без времени", "time", "main"),
    "lead_minutes": AskField("напоминать за", "minutes", "lead"),
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
    "lead": "lead",
    "quiet_toggle": "quiet",
    "digest_toggle": "digest",
    "stt": "stt",
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


def _back_row(menu: str = "main") -> list[InlineKeyboardButton]:
    return [button(BACK, SetCb(key="menu", v=menu))]


def _ask_button(text: str, field: str) -> InlineKeyboardButton:
    return button(text, SetCb(key="ask", v=field))


def _field_value(prefs: Prefs, field: str) -> str:
    value = getattr(prefs, field)
    if field == "lead_minutes":
        return _lead_setting(value)
    if field == "budget_usd":
        return fmt_money(value)
    return fmt_time(value) if hasattr(value, "hour") else esc(value)


def settings_menu(prefs: Prefs) -> MenuView:
    """All current values + a button per group."""
    slots = " · ".join(f"{SLOT_LABELS[s]} {fmt_time(prefs.slot(s))}" for s in SLOT_NAMES)
    lines = [
        "⚙️ <b>Настройки</b>",
        "",
        f"🌍 Часовой пояс: {esc(prefs.tz)}",
        f"📌 Задача без времени: {fmt_time(prefs.task_default_time)}",
        f"⏱ Напоминать: {_lead_setting(prefs.lead_minutes)}",
        f"🕘 Время суток: {slots}",
        f"   выходные — сб {fmt_time(prefs.weekend_time)}, дата без времени — {fmt_time(prefs.bare_date_time)}",
        f"🌙 Тихие часы: {fmt_time(prefs.quiet_start)}–{fmt_time(prefs.quiet_end)} ({_on_off(prefs.quiet_enabled)})",
        f"📰 Дайджесты: утро {fmt_time(prefs.digest_morning_time)} ({_on_off(prefs.digest_morning_enabled)}),"
        f" вечер {fmt_time(prefs.digest_evening_time)} ({_on_off(prefs.digest_evening_enabled)})",
        f"💰 Бюджет: {fmt_money(prefs.budget_usd)} в месяц",
        f"🎙 Язык голосовых: {_stt_label(prefs.stt_language)}",
        "",
        SETTINGS_NOTE,
    ]
    kb = markup(
        [
            button("🌍 Часовой пояс", SetCb(key="menu", v="tz")),
            _ask_button("📌 Задача без времени", "task_default_time"),
        ],
        [
            button("⏱ Напоминать за", SetCb(key="menu", v="lead")),
            button("🕘 Время суток", SetCb(key="menu", v="slots")),
        ],
        [button("🌙 Тихие часы", SetCb(key="menu", v="quiet")), button("📰 Дайджесты", SetCb(key="menu", v="digest"))],
        [button("💰 Бюджет", SetCb(key="menu", v="budget")), button("🎙 Голосовые", SetCb(key="menu", v="stt"))],
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


def settings_lead_menu(prefs: Prefs) -> MenuView:
    text = "\n".join(
        [
            "⏱ <b>Напоминать о событии</b>",
            f"Сейчас: {_lead_setting(prefs.lead_minutes)}",
            "Для событий с точным временем; в сообщении можно указать своё («напомни за 30 минут»).",
            "",
            SETTINGS_NOTE,
        ]
    )
    buttons = [
        button(
            f"{_check(m == prefs.lead_minutes)}{'В момент' if m == 0 else fmt_duration(m)}", SetCb(key="lead", v=str(m))
        )
        for m in SETTINGS_LEAD_CHOICES
    ]
    return text, markup(*chunked(buttons, 3), [_ask_button("✍️ Своё значение", "lead_minutes")], _back_row())


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


def settings_stt_menu(prefs: Prefs) -> MenuView:
    current = prefs.stt_language or "auto"
    text = "\n".join(
        [
            "🎙 <b>Язык голосовых</b>",
            f"Сейчас: {_stt_label(prefs.stt_language)}",
            "«Авто» — распознавание само определит язык; укажи язык, если часто ошибается.",
        ]
    )
    buttons = [button(f"{_check(code == current)}{label}", SetCb(key="stt", v=code)) for code, label in STT_CHOICES]
    return text, markup(buttons, _back_row())


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
    "lead": settings_lead_menu,
    "slots": settings_slots_menu,
    "quiet": settings_quiet_menu,
    "digest": settings_digest_menu,
    "stt": settings_stt_menu,
    "budget": settings_budget_menu,
}


def settings_view(name: str, prefs: Prefs) -> MenuView:
    """Menu for ``SetCb(key="menu", v=name)``; unknown names fall back to the main menu."""
    return SETTINGS_MENUS.get(name, settings_menu)(prefs)


_ASK_HINTS: Final[dict[str, str]] = {
    "time": "в формате ЧЧ:ММ, например 09:30",
    "tz": "— название IANA, например Europe/Warsaw или Asia/Tbilisi",
    "minutes": "— число минут от 0 до 10080, например 15",
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
