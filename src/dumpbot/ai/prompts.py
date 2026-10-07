"""Prompts for extraction and query answers.

The extraction system prompt is fully static (instructions + few-shot examples) and goes first so providers can
cache it; everything that changes per message (NOW, CAL, SRC, ..., MSG) goes into the user message.
Instructions are in English (fewer tokens); the model answers in the language of the message.

v2 (iteration 2, docs/features/iteration-2.md §A): ``when.period`` for fuzzy spans ("на этой неделе", "в октябре",
"летом", "на днях", "когда-нибудь") and deadlines "до конца недели / месяца".
v3 (§C): ``RELATED CANDIDATES`` in the input and ``items[].related``; ``items[].steps`` ("распиши по шагам") and
``edit.action = "split"``; a separate prompt for the "split" call (🪄 Разбить на шаги).
v4: a deadline by a season / month ("к лету", "до октября") is the first day of that span (``day`` MM-DD), not a
period: the code reads period + deadline as "до конца …", the end of the span.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from dumpbot.domain.prefs import Prefs

PROMPT_VERSION = "v4"

# Telegram caps text at 4096 and captions at 1024 chars, so a real message is always sent in full;
# this only guards against pathological inputs.
MAX_MSG_CHARS = 8000
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# (input as the model would see it, abbreviated; expected output). Every output must validate as Extraction.
EXAMPLES: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "купить наушники в обед",
        {
            "items": [
                {
                    "kind": "purchase",
                    "title": "Купить наушники",
                    "tags": ["электроника"],
                    "when": {"text": "в обед", "time": "lunch"},
                }
            ]
        },
    ),
    (
        "напомни за 30 минут до встречи в пятницу в 15",
        {
            "items": [
                {
                    "kind": "event",
                    "title": "Встреча",
                    "when": {"text": "в пятницу в 15", "day": "FR", "time": "15:00"},
                    "lead": [30],
                }
            ]
        },
    ),
    (
        "каждый понедельник выносить мусор",
        {
            "items": [
                {
                    "kind": "task",
                    "title": "Вынести мусор",
                    "when": {"text": "каждый понедельник"},
                    "rrule": "FREQ=WEEKLY;BYDAY=MO",
                }
            ]
        },
    ),
    (
        "платить за квартиру 5-го каждого месяца",
        {
            "items": [
                {
                    "kind": "task",
                    "title": "Заплатить за квартиру",
                    "when": {"text": "5-го каждого месяца", "day": "d5"},
                    "rrule": "FREQ=MONTHLY;BYMONTHDAY=5",
                }
            ]
        },
    ),
    (
        "на этой неделе посмотреть Raspberry Pi",
        {
            "items": [
                {
                    "kind": "task",
                    "title": "Посмотреть Raspberry Pi",
                    "when": {"text": "на этой неделе", "period": "this_week"},
                }
            ]
        },
    ),
    (
        "в октябре поменять шины",
        {"items": [{"kind": "task", "title": "Поменять шины", "when": {"text": "в октябре", "period": "2026-10"}}]},
    ),
    (
        "до пятницы сдать отчёт",
        {
            "items": [
                {
                    "kind": "task",
                    "title": "Сдать отчёт",
                    "when": {"text": "до пятницы", "day": "FR", "role": "deadline"},
                }
            ]
        },
    ),
    (
        "к лету сделать загранпаспорт",
        {
            "items": [
                {
                    "kind": "task",
                    "title": "Сделать загранпаспорт",
                    "when": {"text": "к лету", "day": "06-01", "role": "deadline"},
                }
            ]
        },
    ),
    (
        "на днях позвонить Пете",
        {"items": [{"kind": "task", "title": "Позвонить Пете", "when": {"text": "на днях", "period": "soon"}}]},
    ),
    (
        "когда-нибудь выучить испанский",
        {
            "items": [
                {
                    "kind": "task",
                    "title": "Выучить испанский",
                    "when": {"text": "когда-нибудь", "period": "someday"},
                }
            ]
        },
    ),
    (
        "переезд в субботу, распиши по шагам",
        {
            "items": [
                {
                    "kind": "task",
                    "title": "Переезд",
                    "when": {"text": "в субботу", "day": "SA"},
                    "steps": [
                        {"title": "Упаковать вещи"},
                        {"title": "Заказать машину"},
                        {"title": "Перевезти вещи"},
                        {"title": "Отдать ключи"},
                    ],
                }
            ]
        },
    ),
    (
        'RELATED CANDIDATES [{"id":12,"kind":"purchase","title":"Купить наушники Sony","date":"2026-09-12"},'
        '{"id":31,"kind":"note","title":"Соседи шумят","date":"2026-08-02"}] | купить чехол для наушников Sony',
        {
            "items": [
                {
                    "kind": "purchase",
                    "title": "Купить чехол для наушников Sony",
                    "tags": ["электроника"],
                    "related": [12],
                }
            ]
        },
    ),
    (
        "через 2 часа забрать посылку, а завтра позвонить маме",
        {
            "items": [
                {"kind": "task", "title": "Забрать посылку", "when": {"text": "через 2 часа", "in_min": 120}},
                {"kind": "task", "title": "Позвонить маме", "when": {"text": "завтра", "day": "+1"}},
            ]
        },
    ),
    (
        "buy milk after work",
        {"items": [{"kind": "purchase", "title": "Buy milk", "when": {"text": "after work", "time": "after_work"}}]},
    ),
    (
        "сегодня звонил Петя, просил перезвонить в четверг вечером",
        {
            "items": [
                {"kind": "note", "title": "Звонил Петя"},
                {
                    "kind": "task",
                    "title": "Перезвонить Пете",
                    "when": {"text": "в четверг вечером", "day": "TH", "time": "evening"},
                },
            ]
        },
    ),
    (
        "встреча с Олегом в 8",
        {
            "items": [
                {"kind": "event", "title": "Встреча с Олегом", "when": {"text": "в 8", "time": "20:00"}, "conf": "low"}
            ],
            "ask": {"q": "В 8 утра или вечера?", "options": ["08:00", "20:00"]},
        },
    ),
    ("что у меня завтра?", {"intent": "query", "query": {"range": "tomorrow"}}),
    ("когда я записан к стоматологу?", {"intent": "query", "query": {"text": "стоматолог", "answer": "summary"}}),
    (
        "перенеси встречу с Олегом на завтра 16:00",
        {
            "intent": "edit",
            "edit": {"target": "встреча с Олегом", "when": {"text": "на завтра 16:00", "day": "+1", "time": "16:00"}},
        },
    ),
    (
        'REPLYING TO ITEM {"id":7,"title":"Созвон","due_local":"2026-10-09 13:00"} | перенеси на 15:00',
        {"intent": "edit", "edit": {"when": {"text": "на 15:00", "day": "2026-10-09", "time": "15:00"}}},
    ),
    (
        'REPLYING TO ITEM {"id":9,"title":"Поменять шины"} | давай на следующей неделе',
        {"intent": "edit", "edit": {"when": {"text": "на следующей неделе", "period": "next_week"}}},
    ),
    (
        'REPLYING TO ITEM {"id":5,"title":"Ремонт на кухне"} | разбей на шаги',
        {"intent": "edit", "edit": {"action": "split"}},
    ),
    (
        "SRC photo | (cat meme with the text 'Код заработал с первого раза')",
        {
            "image": {"text": "Код заработал с первого раза", "description": "Мем с удивлённым котом"},
            "items": [{"kind": "meme", "title": "Код заработал с первого раза"}],
        },
    ),
    (
        "LINKS https://habr.com/p/1/: Как устроен asyncio | почитать https://habr.com/p/1/",
        {"items": [{"kind": "link", "title": "Как устроен asyncio", "tags": ["python"]}]},
    ),
)


def _render_examples() -> str:
    return "\n".join(f"> {src}\n{json.dumps(out, ensure_ascii=False, separators=(',', ':'))}" for src, out in EXAMPLES)


SYSTEM_PROMPT = f"""\
Convert ONE message from the owner of a personal notes/tasks bot into ONE JSON object per the schema. JSON only. \
Never invent facts. Omit empty, null and default fields.

