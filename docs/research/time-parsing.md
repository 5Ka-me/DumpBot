# Date, time and recurrence extraction for the Telegram dump bot (RU+EN, chutes.ai, Europe/* DST)

Research done 2026-10-06. I checked the DST and rrule behaviour by running Python 3.13.7 with dateutil 2.9.0.post0 in memory. No files were written and nothing was installed.

## 1. Recommendation: the hybrid approach (c)

The LLM only describes when something happens, as a short structured spec. Python then works out the exact date and time using the user's settings and `zoneinfo`. For rare phrasings the spec can't express, the LLM may fill an `at` field with a local wall time (no UTC offset). That case is always flagged for the user to confirm.

| Criterion | (a) LLM → ISO-8601 | (b) spec → Python | **(c) hybrid** |
|---|---|---|---|
| Weekday and month maths, "в обед" at 14:00 → tomorrow | Done by the LLM. LLM date arithmetic is known to be fragile (MultiTempBench, 2026). | Deterministic | Deterministic for about 95% of cases |
| DST offset (+02 vs +01; the switch is on **2026-10-25**, 19 days away) | LLM must output the right offset | `zoneinfo` | `zoneinfo`; the LLM never outputs offsets |
| User vocabulary (lunch=13:00, after work=18:30…) | Must be in every prompt; changing a setting doesn't update stored items | Read from the DB when resolving | Same as (b) |
| Knowing whether the time was explicit or defaulted (needed for the 5-min lead and quiet hours) | Needs extra flags | Built in (`precision`) | Built in |
| Rare phrasings ("последняя пятница ноября") | Handles them | Fails | `at` fallback, marked for confirmation |
| Testability | Only LLM evals | Pure unit tests | Pure unit tests plus a small LLM eval |
| Works with a cheap model with thinking turned off | Risky | Yes | Yes |

**Recurrence:** the LLM outputs an RFC 5545 RRULE without DTSTART. Python validates it against an allowlist, then builds DTSTART from the resolved local time and the user's IANA timezone.

## 2. Date-parsing libraries (as validators or fallbacks)

| Library | Status (checked 2026-10-06) | Fit for this bot | Verdict |
|---|---|---|---|
| **dateparser** | 1.4.3, released 2026-09-03; releases 1.3.0→1.4.3 in 2026; Python ≥3.10; BSD-3 | `ru.py` has сегодня/завтра/послезавтра and regexes for `через N <день/час/минута/неделя/месяц/год>`. **No** time-of-day words (утром / в обед / вечером), no "после работы", no recurrence. Relevant settings: `RELATIVE_BASE`, `PREFER_DATES_FROM='future'`, `TIMEZONE`, `RETURN_AS_TIMEZONE_AWARE`, `DATE_ORDER='DMY'`. | **Use it** to cross-check `when.text`, and as a fallback when chutes is down |
| **Duckling** (Haskell) | facebook/duckling not archived; last commit 2026-03-15. `Duckling/Time/RU/Rules.hs` has `ruleLunch` (`"(в )?обед"`), `ruleInDuration` (`"через"`), morning/evening/night/weekend rules. **No** `кажд` (no recurrence) and **no** `числа` | Best rule-based Russian coverage, but needs a Haskell service. The `rasa/duckling` Docker image is over 4 years old. pyduckling-native: Linux x86_64 and Python 3.8–3.12 only, per search results | Skip for the MVP; possible later as a sidecar validator |
| natasha `DatesExtractor` | natasha 1.6.0 (2023-07-24) is the last release | The Date fact only has `year, month, day`. No relative dates, weekdays or times | Not useful |
| yargy | Not checked | You would have to write your own grammar | Not worth it |
| rutimeparser | Repo sits under `orsinium-archive` (archived) | — | Skip |
| MS Recognizers-Text | No Russian in its language matrix | — | Skip |
| **python-dateutil** | 2.9.0.post0 (2024-03-01) | `rrulestr`, `relativedelta` | **Required** |
| `zoneinfo` + **`tzdata`** (PyPI) | stdlib | The docs recommend the `tzdata` dependency where no system tz database exists (Windows, slim containers) | **Required.** Pin `tzdata` and rebuild the image periodically |