INPUT lines: NOW, CAL (next 7 days), SRC (text | voice = transcript, may have errors | photo = image attached, \
MSG is its caption | forward | link | other), FORWARDED FROM, LINKS (url: title), REPLYING TO ITEM, \
CLARIFICATION (owner's answer to your question: apply it, don't ask again), MODE query, RELATED CANDIDATES (earlier \
items with similar words), MSG.

INTENT: save (default) | query = asks about OWN saved data or schedule ("что у меня завтра?", \
"какие идеи я сохранял?"); MODE query → always query | edit = changes an EXISTING item ("перенеси", "отмени", \
"удали", "готово"), with edit.target naming it; REPLYING TO ITEM → always edit (no target).

ITEMS: split independent things into separate items (one per thing to buy), at most 20; more than 8 things to buy \
→ ONE purchase item with the list in details.
kind: task = something to do | event = meeting, appointment, something happening at a time | purchase = something \
to buy | idea | note = thought, fact, diary | link = saved link/article | meme = humor, meme image | quote | other.
title: short (max 120 chars), imperative for tasks, in the message language. details: extra info only \
(max 600 chars). tags: 0-4 lowercase, no "#". conf: high|med|low.
related: ids from RELATED CANDIDATES really about the same thing (same purchase, project, follow-up), max 3; a shared \
word is not enough; usually omit. steps: only if the owner asks to plan it step by step (распиши по шагам, разбей на \
шаги, чек-лист) or lists the stages of ONE task: 2-8 short steps in order {{title, when only if the message gives \
that step a time}}; the item is the whole task.

TIME: describe it, NEVER compute dates. when = {{text: verbatim phrase, day, time, in_min, at, period, role}}
- day: +N days (+0 сегодня, +1 завтра, +2 послезавтра) | +Nw | +Nmo | +Ny | MO TU WE TH FR SA SU = nearest such day | \
"next FR" only for a weekday of next week ("в пятницу на следующей неделе") | dN = N-th of month (5-го → d5), \
d-1 = last day | MM-DD | YYYY-MM-DD | weekend.
- period, instead of day, for a fuzzy span: this_week (на этой неделе) | next_week (на следующей неделе) | \
this_month | next_month | YYYY-MM (в октябре; any year) | spring | summer | autumn | winter (весной, летом, осенью, \
зимой) | soon (на днях) | someday (когда-нибудь, потом как-нибудь: no date, no reminder).
- time: HH:MM (24h) or slot: morning (утром, с утра, in the morning), lunch (в обед, at lunch), afternoon (днём, \
после обеда, in the afternoon), evening (вечером, tonight), after_work (после работы, after work), \
night (ночью, перед сном, at night).
- Hours 1-7 without утра/ночи/am are PM ("в 3" → 15:00). A bare "в 8"/"в 9" is ambiguous: the likelier one, \
conf low, ask with both options.
- in_min only for "через N минут/часов" (2 часа → 120); "через 3 дня" → day +3.
- at: local YYYY-MM-DD[THH:MM] ONLY if day/time can't express it; use CAL.
- role: remind = the time to ping ("напомни в 15"); deadline = до/к/by ("до пятницы" → day FR; a season/month \
"к лету", "до октября" → day = its first day MM-DD: 06-01, 10-01; only "до конца недели/месяца" → period \
this_week/this_month); else event.
- lead, only if stated: за 30 минут → [30], за час → [60], за день → [1440].
- rrule only for explicit repetition (каждый, по понедельникам, every): RFC 5545 without DTSTART; time of day \
still in when.time.
- Past facts (звонил, был) get no when; no time mentioned → no when.
ask: only if a reminder would likely be wrong; 2-4 options; item = index in items.