## 3. Extraction schema (Pydantic v2)

```python
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Category = Literal["work","home","health","finance","shopping","learning","fun","people","other"]  # from settings
SLOTS = ["morning","lunch","afternoon","evening","after_work","night"]                         # from settings
HHMM = r"([01]\d|2[0-3]):[0-5]\d"
DAY_RE = (r"^(\+\d{1,3}(d|w|mo|y)?"           # +0 today, +1 завтра, +2w, +1mo
          r"|(next )?(MO|TU|WE|TH|FR|SA|SU)"    # nearest upcoming / weekday of next week
          r"|d(-1|[1-9]|[12]\d|3[01])"          # 5-го числа -> d5 ; конец месяца -> d-1
          r"|(\d{4}-)?\d{2}-\d{2}"              # explicit date, year optional
          r"|weekend)$")

class S(BaseModel):
    model_config = ConfigDict(extra="forbid")

class When(S):
    text: str = Field(max_length=60)                       # verbatim span: used for validation and UI
    day: Annotated[str, StringConstraints(pattern=DAY_RE)] | None = None
    time: Annotated[str, StringConstraints(pattern=rf"^({HHMM}|{'|'.join(SLOTS)})$")] | None = None
    in_min: int | None = Field(None, ge=1, le=10080)        # "через 2 часа" -> 120 (elapsed time)
    at: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")  # rare cases, LOCAL wall time
    role: Literal["event","remind","deadline"] = "event"    # "напомни в 15" -> remind (no lead time)

class Item(S):
    kind: Literal["task","reminder","event","idea","note","purchase","link","meme","quote"]
    title: str = Field(max_length=100)                      # short, imperative, same language as the message
    details: str | None = Field(None, max_length=400)
    cat: Category
    tags: list[str] = Field(default_factory=list, max_length=4)
    when: When | None = None
    rrule: str | None = Field(None, pattern=r"^FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(;[A-Z]+=[-+,0-9A-Z]+)*$")
    lead: list[int] | None = Field(None, max_length=3)      # minutes before; overrides default 5 ("за день и за час" -> [1440,60])
    conf: Literal["high","med","low"] = "high"              # 3 levels: small models' floats aren't calibrated

class Ask(S):
    q: str = Field(max_length=120)
    options: list[str] = Field(min_length=2, max_length=3)  # on tap: re-run with "MSG + chosen option"

class Extraction(S):
    intent: Literal["save","query","edit"]                  # edit = "перенеси/отмени/готово" (can be stubbed in MVP)
    items: list[Item] = Field(default_factory=list, max_length=8)
    query: str | None = None                                 # normalized question for the DB-QA step
    ask: Ask | None = None
```

- **Categories and slots come from settings.** Inject them at startup, e.g. `Literal[tuple(cats)]` or `create_model`.
- **Sending the schema:** `response_format={"type":"json_schema","json_schema":{"name":"extraction","schema":Extraction.model_json_schema()}}` (SGLang format).
- **Always re-validate with Pydantic**, even with structured outputs on. On a `ValidationError`, retry once with the error text appended.
- **Checks the schema can't express:**
  - Only one of `day`/`at`/`in_min` per item.
  - `rrule` keys limited to {FREQ, INTERVAL, BYDAY, BYMONTHDAY, BYMONTH, BYSETPOS, COUNT, UNTIL, WKST}.
  - `rrulestr(...)` must parse.
  - Lead values between 0 and 10080.
  - Tags lower-cased and de-duplicated.

## 4. How Python resolves the spec

**Reference time.** `now` is the Telegram `message.date` converted to the user's timezone, not the processing time. This keeps "через 2 часа" correct when a message is re-processed after a chutes outage. If the reminder time has already passed by then, it fires immediately and is marked as late.