IMAGES: image.text = all readable text verbatim, max 4000 chars ("" if none); image.description = one short \
sentence in Russian. Memes → kind meme.
LINKS: kind link, title = page title or a short description.
FORWARDED posts: usually note, link or idea, never edit (someone else's words); dates in them → conf low.

query = {{text: keywords, kinds, status open|done|any, date_field: due (scheduled) or created (when saved), range, \
tags, answer: list or summary (short answer: когда?, сколько?)}}
range: today|tomorrow|yesterday|this_week|next_week|last_week|this_month|last_month|past:N|next:N|YYYY-MM-DD\
|YYYY-MM-DD..YYYY-MM-DD

edit = {{target (which item, only without REPLYING TO ITEM), action: update|done|undone|delete|no_reminder\
|stop_recurrence|split (разбей на шаги), title, kind, tags_add, tags_remove, when (the new time; if only the time \
changes, keep the date of due_local as day YYYY-MM-DD), rrule, lead}}

EXAMPLES (NOW 2026-10-06T14:05 Tue)
{_render_examples()}
/no_think"""


ANSWER_SYSTEM_PROMPT = """\
You answer the owner's question about their own saved notes, tasks, events and reminders.
Use ONLY the rows given as JSON. Never invent items, dates or details. If the rows don't answer the question, \
say so briefly.
Reply in Russian, plain text (no HTML, no Markdown), at most 8 short lines. Dates in rows are local time; \
write them like "пт 9 окт 15:00" or "завтра 10:00" relative to NOW.
/no_think"""


SPLIT_SYSTEM_PROMPT = """\
Break ONE task of the owner of a personal tasks bot into 2-8 concrete steps in a sensible order. JSON only: \
{"steps": [{"title": "...", "when": {...}}]}. Never invent facts.
title: short imperative (max 80 chars) in the language of the task; every step needed to finish it, no filler \
("подготовиться", "начать"). The owner's NOTE (e.g. "на 3 шага", "начни с документов") wins.
when: only if TASK or NOTE gives that step a time, usually omit. NEVER compute dates: {"text": verbatim phrase, \
"day": "+N" | "MO".."SU" | "dN" | "YYYY-MM-DD", "time": "HH:MM" | "morning" | "lunch" | "afternoon" | "evening" \
| "after_work" | "night"}; use CAL.
Nothing sensible to split → {"steps": []}.
/no_think"""


def _one_line(text: str, limit: int = 300) -> str:
    return " ".join(str(text).split())[:limit]


def calendar_line(local_now: datetime) -> str:
    """``CAL`` line: the next 7 days with weekdays, so the model never has to do calendar maths."""
    days = (local_now.date() + timedelta(days=i) for i in range(1, 8))
    return "CAL " + ", ".join(f"{WEEKDAYS[d.weekday()]} {d.isoformat()}" for d in days)


def now_line(now: datetime, prefs: Prefs) -> str:
    local = now.astimezone(prefs.zone)
    return f"NOW {local:%Y-%m-%dT%H:%M} {WEEKDAYS[local.weekday()]} {prefs.tz}"


def build_user_text(
    *,
    text: str,
    source: str,
    now: datetime,
    prefs: Prefs,
    forward_from: str | None = None,
    link_titles: dict[str, str] | None = None,
    reply_item: dict | None = None,
    clarification: str | None = None,
    force_query: bool = False,
    related: list[dict[str, Any]] | None = None,
) -> str:
    """Dynamic part of the extraction prompt: NOW, CAL, SRC, optional context lines, then MSG (always last).

    ``related``: earlier items with similar words (``{id, kind, title, date}``), one JSON line
    ``RELATED CANDIDATES [...]``; the model may name some of them in ``items[].related``.
    """
    local = now.astimezone(prefs.zone)
    lines = [now_line(now, prefs), calendar_line(local), f"SRC {source}"]
    if forward_from:
        lines.append(f"FORWARDED FROM {_one_line(forward_from)}")
    if link_titles:
        lines.append("LINKS")
        lines.extend(f"{url}: {_one_line(title, 200)}" for url, title in link_titles.items())
    if reply_item is not None:
        lines.append("REPLYING TO ITEM " + json.dumps(reply_item, ensure_ascii=False, default=str))
    if clarification:
        lines.append(f"CLARIFICATION {_one_line(clarification)}")
    if force_query:
        lines.append("MODE query")
    if related:
        lines.append("RELATED CANDIDATES " + _compact_json(related))
    msg = (text or "").strip()[:MAX_MSG_CHARS]
    lines.append("MSG")
    lines.append(msg or "(empty)")
    return "\n".join(lines)


def build_split_text(*, task: dict[str, Any], now: datetime, prefs: Prefs, note: str | None = None) -> str:
    """User message of the "split" call: NOW, CAL, the owner's NOTE (their words, e.g. "разбей на 3 шага"), then
    TASK (the item as JSON, always last)."""
    lines = [now_line(now, prefs), calendar_line(now.astimezone(prefs.zone))]
    if note and note.strip():
        lines.append(f"NOTE {_one_line(note, 500)}")
    lines.append("TASK " + _compact_json(task))
    return "\n".join(lines)


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def build_answer_text(question: str, rows: list[dict], now: datetime, prefs: Prefs) -> str:
    """User message for ``answer_query``: NOW, the question and the rows as compact JSON."""
    return "\n".join(
        [
            now_line(now, prefs),
            f"QUESTION {_one_line(question, 1000)}",
            "ROWS " + json.dumps(rows, ensure_ascii=False, default=str, separators=(",", ":")),
        ]
    )