| Input | Rule (all values configurable in settings) |
|---|---|
| `in_min` | `now_utc + minutes` (elapsed time, so the maths is done in UTC) |
| `time` only (slot or HH:MM) | Today; if that time is ≤ now, **tomorrow**. So "в обед" at 14:05 → tomorrow 13:00 |
| `day` without time | `bare_date_time` = 10:00; `weekend` → Sat 11:00 |
| No `when`, kind=task | Next 19:00 (`task_default_time`) |
| Weekday is today and the time has passed | +7 days |
| `dN` already passed this month | Next month. `relativedelta(day=N)` clamps 31 → 30/28. `d-1` = last day of the month |
| `MM-DD` already passed | +1 year |
| Explicit `+0`/`YYYY-MM-DD` in the past | No reminder; ask for confirmation (it's probably a fact about the past) |
| Lead time | Applies only when `role=event` and the time is exact (e.g. "в 15:00"). Slots and default times fire exactly at that time. Leads under 1440 min are subtracted in UTC; multiples of 1440 are subtracted in wall-clock days |
| Quiet hours | Shift slot/default reminders to the end of quiet hours; leave exact, explicit times alone |
| `night_cutoff` (suggested 04:00) | Before 04:00, "завтра" means the same calendar date |

```python
from datetime import datetime, date, time, timedelta, timezone
from dateutil.rrule import rrulestr
UTC = timezone.utc

def localize(d: date, t: time, tz) -> datetime:
    # fold=0: ambiguous 02:30 on 2026-10-25 -> first (CEST) instance; non-existent 02:30 on
    # 2027-03-28 -> 03:30 CEST. Same as RFC 5545 §3.3.5 (first occurrence / offset before the gap).
    return datetime.combine(d, t, tzinfo=tz).astimezone(UTC).astimezone(tz)

def next_recurring(rrule: str, t: time, now: datetime) -> datetime:
    start = localize(now.date(), t, now.tzinfo)                  # wall-clock DTSTART in user tz
    nxt = rrulestr(rrule, dtstart=start).after(now)              # dateutil keeps wall time across DST
    return localize(nxt.date(), nxt.time(), now.tzinfo)          # normalize DST-gap instances
```

**DST behaviour I verified locally** (Europe/Berlin, dateutil 2.9.0.post0):
- A weekly 09:00 rule gives 2026-10-19 09:00+02 and then 2026-10-26 **09:00+01**, so the wall time is kept.
- A naive `UNTIL` with an aware DTSTART raises `RRULE UNTIL values must be specified in UTC when DTSTART is timezone-aware`. The RFC requires this, so convert `UNTIL` to `...Z` yourself.
- `rrule` yields a gap time like `2027-03-28T02:30+01:00` without normalizing it. That's why the round-trip through `localize` above is needed.
- `BYMONTHDAY=31` skips months that don't have a 31st. Use `-1` for "end of month".
- A DTSTART that doesn't match `BYDAY` is not returned as an occurrence (Tuesday DTSTART with `BYDAY=MO` → `after()` returns Monday 2026-10-12).
- Adding 1 day to 2026-10-24 19:00 gives 2026-10-25 19:00+01, while adding 24h gives 18:00. So days use wall-clock maths and hours use UTC.

**Storage:**
- One-off items: `due_at timestamptz` (UTC) plus the original spec JSON and the time precision.
- Recurring items: `rrule`, `dtstart_local` (timestamp without time zone) and `tz` (an IANA name). Never store a fixed UTC offset.
- **Timezone setting:** "CET" still works (locally it gave +2 in summer and +1 in winter). Since tz 2024b it is only a legacy alias for a geographic zone, so save a city name such as `Europe/Warsaw` or `Europe/Berlin`. Validate input with `ZoneInfo(name)`.

**Cross-check with dateparser.** Only run it when `when.text` has no slot word and there is no `rrule`. If its date differs from the resolver's, set `needs_confirm`.

```python
dateparser.parse(w.text, languages=["ru","en"], settings={"RELATIVE_BASE": now.replace(tzinfo=None),
  "PREFER_DATES_FROM":"future","TIMEZONE":tz_name,"RETURN_AS_TIMEZONE_AWARE":True,"DATE_ORDER":"DMY"})
```

`needs_confirm` is set by any of these:
- the model returned `conf=low`;
- the `at` fallback was used;
- dateparser disagrees;
- an hour of 1–7 with no "утра/ночи/am" in the span (should be read as PM);
- an explicit date that is in the past;
- the source is OCR or a forwarded post.

The acknowledgement message always shows the resolved weekday and date, e.g. "пт 9 окт 15:00, напомню в 14:30", with buttons.

## 5. System prompt outline

Write the instructions in English (fewer tokens); the output follows the message's language. Put the static part first so it can be served from cache. chutes lists an `input_cache_read` price, but I did not verify whether caching is applied automatically.

```
SYSTEM (static, ~650 tok + ~550 tok examples)
Convert ONE message from the owner into JSON per schema. JSON only. Never invent facts.
INTENT: query = asks about own saved data/schedule ("что у меня завтра", "когда я...", "покажи идеи");
        edit = changes existing item ("перенеси", "отмени", "сделано"); else save.
ITEMS: split independent things; one item per product to buy; title short+imperative; tags 0-4 lowercase.
TIME - describe, NEVER compute dates:
 day: +N days(+0 сегодня,+1 завтра,+2 послезавтра) | +Nw/+Nmo/+Ny | MO..SU nearest upcoming |
      "next FR" only for "на следующей неделе"/"next week" | dN = N-го числа, d-1 = конец месяца |
      MM-DD or YYYY-MM-DD | weekend
 time: HH:MM 24h, or slot: morning(утром,с утра) lunch(в обед,at lunch) afternoon(днём,после обеда)
      evening(вечером,tonight) after_work(после работы) night(ночью,перед сном).
      Hours 1-7 without утра/ночи/am = PM. "в 8"/"в 9" alone = ambiguous -> conf low + ask.
 in_min only for "через N минут/часов"; "через 3 дня" -> day +3.
 at: local YYYY-MM-DDTHH:MM ONLY if inexpressible above; use CAL line.
 role: remind if the time is when to ping ("напомни в 15"); deadline for "до/к"; else event.
 lead: "за 30 минут"/"за час"/"за день" -> [30]/[60]/[1440].
 rrule only for explicit repetition (каждый, по понедельникам, ежемесячно, every); RFC5545 w/o DTSTART;
      time goes to when.time.
 Past facts ("звонил","был") get no when. SRC ocr/forward: dates -> conf low.
 Omit unknown fields. ask only if a reminder would likely be wrong.
EXAMPLES (NOW Tue 2026-10-06 14:05):
купить наушники в обед
{"intent":"save","items":[{"kind":"purchase","title":"Купить наушники","cat":"shopping","tags":["электроника"],"when":{"text":"в обед","time":"lunch"}}]}
напомни за 30 минут до встречи в пятницу в 15
{"intent":"save","items":[{"kind":"event","title":"Встреча","cat":"work","when":{"text":"в пятницу в 15","day":"FR","time":"15:00"},"lead":[30]}]}
каждый понедельник выносить мусор
{"intent":"save","items":[{"kind":"task","title":"Вынести мусор","cat":"home","when":{"text":"каждый понедельник"},"rrule":"FREQ=WEEKLY;BYDAY=MO"}]}
оплатить интернет 5-го числа
{"intent":"save","items":[{"kind":"task","title":"Оплатить интернет","cat":"finance","when":{"text":"5-го числа","day":"d5","role":"deadline"}}]}
через 2 часа забрать посылку, а завтра позвонить маме
{"intent":"save","items":[{"kind":"task","title":"Забрать посылку","cat":"home","when":{"text":"через 2 часа","in_min":120}},{"kind":"task","title":"Позвонить маме","cat":"people","when":{"text":"завтра","day":"+1"}}]}
buy milk after work
{"intent":"save","items":[{"kind":"purchase","title":"Buy milk","cat":"shopping","when":{"text":"after work","time":"after_work"}}]}
сегодня звонил Петя, просил перезвонить в четверг вечером
{"intent":"save","items":[{"kind":"note","title":"Звонил Петя","cat":"people"},{"kind":"task","title":"Перезвонить Пете","cat":"people","when":{"text":"в четверг вечером","day":"TH","time":"evening"}}]}
встреча с Олегом в 8
{"intent":"save","items":[{"kind":"event","title":"Встреча с Олегом","cat":"people","when":{"text":"в 8","time":"20:00"},"conf":"low"}],"ask":{"q":"В 8 утра или вечера?","options":["08:00","20:00"]}}
что у меня завтра?
{"intent":"query","query":"что у меня завтра","items":[]}

USER (dynamic, ~100 tok):
NOW 2026-10-06 Tue 14:05 Europe/Berlin
CAL We07 Th08 Fr09 Sa10 Su11 Mo12 Tu13
SRC text|voice|ocr|forward
MSG <text>
```

**Call parameters:** `temperature=0`, `seed` (listed as supported), `max_tokens≈600`, thinking off. For Qwen3 that is `chat_template_kwargs={"enable_thinking": false}` (the Qwen3 card says it works on SGLang/vLLM), with `/no_think` in the system prompt as a backup.

## 6. Model choice and cost

Prices are from the public `https://llm.chutes.ai/v1/models` snapshot on 2026-10-06, per 1M tokens.

| Model ID | Input / output | Notes |
|---|---|---|
| `Qwen/Qwen3-32B-TEE` | $0.104 / $0.416 | `supported_features` includes `structured_outputs`; 40,960-token context; cache read $0.0104 |
| `google/gemma-4-31B-turbo-TEE` | $0.12 / $0.37 | `structured_outputs`; 131k context; accepts image input |
| `deepseek-ai/DeepSeek-V4-Flash-0731-TEE` | $0.44 / $1.32 | Fallback for low-confidence retries |

**Monthly estimate:** about 930 calls × (~1.3k input + ~0.15k output tokens) is roughly **$0.18 on Qwen3-32B, $0.20 on Gemma-4, $0.72 on DeepSeek-V4-Flash**. Leaving thinking on would multiply output tokens by about 3–10× and add seconds of delay; it still fits $5 but isn't needed with approach (c).

Recommendation: compare Qwen3-32B and Gemma-4-31B on a golden set of about 60 phrases. Keep the model ID in config, since the public list had only 14 models.

## 7. Pipeline and fallback

1. Build the prompt with NOW, CAL and SRC.
2. Call the LLM with the JSON schema.
3. Validate with Pydantic; retry or escalate once on failure.
4. Run the semantic checks (exclusive fields, rrule allowlist).
5. Resolve in Python.
6. Cross-check with dateparser and the other confirmation signals.
7. Send the acknowledgement with buttons.
8. Persist the raw text, the LLM JSON, the model ID, the prompt version and the resolved UTC times.

**Fallback mode (chutes down):** queue the raw message. If a regex sees `напомни|remind` plus an explicit time, or `через N мин/час`, create a temporary reminder via dateparser. When the message is re-processed, merge the results by `message_id`.

**Tests:**
- Pure unit tests for the resolver using fixed `now` values: 2026-10-24 23:30, 2026-10-25 02:30, 2027-03-28 02:30, Friday after 15:00, the 31st of a month, 00:30 (`night_cutoff`).
- A small LLM eval, re-run whenever the model or the prompt changes.

## 8. Risks and things I could not verify

- **Structured outputs on chutes:** the models list `structured_outputs`, and LiteLLM says chutes accepts `response_format`. I did **not** test that `json_schema`, regex `pattern` or omitting optional properties actually work through chutes. That is why Pydantic re-validation is mandatory.
- **Thinking switch:** I couldn't confirm that chutes passes `chat_template_kwargs` through to the model. Whether Gemma-4 thinks by default is unknown.
- **chutes model list changes often.** These prices and IDs are a snapshot from 2026-10-06.
- **dateparser not run on these phrases.** It is not installed here and I didn't install it. Its coverage is inferred from its `ru.py` data file.
- **Duckling RU coverage** is inferred from `Rules.hs` only; I did not run it.
- **EU DST:** clocks go back on 2026-10-25. News reports say the Commission published DST dates for 2027–2031; I did not check the primary source. Keep `tzdata` updated.
- **Voice transcription** can garble numbers ("в пятнадцать").
- **The LLM's `conf` is not calibrated**, so it is combined with the code-based checks rather than trusted alone.
- **"next FR" is ambiguous in Russian** (this Friday or next week's).

## 9. Open questions for the user

1. Какой город для часового пояса: Europe/Warsaw, Berlin, Prague…? CET/CEST у них одинаковый, но нужен IANA-идентификатор.
2. «В следующую пятницу», сказанное во вторник: это 9 окт или 16 окт?
3. Напоминание за 5 минут: только для точного времени («в 15:00»)? А для «в обед», 10:00 и 19:00 напоминать ровно в это время?
4. «В 8» / «в 9» без уточнения: всегда спрашивать или по правилу (1–7 → вечер, 8–11 → утро)?
5. Явно указанное время внутри тихих часов («в 23:30»): напоминать всё равно?
6. Даты из пересланных постов и скриншотов: ставить напоминание сразу или только после кнопки «Подтвердить»?
7. Повторяющееся без времени («каждый понедельник мусор»): 10:00 или 19:00?
8. «Сегодня в обед», отправленное в 14:00: это заметка о прошлом или перенос на завтра?

## Sources (read 2026-10-06)

- https://pypi.org/project/dateparser/ · https://dateparser.readthedocs.io/en/latest/settings.html · https://raw.githubusercontent.com/scrapinghub/dateparser/master/dateparser/data/date_translation_data/ru.py
- https://github.com/facebook/duckling · https://github.com/facebook/duckling/commits/main · https://raw.githubusercontent.com/facebook/duckling/main/Duckling/Time/RU/Rules.hs · https://hub.docker.com/r/rasa/duckling · https://pypi.org/project/pyduckling-native-phihos/ · https://github.com/treble-ai/pyduckling
- https://github.com/natasha/natasha · https://raw.githubusercontent.com/natasha/natasha/master/natasha/grammars/date.py · https://cloudsmith.com/navigator/pypi/natasha · https://www.piwheels.org/project/rutimeparser · https://github.com/microsoft/Recognizers-Text
- https://pypi.org/project/python-dateutil/ · https://dateutil.readthedocs.io/en/stable/rrule.html · https://docs.python.org/3/library/zoneinfo.html · https://www.rfc-editor.org/rfc/rfc5545 (§3.3.5, §3.3.10)
- https://lists.iana.org/hyperkitty/list/tz-announce@iana.org/thread/IZ7AO6WRE3W3TWBL5IR6PMQUL433BQIE/ (tz 2024b) · https://www.sortiraparis.com/en/news/in-paris/articles/232247-time-change-in-2026-when-is-the-switch-to-winter-time-expected · https://www.leinetal24.de/welt/zeitumstellung-2026-warum-wir-diesen-herbst-schon-wieder-an-der-uhr-drehen-zr-94482665.html
- https://llm.chutes.ai/v1/models · https://chutes.ai/docs/guides/agents-and-tools · https://docs.litellm.ai/docs/providers/chutes · https://docs.sglang.io/advanced_features/structured_outputs.html · https://huggingface.co/Qwen/Qwen3-32B
- https://arxiv.org/abs/2603.19017 (MultiTempBench, 2026-03-19)