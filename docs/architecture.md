# Dump Bot — архитектура и контракты модулей

Документ — источник правды для реализации. Требования: [requirements.md](requirements.md).
Исследования (версии, API chutes, Telegram, разбор времени): [research/](research/).
Итерация 2 (размытые сроки, локальный Whisper, связи и подзадачи, Mini App): [features/iteration-2.md](features/iteration-2.md)
— там изменения и дополнения к контрактам ниже; сделанное оттуда (A, B, C, D) кратко отмечено в этом документе.
**2026-10-07: время записи — это время уведомления** ([requirements.md](requirements.md), последний раздел): без
«за 5 минут» по умолчанию, явный «за N» сдвигает само время записи, «⏰ / 📅» переносят запись. Контракты ниже уже
описаны в этой модели.
Контракт API Mini App — [features/miniapp-api.md](features/miniapp-api.md).

## 1. Общая схема

```
Telegram ──long polling──► aiogram Dispatcher
                              │  OwnerOnly middleware (whitelist OWNER_ID)
                              │  routers: commands → settings/edit FSM → callbacks → capture (catch-all)
                              ▼
          capture: save RawMessage (idempotent) → react 👀 → enqueue job → wake worker      (быстро, без ИИ)
                              │
           JobWorker (asyncio task) ◄── jobs table (FOR UPDATE SKIP LOCKED, backoff, RetryLater)
              handle_message / handle_album / handle_edit / handle_clarify   (services/processor.py)
                 голос/аудио → STT (RoutedSTT: chutes Whisper и/или локальный Whisper по Prefs.stt_mode) ;
                 фото → vision в том же вызове LLM ; ссылки → заголовки
                 → ai.extract() (structured output) → services.items.create_items() / query / apply_patch
                 → bot.views.* + bot.sender.send() → карточки, реакции 👌/✍/🤔
                              │
           Scheduler (asyncio task, тик ≤30 c или wake):
                 напоминания (claim → send → sent, восстановление зависших), повторы (следующее occurrence),
                 дайджесты (once_keys), простой (последний тик в app_state → одно уведомление), heartbeat-файл
                              │
                         PostgreSQL 18
```

Один процесс Python: aiogram polling + 2 фоновые задачи (`JobWorker.run`, `Scheduler.run`), стартуют в
`dp.startup`, отменяются в `dp.shutdown`. Падение фоновой задачи → лог + `os._exit(1)` → Docker перезапустит. Зависание
(await без ответа, заблокированный event loop) ловит watchdog-поток (`watchdog.py`): цикл, который дольше своего лимита
не отметился в `ctx.watchdog`, → лог + `os._exit(1)`. Сон ПК (замер весь процесс, и поток тоже) зависанием не
считается: проверка, пришедшая намного позже срока, обнуляет отсчёт всех циклов (итерация 3, §5). Все запросы к БД
ограничены `command_timeout` (60 с).

Контейнеры: `db` (postgres:18), `migrate` (alembic upgrade head, one-shot), `bot`; необязательный `whisper`
(compose profile `whisper`, `whisper/app.py`: faster-whisper на CPU, `POST /transcribe`, `GET /health`, модель в томе
`whisper-models`, наружу не публикуется — [iteration-2.md, B](features/iteration-2.md); декодирует покадрово до
`WHISPER_MAX_AUDIO_S` и отвечает 413, не дочитывая длинный файл; запрос отключившегося клиента отменяется
(`handler_cancellation`), его распознавание останавливается после текущего фрагмента, а загрузка модели идёт дальше);
необязательный `tailscale`
(compose profile `miniapp`, userspace-режим, `tailscale/serve.json`: Funnel HTTPS 443 → `http://bot:8080`).

Mini App ([iteration-2.md, D](features/iteration-2.md), [miniapp-api.md](features/miniapp-api.md)): при
`WEBAPP_ENABLED` `Lifecycle` поднимает в том же процессе aiohttp-сервер (`dumpbot/web/`: `/api/*` — тонкий слой над
сервисами, каждый запрос подписан initData владельца; остальное — статика фронтенда с fallback на `index.html`) и
ставит кнопку меню «📱 Свалка» (`WEBAPP_URL`); при остановке сервер закрывается первым. «Закинуть в свалку» —
`RawMessage.source="app"` с синтетическим отрицательным `message_id` через обычный конвейер, но без карточек и реакций
в чате (итог — в `raw_json["_app"]`); «🕒 Время» текстом и «🪄 Разбить» из приложения — задачи `edit_time` / `split`
с `app: true`, их итог — в `jobs.result` (обработчик возвращает dict). ☐ строки списка — `done`/`undone` с
`occurrence_id` (`items.mark_done` / `mark_undone` этого повторения, как `OccCb`); «📅 Перенести невыполненное на
завтра» — `digest.move_not_done`, общий для `DigestCb` и `POST /api/today/move`; миниатюры строк —
`?size=thumb` (меньший размер фото / миниатюра файла из `raw_json`).

Фронтенд Mini App (`webapp/`, React + TypeScript + Vite, без UI-библиотек): `src/api/` — типы контракта и клиент
(`Authorization: tma <initData>` на каждом запросе, ошибки → `ApiError`), `src/telegram.ts` — ready/expand, тема из
`themeParams`, BackButton/MainButton, haptics; `src/lib/` — кэш чтений (stale-while-revalidate, `markStale` после
правок), навигация стеком экранов (нижние вкладки), «закинуть в свалку» с опросом статуса, превью картинок через
`fetch` → blob URL (кэш ограничен числом и байтами), черновики (`lib/drafts`: текст «Добавить», правки карточки и
настроек в localStorage до сохранения — переживают смену вкладки и устаревшую сессию); `src/screens/` — экраны. `npm run dev` отвечает на `/api/*` мок-API в памяти (`webapp/mock/`),
им же пользуются тесты vitest. Dockerfile собирает `dist/` в стадии `node:lts-slim` и кладёт в `/app/webapp`
(`WEBAPP_STATIC_DIR`); в собранном `index.html` — CSP (свои файлы, скрипт Telegram, `blob:` для картинок).

## 2. Ключевые решения

| Тема | Решение |
|---|---|
| ИИ | chutes.ai, OpenAI-совместимый `POST {base}/chat/completions` через **aiohttp** (без openai SDK). Основная модель `google/gemma-4-31B-turbo-TEE` (текст+картинки, structured outputs, $0.12/$0.37 за 1M). Запасные: `Qwen/Qwen3-32B-TEE` (текст), `Qwen/Qwen3.6-27B-TEE` (картинки). ID — в `.env`. |
| Structured output | `response_format={"type":"json_schema","json_schema":{"name":..,"schema":..,"strict":true}}`; ответ **всегда** валидируется Pydantic (слишком длинные строки/списки обрезаются, а не отвергаются); при ошибке — 1 повтор с текстом ошибки, затем запасная модель; ответ, обрезанный по max_tokens, — 1 повтор с большим лимитом. Если API отверг json_schema (400) — повтор с `{"type":"json_object"}`. Thinking выключаем: `chat_template_kwargs={"enable_thinking": false}`; `<think>…</think>` вырезаем из ответа на всякий случай. |
| Голос | Community-chute AudioDojo: `POST https://vonkaiser-audiodojo.chutes.ai/stt/whisper`, JSON `{"audio_b64", "language"?, "return_timestamps": false}` → `{"text"}`. URL в конфиге. Опционально ffmpeg → WAV (`STT_CONVERT_WAV`). Недоступен → retry-очередь. Итерация 2: локальный Whisper (`LOCAL_STT_URL`) — запасной или основной по `Prefs.stt_mode` (`auto`/`local`/`chutes`), бесплатный. |
| Картинки | Отправляются в vision-модель **в том же вызове**, что и разбор: модель возвращает `image.text` (OCR) + `image.description` + items. |
| Время | LLM описывает время (`When`), Python вычисляет (`domain.timeresolve`). `now` = `message.date` (а не время обработки). Хранение в UTC, повторы — RRULE в локальном времени `rec_tz`. Время записи (`Occurrence.due_at`) — это время уведомления: «в 15» → 15:00; «за 30 минут до встречи в 15» → 14:30. |
| Очередь | Таблица `jobs`, без Redis. Ошибки ИИ → экспоненциальный backoff `min(2**attempts мин, 6 ч)`. |
| Планировщик | Свой цикл с опросом БД, без APScheduler. At-least-once доставка, восстановление `sending` → `pending` через 2 мин. |
| Поиск | Postgres FTS (`to_tsvector('russian', …)` — смешанные RU/EN), `websearch_to_tsquery('russian', q)` + фильтры. Без векторов. |
| Формат сообщений | HTML parse mode; весь пользовательский/LLM-текст экранировать `html.escape(…, quote=False)`. Даты рендерим сами по-русски (`domain.fmt`). |
| FSM | `MemoryStorage` (короткие диалоги правки/настроек; потеря при рестарте не критична). Ответом диалога считается только набранное владельцем (не пересланное, не «?…») сообщение без reply в течение `ANSWER_TTL` (5 мин) или reply на сам запрос в течение `PROMPT_TTL` (15 мин); всё остальное закрывает диалог и уходит в capture. |
| Логи | stdlib `logging`; **никогда** не логировать текст сообщений/транскрипты/OCR на уровне INFO, не логировать токены/URL файлов Telegram. |

## 3. Структура проекта и владельцы

```
src/dumpbot/
  __main__.py              main(): logging, Settings, engine, AI clients, Bot, Dispatcher, polling   [bot]
  logging_setup.py                                                                                    [bot]
  config.py  clock.py  context.py  dto.py                                                              [foundation ✔]
  watchdog.py              отметки живости фоновых циклов, проверка из потока                          [bot]
  keepalive.py             KEEPALIVE_URL: самопинг, чтобы бесплатный хостинг (Render) не усыплял бота  [bot]
  domain/
    kinds.py  prefs.py                                                                                [foundation ✔]
    timeresolve.py         When → UTC, план напоминаний, диапазоны запросов                          [domain]
    recurrence.py          RRULE: нормализация, следующее повторение, описание по-русски             [domain]
    quiet.py               тихие часы                                                                [domain]
    fmt.py                 русское форматирование дат/длительностей                                  [domain]
  ai/
    schemas.py                                                                                        [foundation ✔]
    client.py              ChutesClient (chat completions, цены, учёт расходов)                      [ai]
    prompts.py             системные промпты                                                         [ai]
    extract.py             extract(), answer_query()                                                 [ai]
    stt.py                 SpeechToText / ChutesWhisperSTT / LocalWhisperSTT / RoutedSTT             [ai]
    linkinfo.py            заголовки страниц по ссылкам                                              [ai]
  db/ base.py models.py engine.py                                                                     [foundation ✔]
  services/
    settings_repo.py                                                                                  [foundation ✔]
    usage.py               запись ai_usage, траты за месяц, состояние бюджета                        [ai]
    items.py               жизненный цикл записей/повторений/напоминаний                             [items]
    query.py               чтение: повестка, списки, поиск, QuerySpec, расходы                        [items]
    related.py             «предметные» лексемы для «🔗 похоже на» (без инфинитивов и общих слов)     [items]
    undo.py                правка описанием: что изменила (разница) и «↩️ Отменить» (итерация 3)      [items]
    app_state.py           значения, переживающие рестарт (app_state: последний тик планировщика)    [workers]
    ingest.py              Message → RawMessage, постановка в очередь                                [workers]
    processor.py           обработчики задач очереди                                                  [workers]
    digest.py              данные для дайджестов                                                      [workers]
  workers/
    jobs.py                enqueue/claim/JobWorker/RetryLater                                         [workers]
    scheduler.py           Scheduler                                                                  [workers]
  bot/
    app.py                 build_bot(), build_dispatcher(), on_startup/on_shutdown                    [bot]
    middlewares.py  cbdata.py  keyboards.py  states.py  views.py  sender.py                           [bot]
    cards.py               карточка + CardExtras (чек-лист, похожие, родитель), 🎉 после закрытия шагов   [bot]
    lists.py               списки с [✅ N]: /today, /tasks, дайджесты; перерисовка по кнопкам сообщения     [bot]
    routers/ commands.py capture.py callbacks.py settings.py edit.py                                 [bot]
  web/                     Mini App: server.py (aiohttp, статика, WebServer) auth.py (initData) http.py  [web]
                           api_lists.py api_items.py api_inbox.py files.py (превью, LRU) serialize.py
migrations/  alembic (0001 initial, 0002 item periods, 0003 subtasks + item_links, 0004 mini app,      [infra]
             0005 notification time: данные — «за 5 минут» → само время, явные «за …» → время записи,
             0006 downtime: таблица app_state, reminders.held_from)
whisper/     app.py Dockerfile requirements.txt — локальный Whisper (compose profile whisper)        [infra]
tailscale/   serve.json — Funnel для Mini App (compose profile miniapp)                              [infra]
webapp/      фронтенд Mini App: src/ (api, lib, screens, components) mock/ (мок-API для dev и тестов)   [webapp]
Dockerfile  compose.yaml  .dockerignore  .env.example  README.md                                     [infra]
tests/conftest.py (фикстуры БД)                                                                       [foundation ✔]
tests/<area>/test_*.py                                                                                [владелец модуля]
```

Правило: агент меняет **только свои файлы**. Нужна правка чужого контракта — не меняй, опиши в отчёте.
Публичные сигнатуры ниже — обязательный контракт; приватные хелперы — на усмотрение.

## 4. Контракты

**Общие dataclass-ы контрактов уже определены в `src/dumpbot/dto.py`** (foundation ✔): `ReminderPlan`, `Resolution`,
`CreatedItem`, `PatchResult`, `Row`, `Agenda`, `UsageSummary`, `BudgetState`, `DigestData` (итерация 2, C: `Step`,
`RelatedRef`, `CardExtras`). Модули импортируют их оттуда
(и могут реэкспортировать: `from dumpbot.dto import Resolution` в `timeresolve.py` и т.п.), а не объявляют заново.
Ниже их поля приведены для справки.

Общие типы: `Prefs` (`domain/prefs.py`), `Kind`, `SourceKind`, `Precision`, `ACTIONABLE` (`domain/kinds.py`),
`Extraction`, `ExtractedItem`, `When`, `QuerySpec`, `EditPatch`, `Ask`, `ImageInfo` (`ai/schemas.py`),
ORM-модели (`db/models.py`), `AppContext` (`context.py`), `utcnow()` (`clock.py`).

Все функции сервисов принимают `AsyncSession` и **не коммитят** (коммитит вызывающий), если не сказано иное.
Все `datetime` в API — aware UTC, кроме явно помеченных `*_local` (naive локальное время).

### 4.1 domain/timeresolve.py  [domain]

```python
@dataclass(frozen=True)
class ReminderPlan:
    fire_at: datetime          # aware UTC
    lead_minutes: int          # за сколько до времени повторения (ранние напоминания срока); 0 = в него или позже
    auto: bool                 # True → время выбрано автоматически → сдвигается из тихих часов

@dataclass(frozen=True)
class Resolution:
    due_at: datetime | None    # aware UTC; время записи = время уведомления; None → нет времени и напоминаний
    precision: Precision       # exact|slot|date|default|relative|fallback|period|soon|someday|none
    reminders: tuple[ReminderPlan, ...]
    rrule: str | None          # нормализованный RRULE (без DTSTART) или None
    rec_time_local: time | None
    rec_dtstart_local: datetime | None   # naive, локальное время prefs.tz
    needs_confirm: bool
    warnings: tuple[str, ...]  # по-русски, для карточки: "время уже прошло — перенёс на завтра"
    period_kind: str | None    # week|month|season|soon|someday (итерация 2)
    period_start: date | None  # локальные даты срока, включительно (None у someday)
    period_end: date | None
    leads: tuple[int, ...] | None   # явные «за N» в силе, по убыванию (то, что хранит Item.lead_minutes)
    stated_at: datetime | None      # названное время, если «за N» сдвинул уведомление раньше («(15:00)» в названии)

def localize(d: date, t: time, tz: ZoneInfo) -> datetime
    """DST-safe: aware datetime в tz. Несуществующее время (весенний переход) → сдвиг вперёд;
    неоднозначное (осенний) → первое (fold=0)."""

def resolve_item(item: ExtractedItem, now: datetime, prefs: Prefs, *, source: SourceKind) -> Resolution
def resolve_when(when: When, now: datetime, prefs: Prefs) -> tuple[datetime | None, Precision, list[str], bool]
    """(due_at UTC | None, precision, warnings, needs_confirm) — без напоминаний; используется и для правок."""
def plan_reminders(due_at: datetime, precision: Precision, role: str, lead: list[int] | None,
                   prefs: Prefs, now: datetime, *, period: tuple[date, date] | None = None,
                   catch_up: bool = False) -> tuple[ReminderPlan, ...]
def notify_due(stated: datetime, precision: str, role: str, lead: Sequence[int] | None, prefs: Prefs,
               now: datetime) -> tuple[datetime, tuple[int, ...] | None]
    """(due, leads) записи, названной на stated: без «за N» — stated; «за 30 минут» — на 30 минут раньше; «за день и
    за час» — последний момент (за час: запись стоит в день встречи), остальные leads — более ранние уведомления.
    Прошедшие моменты отбрасываются; если не осталось ни одного — stated. Дедлайн сохраняет своё время (leads двигают
    только его напоминания)."""
def effective_leads(precision: str, lead) -> list[int]   # exact/slot/relative — любые; date/fallback — кратные 1440;
                                                          # default/period/soon — никаких; 0 — не lead
def lead_shift(precision: str, role: str, lead) -> int     # насколько время записи раньше названного: наименьший
                                                          # lead (0 у дедлайна); названное = due + lead_shift
def shift_back(t, minutes, tz) / shift_forward(t, minutes, tz)   # кратное 1440 — дни по местным часам, иначе UTC
def next_series_due(rule, dtstart_local, tz, after, *, inclusive=False, shift=0, now=None) -> datetime | None
    """Следующее повторение серии (названные времена правила) минус shift; с shift пропускает повторения, чьё
    названное время уже не после now (у повторения с прошедшим «за N», но не прошедшим временем due в прошлом —
    `plan_reminders(catch_up=True)` напомнит в само время)."""
def period_bounds(period: str, now: datetime, prefs: Prefs) -> tuple[date, date]
    """When.period → локальные [start, end]: неделя пн–вс, месяц (YYYY-MM: прошедший → следующий год), ближайший
    незакончившийся сезон (зима дек–фев), soon = сегодня…+soon_days. someday → ValueError."""
def when_period(when: When, now: datetime, prefs: Prefs) -> tuple[str | None, date | None, date | None]
    """(period_kind, start, end), которые запись хранит; дедлайн «до конца недели» — обычная дата → (None, None, None)."""
def resolve_range(spec_range: str, now: datetime, prefs: Prefs) -> tuple[datetime, datetime]
    """QuerySpec.range → [start, end) в UTC по локальному календарю prefs.tz.
    today|tomorrow|yesterday|this_week|next_week|last_week (неделя с понедельника)|this_month|last_month
    |past:N (последние N дней включая сегодня)|next:N (сегодня + N дней)|YYYY-MM-DD|YYYY-MM-DD..YYYY-MM-DD (включительно).
    tomorrow учитывает night_cutoff, как day `+1` (до 04:00 «завтра» = только что начавшийся день)."""
def tomorrow(now: datetime, prefs: Prefs) -> date
    """Дата, которую значит «завтра» в момент now: до night_cutoff — текущая календарная, иначе следующая.
    Общая для `+1`, resolve_range("tomorrow"), items.snooze(tomorrow) и items.move_to_tomorrow."""
```

Правила (все времена — из `prefs`; `now` переводится в `prefs.zone`):

| Вход | Результат |
|---|---|
| Приоритет полей When | `at` > `in_min` > `period` > (`day` и/или `time`). Лишние игнорируются. |
| `period` (итерация 2) | неделя / месяц / сезон: precision `period`, due = напоминание (2) — последний рабочий день срока в `task_default_time`, если прошёл — последний день срока, если и он прошёл — этот момент без напоминания; `soon`: через `soon_days` в `task_default_time`, precision `soon`; `someday`: без due и без напоминания по умолчанию, precision `someday`. С role `deadline` неделя/месяц/сезон → обычная дата: последний рабочий день срока (прошёл → последний день); «к лету», «до октября» (`text` с «до/к/by» без «конца», срок ещё не начался) → первый день срока — промпт v4 сразу просит `day` MM-DD. Подробно: [iteration-2.md §A](features/iteration-2.md). |
| `in_min` | `due = now + in_min` (в UTC), precision `relative`, напоминание ровно в `due` (lead 0, auto=False). |
| только `time` (слот или HH:MM) | сегодня в это время; если ≤ now → **завтра** (без предупреждения). precision `slot`/`exact`. |
| `day` без `time` | время `bare_date_time` (10:00); для `weekend` — `weekend_time`; precision `date`. Явная дата (`+N`, `dN`, `MM-DD`, `YYYY-MM-DD`), которая сегодня, **остаётся сегодня** и после 10:00 (это не время пользователя): `task_default_time` (19:00), если ещё впереди, иначе через 1 ч (не позже 23:59); без предупреждения. |
| `day` + `time` | дата по `day`, время по `time`. |
| `+N` / `+Nd` | сегодня + N дней. **night_cutoff**: если локальное now < `night_cutoff` (04:00) и N ≥ 1 → N−1. |
| `+Nw`, `+Nmo`, `+Ny` | + недели/месяцы (`relativedelta`, 31 → последний день месяца)/годы. |
| `MO`..`SU` | ближайший такой день; сегодня — если время (заданное или 10:00) ещё не прошло, иначе через неделю. |
| `next XX` | этот день недели **следующей** календарной недели (неделя с понедельника). |
| `dN` / `d-1` | N-е число этого месяца, если не прошло, иначе следующего (clamp к концу месяца); `d-1` — последний день. «Прошло» — только если пользователь назвал время; без времени сегодняшнее число = сегодня (см. «`day` без `time`»). |
| `MM-DD` | в этом году, если не прошло, иначе в следующем (то же правило про сегодня без времени). |
| `YYYY-MM-DD` в прошлом | due_at = эта дата (время по правилам), **без напоминаний**, needs_confirm, warning «дата в прошлом». Сегодняшняя дата без времени — не в прошлом. |
| `+0`/сегодня + **явное** время уже прошло | перенос на завтра, warning «время уже прошло — перенёс на завтра», needs_confirm. Без времени — см. «`day` без `time`». |
| `weekend` | ближайшая суббота (сегодня, если суббота и время не прошло). |
| `at` | локальное время, needs_confirm. С временем (`…THH:MM`) — это время пользователя: precision `exact` (без сдвига из тихих часов). Только дата → `bare_date_time` (сегодня — как «`day` без `time`»), precision `fallback`. |
| Нет `when`, kind ∈ ACTIONABLE, нет rrule | `due` = ближайшее `task_default_time` (сегодня, если ещё не прошло, иначе завтра), precision `default`, одно напоминание в `due`, **auto=True**. |
| Нет `when`, kind не actionable | `due_at=None`, precision `none`, напоминаний нет. |
| Час 0–6 при precision `exact` без «утра/ночи/am/a.m.» в `when.text` | needs_confirm (не меняем время). |
| `conf == "low"` или source ∈ {photo, forward} и есть время | needs_confirm. |

Время записи — время уведомления (2026-10-07). `resolve_item` берёт названное время из `resolve_when` и переводит его
в `due` через `notify_due` с явными «за N» (`item.lead`, только из сообщения или правки): «напомни за 30 минут до
встречи в 15» → due 14:30, `Resolution.leads=(30,)`, `stated_at` 15:00 (`items.create_items` дописывает «(15:00)» в
название, если модель его не оставила). Роль `remind` больше не отличается от `event`.

Напоминания (`plan_reminders`; `due_at` — время записи, `lead` — её `Item.lead_minutes`):
- одно уведомление ровно в `due`: auto=False для времени пользователя (exact/slot/relative), **auto=True** для
  выбранного ботом (date/fallback/default/period/soon). **Никаких «за 5 минут» по умолчанию.**
- несколько явных leads («за день и за час» = [1440, 60]): due = названное − наименьший (последнее уведомление,
  запись стоит в день встречи); остальные — раньше, как «накануне» у срока: (due + наименьший) − lead. Какие leads
  действуют — `effective_leads` (date/fallback — только целые дни).
- role `deadline` с exact/slot/date/fallback: время записи — сам срок; уведомление в него, а с явными leads — за
  них до срока (у даты-срока утро самого дня остаётся); плюс накануне в `task_default_time`.
- precision `period` → (2) в due и (1) утром первого дня срока (`bare_date_time`), если срок начинается после
  сегодняшнего дня; `soon` → ровно в due.
- lead кратный 1440 считается в днях по локальным часам (wall clock), остальные — в UTC.
- Прошедшие моменты («за …», (1), «накануне», сам due) пропускаются: при `due ≤ now` не остаётся ничего.
  `catch_up=True` (новое повторение серии: создание, `_create_next`, новый lead, `_set_rrule`, возврат): все
  моменты «за …» прошли, а названное время нет → одно уведомление в названное время, как `notify_due` у разовой.
- auto=True → `quiet.shift_out_of_quiet(fire_at, prefs)`. Дубликаты `fire_at` схлопнуть; сортировка по времени.

Повторы (`item.rrule` есть): `normalize_rrule`; время суток = `time` из When (слот/HH:MM) или `task_default_time`;
`rec_dtstart_local` = дата из `day` («с понедельника», «начиная со следующей пятницы» — от неё отсчитываются INTERVAL и
голый FREQ=WEEKLY/YEARLY), а если `day` нет, он в прошлом или непонятен — сегодня (локально), в это время (naive);
`due_at` = `recurrence.next_occurrence(…, after=now, inclusive=True)` минус явный lead (`next_series_due`: правило
хранит названное время, «каждый пн в 10, напомни за 15 минут» → повторения на 09:45; сегодняшнее повторение, у
которого прошёл только момент «за N», остаётся и напоминает в само время — и считается в COUNT, как без «за N»);
precision `slot`/`exact`/`default`; напоминания для этого повторения — как обычно. Невалидный RRULE → запись без повтора +
warning «не понял правило повтора», needs_confirm.

### 4.2 domain/recurrence.py  [domain]

```python
ALLOWED_KEYS = {"FREQ","INTERVAL","BYDAY","BYMONTHDAY","BYMONTH","BYSETPOS","COUNT","UNTIL","WKST"}
def normalize_rrule(rule: str) -> str            # убирает "RRULE:", upper, проверяет ключи и парсинг; ValueError
def next_occurrence(rule: str, dtstart_local: datetime, tz: ZoneInfo, after: datetime,
                    *, inclusive: bool = False) -> datetime | None
    """Следующее повторение (aware UTC) строго после `after` (или ≥ при inclusive). Разворачивает правило
    в naive локальном времени (сохраняет 09:00 через смену DST), затем localize(). UNTIL приводится к naive local.
    None — правило исчерпано (COUNT/UNTIL)."""
def describe_rrule_ru(rule: str) -> str          # "каждый день", "каждый понедельник", "по пн и чт",
                                                 # "каждые 2 недели", "5-го числа каждого месяца", "каждый год 1 янв"
```

### 4.3 domain/quiet.py  [domain]

```python
def in_quiet_hours(t: datetime, prefs: Prefs) -> bool       # окно может пересекать полночь; start==end или
                                                            # quiet_enabled=False → тихих часов нет
def shift_out_of_quiet(t: datetime, prefs: Prefs) -> datetime   # если t в тихих часах → ближайший quiet_end (UTC)
```

### 4.4 domain/fmt.py  [domain]

```python
def fmt_dt(dt: datetime, tz: ZoneInfo, *, now: datetime | None = None) -> str
    # "сегодня 13:00", "завтра 09:00", "вчера 18:00", "пт 9 окт 15:00", "9 окт 2027 15:00" (другой год)
def fmt_date(d: date, *, today: date | None = None) -> str   # "сегодня", "завтра", "пт 9 окт"
def fmt_time(t: time) -> str                                 # "13:00"
def fmt_duration(minutes: int) -> str                        # "5 мин", "1 ч", "1 ч 30 мин", "1 дн", "2 дн"
def plural_ru(n: int, one: str, few: str, many: str) -> str  # plural_ru(5, "задача", "задачи", "задач")
```

### 4.5 ai/client.py  [ai]

```python
class AIError(Exception): ...
class AIUnavailable(AIError):  """Временная: сеть, таймаут, 429, 5xx, 503 'No instances available'. → повторить позже."""
class AIConfigError(AIError):  """401 (ключ), 402 (нет баланса), 403, 404 (модель/chute не найдены). → нужен пользователь."""
class AIModelNotFound(AIConfigError):  """404: нет именно этой модели/chute (переименовали, убрали, опечатка) — другие
                                          модели могут работать, поэтому extract/answer_query пробуют запасную."""
class AIBadResponse(AIError):  """Модель ответила, но результат непригоден (после всех повторов)."""
    # у AIUnavailable есть атрибут retry_after: float | None (из заголовка Retry-After)

@dataclass
class ChatResult:
    content: str               # без <think>…</think>
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float            # (prompt − cached)·price_in + cached·price_cache_read + completion·price_out (за 1M)
    latency_ms: int
    finish_reason: str | None = None   # "stop", "length" (обрезано по max_tokens), …

class ChutesClient:
    def __init__(self, settings: Settings, sessionmaker: async_sessionmaker | None = None,
                 http: aiohttp.ClientSession | None = None) -> None
    async def close(self) -> None
    async def chat(self, *, purpose: str, model: str, messages: list[dict], json_schema: dict | None = None,
                   schema_name: str = "out", max_tokens: int | None = None, temperature: float = 0.0) -> ChatResult
        """Один HTTP-вызов. Пишет ai_usage (успех и ошибка) через services.usage.record_usage, если есть sessionmaker.
        messages могут содержать image-part: {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}"""
    async def price(self, model: str) -> tuple[float, float]
        """USD за 1M (input, output) из GET {base}/models (кэш 6 ч), иначе settings.llm_fallback_price_*.
        Список публичный и запрашивается без ключа (с неверным ключом он отвечает 401). Цена кэшированного входа —
        pricing.input_cache_read (нет в списке → как обычный вход); cached_tokens — из
        usage.prompt_tokens_details.cached_tokens (входят в prompt_tokens)."""
    def cooldown_left(self, model: str) -> float
        """Итерация 3 (§1): сколько секунд ещё длится пауза модели (0.0 — паузы нет). Пауза — llm_cooldown_s (600)
        после 408 / 429 / 5xx / таймаута ответа (не после ошибки соединения); в памяти процесса, по модели; любой 200
        её снимает. Вызов, отменённый вызывающим (Task.cancel(HEDGE_LOST) — проигравший параллельный запрос, или
        остановка бота), пишется в ai_usage с ok=false, error="cancelled: hedge lost" / "cancelled"; токены и цена —
        только если ответ уже пришёл, иначе 0."""
    def retry_after_left(self, model: str) -> float
        """Сколько ещё действует Retry-After последней 408 / 429 / 5xx модели (не дольше её паузы); 0.0 — нет."""
    def note_lost_hedge(self, model: str) -> None
        """Вызов модели отменён: параллельный запрос другой ответил первым. LOST_HEDGES_TO_PAUSE=2 таких гонок
        подряд (без ответа модели между ними) → пауза, как после таймаута: зависшая модель иначе никогда не доходит
        до своего llm_timeout_s."""
    def is_missing(self, model: str) -> bool
        """Модель ответила 404 и с тех пор не отвечала 200."""
```

### 4.6 ai/stt.py  [ai]

```python
class SpeechToText(Protocol):
    async def transcribe(self, audio: bytes, *, mime_type: str | None = None, language: str | None = None) -> str: ...

class ChutesWhisperSTT:
    def __init__(self, settings: Settings, sessionmaker: async_sessionmaker | None = None,
                 http: aiohttp.ClientSession | None = None) -> None
    async def transcribe(...) -> str      # AIUnavailable / AIConfigError; ai_usage(purpose="stt", audio_seconds, cost)
    async def close(self) -> None
```

Стоимость — `stt_usd_per_second · max(1, ceil(latency_s))`, в том числе при таймауте ответа (chute продолжает
работать и тарифицироваться, когда мы перестали ждать); ошибки соединения не тарифицируются.

Итерация 2 ([iteration-2.md, B](features/iteration-2.md)):

```python
class LocalWhisperSTT:                     # тот же протокол; POST {local_stt_url}/transcribe, таймаут local_stt_timeout_s
    ...                                    # ai_usage(purpose="stt", model="local:<model>", audio_seconds=длительность, cost=0)
                                           # сеть/таймаут/5xx → AIUnavailable; прочие статусы (413 длиннее лимита сервиса,
                                           # 400, 404 неверный LOCAL_STT_URL) → AIBadResponse, никогда AIConfigError;
                                           # LOCAL_STT_URL — базовый адрес, «…/transcribe» на конце тоже понимается
class RoutedSTT:                           # ctx.stt
    def __init__(self, chutes: SpeechToText, local: SpeechToText | None = None) -> None
    def order(self, mode: str) -> list[str]   # ORDER_BY_MODE: auto → chutes, local; local → local, chutes; chutes → chutes
    async def transcribe(self, audio, *, mime_type=None, language=None, mode="auto",
                         before_paid: Callable[[], Awaitable[object]] | None = None) -> str
```
Без `local_stt_url` все режимы — `chutes`. Любая `AIError` бэкенда → следующий; если упали все — `AIUnavailable`,
иначе `AIConfigError`, иначе последняя. `before_paid` (проверка бюджета процессором) вызывается перед chutes и может
прервать распознавание (`RetryLater`).

### 4.7 ai/extract.py  [ai]

```python
PROMPT_VERSION: str  # "v8" (итерация 2: when.period; related / steps / edit.action split; «к лету» → day;
                     # v5: часы 13–23 и 0 не переспрашивать, related — только тот же конкретный предмет;
                     # v6: время — момент уведомления, lead = «за N до названного», названное время — в title;
                     # v7 (итерация 3, §3): «купил X», «сделал X», «готово: X» про открытую задачу из RELATED
                     # CANDIDATES → edit {target: X, action: done}, иначе — прошедший факт;
                     # v8: только если сообщение лишь об этом; просит ещё что-то новое → save, ничего не теряется)

@dataclass
class ExtractInput:
    text: str                                  # текст/подпись/транскрипт (может быть пустым для картинки)
    source: SourceKind
    now: datetime                              # aware; время сообщения
    prefs: Prefs
    images: list[tuple[bytes, str]] = field(default_factory=list)   # (data, mime)
    forward_from: str | None = None            # "канал «Хабр»"
    link_titles: dict[str, str] = field(default_factory=dict)
    force_query: bool = False                  # сообщение начиналось с "?"
    reply_item: dict | None = None             # снимок записи, на карточку которой ответили → intent=edit
    clarification: str | None = None           # выбранный вариант на предыдущий Ask
    related: list[dict] = field(default_factory=list)  # RELATED CANDIDATES: {id, kind, title, date} (итерация 2, C1)

@dataclass
class ExtractOutput:
    extraction: Extraction
    model: str
    prompt_version: str
    missing_model: str | None = None   # основная модель ответила 404, результат дала запасная (model)

async def extract(client: ChutesClient, settings: Settings, inp: ExtractInput) -> ExtractOutput
    """Основная модель (vision-модель = settings.llm_model, если есть картинки) → валидация → при ValidationError
    один повтор с текстом ошибки → запасная модель (vision_fallback_model при картинках, иначе llm_fallback_model).
    Ответ, обрезанный по max_tokens (finish_reason="length"), сначала один раз запрашивается заново с
    LONG_MAX_TOKENS=4096 (без «исправь JSON»: он обрезался бы там же).
    AIUnavailable или AIModelNotFound (404) основной → сразу пробуем запасную; если и она недоступна → AIUnavailable.
    404 запасной: основная была недоступна → её AIUnavailable; обе 404 (или запасной нет) → AIModelNotFound
    (AIConfigError); основная ответила негодным (это уже оплачено) → AIBadResponse, а не вечный часовой повтор.
    Итерация 3 (§1): основная на паузе (client.cooldown_left), а запасная не на паузе и не 404 (client.is_missing)
    → сразу запасная, вместо основной и с её повтором «исправь JSON» (ничего годного → AIUnavailable с retry_after =
    остаток паузы); 404 от запасной здесь → сразу основная, несмотря на паузу (её результат или ошибка — как в
    лестнице). Нет годного ответа основной за llm_hedge_after_s (12 с) → параллельно запасная (пауза запасной этому
    не мешает, только её действующий Retry-After: client.retry_after_left): первый валидный результат выигрывает,
    второй вызов отменяется (Task.cancel(HEDGE_LOST)) и дожидается записи в ai_usage; проигравшая основная —
    client.note_lost_hedge (две гонки подряд → пауза); без валидного результата ошибки разрешаются как в
    последовательной лестнице. llm_hedge_after_s=0 / llm_cooldown_s=0 — выключено. ExtractOutput.missing_fallback —
    запасная известна как 404 (процессор раз в день пишет владельцу).
    force_query → intent принудительно query; reply_item → intent принудительно edit (edit.target=None)."""

async def answer_query(client: ChutesClient, settings: Settings, question: str, rows: list[dict],
                       now: datetime, prefs: Prefs) -> str
    """Краткий ответ по-русски простым текстом (без HTML), опираясь только на rows. Основная модель → при
    AIUnavailable / AIBadResponse / AIModelNotFound запасная; если запасной нет (404) → ошибка основной.
    Пауза и параллельный запрос — как у extract (итерация 3, §1)."""

@dataclass
class SplitInput: task: dict; now: datetime; prefs: Prefs; note: str | None = None   # итерация 2, C2
@dataclass
class SplitOutput: steps: list[StepSpec]; model: str; prompt_version: str; missing_model: str | None = None
async def split_steps(client: ChutesClient, settings: Settings, inp: SplitInput) -> SplitOutput
    """Вызов "split" (🪄 Разбить на шаги, «разбей на шаги»): SPLIT_SYSTEM_PROMPT + NOW/CAL/NOTE/TASK → схема
    {steps: [{title, when?}]} (≤ 8), та же лестница основная → запасная, ai_usage.purpose="split". [] — нечего разбивать."""
```

Итерация 2, C ([iteration-2.md §C](features/iteration-2.md)): `ExtractedItem.related: list[int]` (≤ 3, id из строки
`RELATED CANDIDATES`; чужие id и id, у которых «предметные» лексемы названий не пересекаются с новой записью,
отбрасывает `items.create_items`),
`ExtractedItem.steps: list[StepSpec]` (≤ 8, `{title, when?}`), `EditPatch.action` += `split`. Ответ на карточку с
шагами вместо правки превращается в `split`.

После валидации `extract` убирает бессмысленный вопрос «утра или вечера?» (`_drop_pointless_ask`): все варианты —
время, два из них отличаются на 12 часов, а фраза времени записи (`when.text`) называет этот час только однозначно
(13–23 или 0: «в 17», «17:30», «в семнадцать»; или с «утра/вечера») и ни разу «голым» («в 8»). Вопрос остаётся:
«в 8», «в 9 или в 21», «до 20 в 8», «к 20 окт в 8», «в восемь двадцать» (двадцать — минуты), «двадцать пятого в
восемь» (дата), пустая фраза (`when.time` — лишь догадка модели). Время записи с этим часом остаётся, `conf` low →
med; время другой половины суток (или его нет) заменяется закреплённым часом, `conf` не меняется; другой час или
`at`/`in_min`/`period` — вопрос остаётся.

Схема `Extraction` (ai/schemas.py): до `MAX_ITEMS=20` записей, `title` ≤ 120, `tags` ≤ 6, `image.text` ≤ 4000 и т.д.
Ограничения остаются в JSON-схеме (подсказка для constrained decoding), а при валидации слишком длинные строки
обрезаются с «…», списки — до первых N элементов: лишний тег или длинный заголовок ссылки не роняют всё сообщение.
Промпт называет лимиты явно (в режимах json_object/plain схема модели не видна) и просит длинный список покупок
(больше 8) сохранять одной записью-покупкой со списком в details.

Промпт — по мотивам [research/time-parsing.md §5](research/time-parsing.md): статичная часть (инструкции на английском,
примеры) + динамическая `NOW … / CAL … / SRC … / MSG …`. Обязательно перечислить kinds и их смысл, правила `When`,
`intent`, `QuerySpec`, `EditPatch`, `image`. Ответ на языке сообщения.

### 4.8 ai/linkinfo.py  [ai]

```python
async def fetch_titles(urls: list[str], *, http: aiohttp.ClientSession | None = None,
                       timeout_s: float = 5.0, max_bytes: int = 262144) -> dict[str, str]
    """Только http/https, не более 5 URL, читает максимум max_bytes, <title>/og:title, html.unescape, ≤200 символов.
    Ошибки молча пропускаются (ссылки без заголовка просто не попадают в результат).
    Только публичные адреса (`ipaddress.is_global`): loopback/LAN/link-local/CGNAT/ULA не запрашиваются ни напрямую,
    ни через редирект (редиректы, ≤5, проходятся вручную с проверкой каждого шага), имена проверяются по адресу,
    к которому реально идёт соединение (`PublicResolver`, без DNS rebinding). Разбор HTML линейный по размеру."""
```

### 4.9 services/usage.py  [ai]

```python
@dataclass
class BudgetState:
    spent_usd: float
    limit_usd: float
    ratio: float        # spent/limit (inf если limit == 0 и spent > 0; 0 если оба 0)
    exhausted: bool     # spent >= limit и limit > 0 ... (limit == 0 → ИИ выключен → exhausted=True)

async def record_usage(sessionmaker, *, purpose: str, model: str, input_tokens: int = 0, output_tokens: int = 0,
                       audio_seconds: float = 0.0, cost_usd: float = 0.0, ok: bool = True, latency_ms: int = 0,
                       error: str | None = None) -> None          # своя сессия + commit, ошибки только логируются
async def month_spend(session, prefs: Prefs, now: datetime) -> float   # текущий календарный месяц в prefs.tz
async def budget_state(session, prefs: Prefs, now: datetime) -> BudgetState
def next_month_start(now: datetime, prefs: Prefs) -> datetime         # aware UTC начало следующего месяца по prefs.tz
```

### 4.10 services/items.py  [items]

```python
@dataclass
class CreatedItem:
    item: Item
    occurrence: Occurrence | None
    reminders: list[Reminder]
    resolution: Resolution

@dataclass
class PatchResult:
    item: Item
    occurrence: Occurrence | None      # текущее открытое
    reminders: list[Reminder]          # pending текущего occurrence
    warnings: list[str]
    deleted: bool = False

async def create_items(session, *, raw: RawMessage | None, extraction: Extraction, source: SourceKind,
                       source_text: str | None, source_meta: dict | None, urls: list[str], prefs: Prefs,
                       now: datetime, model: str | None, prompt_version: str | None) -> list[CreatedItem]
    """Для каждого ExtractedItem: resolve_item → Item (+ Occurrence при due_at, + Reminder по плану).
    kind=link и пустые urls у item → item.urls = urls сообщения. flush, без commit."""
async def replace_items_for_raw(session, raw_id: int, ...same kwargs...) -> list[CreatedItem]
    """Для уточнения (Ask): мягко удалить записи этого raw (status=deleted, напоминания cancelled) и создать заново."""
async def get_item(session, item_id: int) -> Item | None
async def item_snapshot(session, item_id: int, prefs: Prefs, now: datetime) -> dict
    """Для промпта правки: {id, kind, title, details, tags, due_local:"YYYY-MM-DD HH:MM"|None (время уведомления),
    rrule, lead, status}."""
async def current_occurrence(session, item_id: int) -> Occurrence | None     # ближайшее open (иначе последнее)
async def pending_reminders(session, occurrence_id: int) -> list[Reminder]
async def mark_done(session, occurrence_id: int, now: datetime, prefs: Prefs) -> Occurrence
    """occurrence.status=done, done_at; pending-напоминания → cancelled. Разовая запись → item.status=done.
    Повторяющаяся → гарантировать следующее open-повторение (с напоминаниями)."""
async def mark_undone(session, occurrence_id: int) -> Occurrence   # обратно open; item.status=open
async def snooze(session, occurrence_id: int, *, minutes: int | None = None, tomorrow: bool = False,
                 at: datetime | None = None, prefs: Prefs, now: datetime) -> Reminder
    """[⏰ 15 мин] / [⏰ 1 час] / [📅 Завтра] / [🌙 Вечером] (итерация 3: `at` = `timeresolve.tonight`, сегодня в
    `slot_evening`, только впереди) ПЕРЕНОСЯТ время записи (2026-10-07): now+minutes, либо «завтра»
    (`timeresolve.tomorrow`: до night_cutoff — уже начавшийся день; если этот момент прошёл — день спустя) в то же
    локальное время суток, что и due_at. Occurrence получает этот due (`/today`, дайджесты, Mini App показывают новое
    время; `_move`: в ежедневной серии сливается с уже созданным завтрашним), все его pending → cancelled, остаётся
    ровно одно напоминание (auto=False) в это время; у разовой записи явные leads сбрасываются, срок
    (period/soon) и время, выбранное ботом (default), становятся этим точным временем (precision exact).
    Раньше своего времени запись не переезжает никогда: отложенная на момент до него (напоминание накануне срока,
    утро первого дня срока, «за день» из «за день и за час», «Отложить» в Mini App у записи, чьё время не
    наступило) сохраняет время, напоминания до этого момента → cancelled, добавляется одно, поздние остаются.
    Возвращённое напоминание указывает на occurrence, к которому оно теперь относится."""
async def delete_item(session, item_id: int, now: datetime) -> None     # soft: status=deleted, deleted_at, rec_active=False, cancel
async def restore_item(session, item_id: int, now: datetime, prefs: Prefs) -> Item   # undo; напоминания open
                                                                        # occurrences, чьё названное время впереди
async def remove_reminders(session, item_id: int, *, prefs: Prefs | None = None, now: datetime | None = None) -> int
    """🔕: cancel все pending, lead_minutes=[]; occurrence, сдвинутые «за N», возвращаются на названное время (у
    серии — все повторения, чьё названное время впереди: следующее не задублирует их). Вернуть число отменённых."""
async def set_kind(session, item_id: int, kind: str) -> Item
async def set_title(session, item_id: int, title: str) -> Item
async def set_tags(session, item_id: int, tags: list[str]) -> Item
async def apply_patch(session, item_id: int, patch: EditPatch, prefs: Prefs, now: datetime) -> PatchResult
    """action: done/undone (текущее occurrence), delete, no_reminder, stop_recurrence, update:
    title/kind/tags_add/tags_remove; when → resolve_when → перенос текущего open occurrence (или создание нового)
    на это время уведомления (только patch.lead этой же правки сдвигает его раньше; прежние leads разовой записи
    сбрасываются, серия хранит свои для следующих повторений); rrule → включить/заменить повтор (правило хранит
    названное время: due предыдущего + его lead); lead без when → уведомление на lead раньше названного времени
    (`_shift_by_lead`, «напомни за час»); новый lead серии переставляет и её остальные повторения, чьё названное
    время впереди (`_relead`). Название: when без lead убирает метку прежнего названного времени («Встреча (15:00)»
    → 17:00 — «Встреча»), when с lead и lead без when дописывают названное время («(15:00)», на другой день —
    «(пт 9 окт 15:00)»), как при создании. kind стал ACTIONABLE без времени → напоминание по умолчанию (19:00);
    kind перестал быть ACTIONABLE (задача → идея) без when/rrule и вне активного повтора → open occurrences с
    precision `default` → cancelled вместе с pending-напоминаниями (время, заданное владельцем, остаётся)."""
async def stop_recurrence(session, item_id: int, now: datetime | None = None) -> Item
    """rec_active=False; текущее (самое раннее) open остаётся, уже созданные следующие (due_at > now) → cancelled
    вместе с напоминаниями (следующее появляется сразу, как только текущее наступило)."""
async def ensure_recurring(session, prefs: Prefs, now: datetime) -> int
    """Для каждой записи rec_active и status!=deleted: если нет open-повторения с due_at > now → создать следующее
    (next_occurrence после названного времени последнего, max(due_at) + lead_shift, и после now) с напоминаниями
    (`catch_up`); open-повторения этой записи, у которых
    due_at < due_at нового и которые старше 12 ч, → status=missed, кроме тех, у кого есть pending-напоминание
    с fire_at > now (отложенное кнопкой): они ждут его отправки. Возвращает число созданных."""
async def move_to_tomorrow(session, occurrence_ids: list[int], prefs: Prefs, now: datetime, *,
                           day: date | None = None, keep_ahead: bool = False) -> list[Occurrence]
    """+1 день к локальной дате при том же времени суток; due в прошлом → на `day` (по умолчанию
    `timeresolve.tomorrow`, не раньше сегодня) в то же время, а если и этот момент прошёл — днём позже. Перепланировать.
    Новое время — единственное уведомление разовой записи (явные leads сбрасываются, кроме дедлайна). keep_ahead
    (вечерний дайджест): не трогать occurrence, чьё названное время ещё впереди («за день» до завтрашней встречи)."""
```

Итерация 2, C ([iteration-2.md §C](features/iteration-2.md)): `Item.parent_id/position` — шаг чек-листа (один
уровень), таблица `item_links(item_id, related_id, kind='similar')`.

```python
async def create_items(..., related_allowed: Collection[int] = ()) -> list[CreatedItem]
    # + шаги ExtractedItem.steps (не у повтора; в результат не входят) и item_links только на id из related_allowed
def can_split(item: Item) -> bool               # open, не шаг, не активный повтор
async def add_steps(session, parent_id: int, steps: Sequence[StepSpec], prefs: Prefs, now: datetime, *,
                    model: str | None = None, prompt_version: str | None = None) -> list[CreatedItem]
    # строка родителя FOR UPDATE; уже есть шаги / нельзя разбить → []
async def toggle_step(session, step_id: int, *, done: bool, prefs: Prefs, now: datetime) -> Item
def take_parent_events(session) -> ParentEvents  # closed: родители, закрытые последним шагом; changed: чей чек-лист менялся
```
Шаг без своего времени не получает occurrence (нет дефолтных 19:00), со временем — обычные occurrence и напоминания.
Любое изменение шага (☐/☑, «Готово» в напоминании шага, правка, удаление, возврат, новое время, «когда-нибудь»)
пересчитывает родителя (`_sync_parent`): строка родителя `FOR UPDATE`, родитель и шаг перечитываются
(`populate_existing`), так что двойное нажатие не закроет его дважды. Нет открытых шагов и есть отмеченный → родитель
done (его open occurrences → done, pending-напоминания → cancelled, `ParentEvents.closed` → 🎉); открытый шаг у
закрытого родителя открывает его. Родитель, закрытый вручную, закрывает свои открытые шаги с его же `done_at`;
`mark_undone` / «не сделано» родителя открывает шаги с этим `done_at` (то, что закрыл его «Готово», или последний
отмеченный шаг, закрывший его). Удаление родителя удаляет шаги с тем же `deleted_at`, `restore_item` возвращает именно
их; возврат шага удалённого родителя сначала возвращает родителя. Родитель-повтор шагами не закрывается, а повтор
(`rrule`) не ставится ни задаче с шагами, ни шагу — предупреждение `WARN_CHECKLIST_RRULE` / `WARN_STEP_RRULE`.
`clear_time` шага отменяет и его done-occurrence. `lock_item(session, item_id)` — строка записи `FOR UPDATE` (кнопка
и endpoint «🪄 Разбить на шаги»: два быстрых нажатия ставят одну задачу; при исчерпанном бюджете —
`processor.split_budget_block`, задача не ставится). Подробно: [iteration-2.md §C2](features/iteration-2.md).

Итерация 2, D (Mini App): `add_step(session, parent_id, title, prefs, now, *, when=None)` — шаг в конец чек-листа
(до `MAX_CHECKLIST` = 20; закрытый родитель открывается), `set_details`, `clear_someday` («когда-нибудь» → запись без
даты, задача получает 19:00), `clear_time` (шаг без своего времени).

Итерация 3, §3 ([features/iteration-3.md](features/iteration-3.md)) — `services/undo.py`, отмена правки описанием:

```python
@dataclass
class Applied: result: PatchResult; snapshot: dict | None; title_before: str; steps_closed: int; moved: bool; changed: bool
async def apply(session, item_id: int, patch: EditPatch, prefs: Prefs, now: datetime) -> Applied
    # apply_patch между двумя чтениями «семьи» записи (родитель + все шаги): snapshot — только разница (строки
    # items / occurrences / reminders до и после, созданные правкой occurrences и reminders, наибольшие id после);
    # None — правку не отменить (она удалила строки). Чтение — выборки колонок из БД, не ORM-объекты сессии.
def keep(raw: RawMessage, item_id: int, snapshot) -> None     # raw_json[ingest.UNDO_KEY][item_id] = snapshot
def stored(raw: RawMessage | None, item_id: int) -> dict | None
async def undo_edit(session, raw_id: int, item_id: int, now: datetime) -> str   # UNDONE | ALREADY | STALE | MISSING
```
`undo_edit` блокирует строки семьи (items → occurrences → reminders, как сервисы), потом raw (FOR UPDATE): второе
нажатие ждёт первого и получает `ALREADY`. Устарело (`STALE`, ничего не меняется) — любая изменённая или созданная
правкой строка уже не такая, как правка её оставила (у самой записи — и `updated_at`: любая следующая правка, кнопка),
любая другая запись семьи не такая, как после правки (снимок `rest`: галочка другого шага, закрывшая родителя, — иначе
отмена оставила бы сделанного родителя с открытым шагом; у снимков, сохранённых до этого, `rest` нет — проверка
пропускается), состав семьи другой (добавлен шаг), или в семье появились occurrence / живое напоминание с id больше
запомненного (следующее повторение, отложенное).
Иначе созданные строки удаляются, изменённые получают прежние значения (occurrences, которые сдвигаются, сначала
«паркуются» на +1000 лет: UNIQUE(item_id, due_at) проверяется построчно), `updated_at` = now, у родителя —
`items.checklist_changed` (его карточки перерисуются). Почему raw_json, а не таблица: отмена принадлежит сообщению с
правкой, там уже живут `_ask` / `_candidates` / `_app`, разница — несколько строк, миграция живому боту не нужна.

Внутренние правила: при любом перепланировании pending-напоминания текущего occurrence → cancelled, создаются новые
по `plan_reminders` (для precision `period` — с `period=(item.period_start, item.period_end)`). Итерация 2: `Item`
хранит `period_kind/period_start/period_end/someday`; `apply_patch` с `when.period` пересчитывает срок и
напоминания («перенеси на следующую неделю»), `someday` отменяет open occurrences с напоминаниями; явная дата,
`move_to_tomorrow` и rrule делают запись обычной датированной (period сбрасывается).
«Когда-нибудь» — без живого occurrence: `restore_item` не возобновляет остановленный им повтор, `mark_undone` его
occurrence → cancelled (не open), «не сделано» правкой снимает только отметку самой записи (и occurrence, закрывший
её), `move_to_tomorrow` такие записи пропускает, `query.agenda` не показывает их в open/overdue. rrule берёт за
образец первое open, иначе последнее не отменённое occurrence («сделал заранее» — только у done); у occurrence
срока (`period`/`soon`) и у `when` со сроком серия начинается с первого дня срока (не раньше сегодня) в
`task_default_time`. Чипы срока Mini App (`PATCH period`) задают срок с ролью `event`, как ответ «перенеси на …».
Перед отменой pending-напоминаний строки occurrences блокируются (`SELECT … FOR UPDATE` до commit):
два одновременных перепланирования одного occurrence (двойное нажатие «+15 мин», кнопка и job правки) выполняются по
очереди, и второе видит и отменяет напоминание первого, а не добавляет дубликат. `Item.updated_at` обновляется. Повторяющиеся записи: `Item.status` остаётся open, пока активен повтор.

### 4.11 services/query.py  [items]

```python
@dataclass
class Row:
    item_id: int
    kind: str
    title: str
    tags: list[str]
    status: str                    # статус записи
    due_at: datetime | None        # due текущего/найденного occurrence
    occurrence_id: int | None
    occ_status: str | None
    rrule: str | None
    created_at: datetime
    snippet: str | None            # фрагмент source_text/details (≤200 символов)
    urls: list[str]
    period_kind: str | None        # итерация 2: Item.period_kind/period_start/period_end/someday
    period_start: date | None
    period_end: date | None
    someday: bool
    parent_id: int | None          # итерация 2, C2: шаг → название родителя; родитель → прогресс шагов
    parent_title: str | None
    steps_done: int
    steps_total: int

@dataclass
class Agenda:
    day: date                      # локальная дата
    open: list[Row]                # open occurrences с due в этот день (сортировка по времени)
    overdue: list[Row]             # open occurrences с due < начала дня (только для сегодня), не старше 30 дней
    done: list[Row]                # done в этот день
    period_items: list[Row]        # итерация 2: open записи, чей срок (неделя/месяц/сезон) содержит день;
                                   # пока срок идёт, их нет в open/overdue

@dataclass
class UsageSummary:
    month: str                     # "октябрь 2026"
    spent_usd: float
    limit_usd: float
    calls: int
    by_purpose: dict[str, tuple[int, float]]   # purpose -> (calls, usd)

async def agenda(session, prefs: Prefs, now: datetime, *, day_offset: int = 0) -> Agenda
async def open_tasks(session, prefs: Prefs, now: datetime, limit: int = 30) -> list[Row]
    # ACTIONABLE, status open, не someday; сначала с due (по возрастанию), потом без
async def someday_items(session, limit: int = 50) -> list[Row]    # open someday-записи (/someday), новые первыми
async def recent(session, limit: int = 15) -> list[Row]           # по created_at desc, без deleted
async def search(session, text: str, limit: int = 20) -> list[Row]  # FTS + ILIKE-фолбэк по title/tags
async def run_query(session, spec: QuerySpec, prefs: Prefs, now: datetime) -> list[Row]
    # date_field=due + range: occurrences с due в диапазоне и записи, чей срок пересекается с диапазоном (тогда —
    # только occurrence самого срока: precision period, due внутри срока; старое done другого времени — нет)
async def recurring(session) -> list[Row]                          # rec_active, с ближайшим open occurrence
async def find_edit_candidates(session, hint: str, prefs: Prefs, now: datetime, limit: int = 5, *,
                               exact: bool = False) -> list[Row]
    # среди open/недавних (30 дней) не deleted: FTS по hint (все слова, или тег = слово; иначе любое слово),
    # приоритет — ближайшие по due. exact=True: только слова самой записи — все слова в title/details (FTS по
    # to_tsvector(title || details), не search_tsv: source_text общий у записей одного сообщения) или hint целыми
    # словами в title («чай» не в «случайно») — без тегов и OR
async def items_of_raw(session, raw_ids: Sequence[int]) -> list[Row]   # не deleted записи этих raw (без шагов), по id
async def related_candidates(session, text: str, now: datetime, *, exclude_raw_ids: Sequence[int] = (),
                             limit: int = 5) -> list[Row]
    # итерация 2, C1: tsquery 'a | b | …' по «предметным» лексемам текста (services/related.py: без инфинитивов,
    # слов напоминаний, времени, частых глаголов, чисел, ссылок; ничего не осталось → []); порядок: больше лексем в
    # title/details, больше лексем во всей записи, ts_rank, новее; только открытые (не done/deleted, задача со
    # сделанным текущим повторением — как done, серии и просроченные неотмеченные остаются), не шаги, created_at за
    # 365 дней, кроме записей exclude_raw_ids
async def related_refs(session, item_id: int) -> list[RelatedRef]   # item_links в обе стороны, не deleted, новые первыми
async def card_extras(session, item: Item) -> CardExtras            # шаги (☐/☑, время), похожие, название родителя
async def usage_summary(session, prefs: Prefs, now: datetime) -> UsageSummary
def rows_for_llm(rows: list[Row], prefs: Prefs) -> list[dict]      # компактные dict для answer_query (локальные даты)
```
Итерация 2, C2: в `/tasks`, `/recent`, `/someday`, `run_query` шаг попадает только со своим временем (есть
не отменённое occurrence); повестка и дайджесты строятся по occurrences, так что шаги без времени туда не попадают.
`search` и `find_edit_candidates` находят и шаги. Строки получают `parent_title` / `steps_done/steps_total`.

Итерация 2, D: `feed(session, prefs, *, text, kinds, tags, status, created_from, created_to, before, limit)` — лента
новых первыми с keyset-курсором `(created_at, id)`; `rows_by_ids`; `item_images(session, item)` / `image_counts` —
картинки сообщения записи (все части альбома; фото и JPEG/PNG/WebP/GIF файлом) для превью.

Итерация 3, §2 ([features/iteration-3.md](features/iteration-3.md)): `list_rows(session, refs, *, current=False)` —
строки пронумерованного списка (`(occurrence_id | None, item_id)` из его кнопок) как они есть сейчас: это occurrence
записи, а если оно отменено (переехало, «когда-нибудь») или его не было — текущее (отменённое → строка без времени,
сама запись); удалённая / пропавшая запись → None. `current=True` (/tasks: строка — это задача) — и оставленное
`missed` повторение серии заменяется текущим. `done_after_day(session, prefs, day)` (§5) — occurrences со сроком в
day, отмеченные после него (напоминание, придержанное до утра).

### 4.12 workers/jobs.py  [workers]

```python
Handler = Callable[[AppContext, dict], Awaitable[None]]

class RetryLater(Exception):
    def __init__(self, delay: timedelta | None = None, reason: str = "", *, at: datetime | None = None,
                 count_attempt: bool = True) -> None

async def enqueue(session, kind: str, payload: dict, *, dedupe_key: str | None = None,
                  run_after: datetime | None = None) -> int | None     # ON CONFLICT (dedupe_key) DO NOTHING → None
async def claim(session, now: datetime, limit: int = 5, lock_for: timedelta = timedelta(minutes=5)) -> list[Job]
    # UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING; status queued|running с истёкшим lock

class JobWorker:
    def __init__(self, ctx: AppContext, handlers: dict[str, Handler], *, poll_interval_s: float = 5.0) -> None
    async def run(self) -> None          # бесконечный цикл; ждёт ctx.jobs_wakeup или poll_interval
    async def run_once(self) -> int      # одна итерация (для тестов): claim + выполнить; вернуть число задач
```
`run` регистрирует `"jobs"` в `ctx.watchdog` (лимит `STALL_AFTER` = 30 мин; processor отмечается и сам — перед
распознаванием каждой части альбома аудиофайлов) и отмечается на каждой итерации и перед
каждой задачей. Итерация 2, D: обработчик может вернуть dict — он сохраняется в `Job.result` (итог для Mini App,
`GET /api/jobs/{id}`); `pending_job(session, kind, item_id)` — id идущей задачи о записи.

Семантика выполнения задачи: обработчик открывает свои сессии через `ctx.sessionmaker`. Успех → `done`.
`RetryLater` → `queued`, `run_after = at or now+delay` (count_attempt=False — не увеличивать attempts, например бюджет).
`AIUnavailable` → `run_after = now + min(2**attempts мин, 6 ч)` (или retry_after). Иное исключение → attempts++, backoff;
при `attempts >= max_attempts` → `dead`, `last_error`, и вызвать `on_dead(ctx, job)` (уведомление владельцу, реакция 😢).

### 4.13 services/ingest.py  [workers]

```python
def message_to_raw_fields(message: aiogram.types.Message) -> dict
    """content_type (text|voice|audio|photo|image_document|document|video|video_note|animation|sticker|location|contact|other),
    text (text or caption), urls (из entities/caption_entities: url и text_link; без дублей),
    файл (самое большое фото; document с mime image/* → image_document), forward_origin (нормализованный dict:
    {type, title?, username?, chat_id?, message_id?, url?, sender_name?, date}), media_group_id, reply_to_message_id,
    tg_date, raw_json (message.model_dump(mode="json", exclude_none=True))."""
async def save_incoming(session, message: Message, *, force_query: bool = False,
                        reply_item_id: int | None = None) -> tuple[RawMessage, bool]
    """INSERT … ON CONFLICT (chat_id, message_id) DO NOTHING; (raw, created)."""
async def enqueue_processing(session, raw: RawMessage, now: datetime) -> None
    """reply_item_id → job "edit" {raw_id, item_id}, dedupe "edit:{raw_id}";
    media_group_id → job "album" {media_group_id}, dedupe "album:{chat_id}:{gid}", run_after now+2s;
    иначе job "message" {raw_id}, dedupe "msg:{raw_id}"."""
async def message_raw_ids(session, chat_id: int, message_id: int) -> list[int]  # raw сообщения или всех частей альбома
async def picked_item(session, raw_id: int, text: str) -> int | None   # ответ «2» на список кандидатов raw → item_id
ASK_KEY = "_ask"; CANDIDATES_KEY = "_candidates"   # ключи raw_json: заданный вопрос / item_id списка кандидатов
EDIT_KEY = "_edit"; UNDO_KEY = "_undo"   # итерация 3, §3: понятая правка (EditPatch JSON) для выбранного кандидата /
                                         # {item_id: снимок undo.apply} для «↩️ Отменить»
async def save_app_text(session, chat_id: int, text: str, now: datetime) -> RawMessage   # итерация 2, D
    # source="app", message_id = -id, urls из текста (text_urls), "?" в начале -> force_query
APP_KEY = "_app"   # raw_json сообщения из приложения: {note, answer: {text, title, item_ids}, edited: [item_id]}
```

### 4.14 services/processor.py  [workers]

```python
HANDLERS: dict[str, Handler]   # {"message": handle_message, "album": handle_album, "edit": handle_edit,
                               #  "clarify": handle_clarify, "edit_time": handle_edit_time, "split": handle_split}
async def on_dead(ctx: AppContext, job: Job) -> None
```

`handle_message(ctx, {"raw_id"})`:
1. Загрузить raw (status done/ignored → выйти). prefs. `now_msg = raw.tg_date`.
2. Бюджет: `budget_state` исчерпан → реакция ✍, `raw.status=deferred`, уведомление один раз за месяц
   (`once_keys` `budget100:YYYY-MM`) → `RetryLater(at=next_month_start, count_attempt=False)`.
   ≥ 80% → уведомление один раз (`budget80:YYYY-MM`), продолжаем.
   Итерация 2: бюджет проверяется только перед платными вызовами — перед chutes STT (`before_paid`) и перед
   `extract()` (после сбора контента); локальный Whisper бесплатен, голосовое без текста сохраняется без ИИ.
3. Контент: голос/аудио → `bot.download` → `stt.transcribe(language=prefs.stt_language, mode=prefs.stt_mode)` →
   `raw.transcript` (commit сразу, чтобы при повторе не платить снова); длиннее `settings.stt_max_duration_s` (300 с) — без STT (остаётся подпись или
   запись «Аудио: name»); фото/image_document → скачать байты (в БД не хранить) → images; картинка больше
   `MAX_IMAGE_BYTES` (8 МБ, по `raw.file_size` до скачивания или по факту) или не влезающая в `MAX_REQUEST_IMAGE_BYTES`
   (12 МБ на запрос) модели не отправляется (загрузка входит в `llm_timeout_s`);
   urls → `fetch_titles` → `raw.link_titles`. Прочие типы без текста → запись kind=other без ИИ (title по типу:
   «Видео», «Кружочек», «Стикер», «Геолокация», «Документ: name»), реакция 👌, конец.
4. `extract(...)` (source: forward > voice > photo > link(если есть urls) > text). `image.text`/`description` → `raw.image_text`.
5. intent `save`: `create_items` (source_text = текст + транскрипт + image.text). Для каждой записи с напоминаниями
   или ACTIONABLE или needs_confirm/warnings → `views.item_card` → `sender.send(reply_to=raw.message_id, purpose="card")`.
   Если карточек нет → реакция 👌; иначе реакция 👌 тоже ставится. `ask` → `views.ask_message` (+ реакция 🤔).
   Пустой items → реакция 👌 и запись kind=note с исходным текстом (ничего не теряем).
6. intent `query`: `run_query` → `answer=="list"` → `views.rows_list`; `summary` → `answer_query` → текст
   (`AIUnavailable`/`AIBadResponse` от `answer_query` → тот же `views.rows_list`: повтор job заплатил бы за extract снова).
7. intent `edit` (итерация 3, §3): `find_edit_candidates(edit.target, exact=True, limit=20)`, если пусто — без
   `exact`. Ровно одна точная среди открытых (для `undone` — среди сделанных; для `done` — открытая задача / событие /
   покупка, не заметка вместо уже сделанной задачи; или единственная точная вообще) →
   правка применяется сразу, любая, и delete / done тоже (`_apply_edit`: `undo.apply`, снимок → `raw_json[UNDO_KEY]`,
   ответ `views.edit_applied` «✅ Закрыл «…»» / «🕒 Перенёс «…» на …» / «🗑 Удалил «…»» с [↩️ Отменить] (UndoCb),
   `purpose="edited"`, item_id — reply на него правит запись). Иначе (несколько, найдено лишь по части слов или по
   тегу) → `views.edit_candidates` (`purpose="pick"`, item_id списка → `raw_json[CANDIDATES_KEY]`, понятая правка →
   `raw_json[EDIT_KEY]`; кнопки PickCb или ответ «2» → job "edit" с item_id). Сообщение из Mini App (отмены там нет):
   delete/done/no_reminder/stop_recurrence и для одной точной записи ждут выбора. 0 → сообщение сохраняется заметкой
   (как при `save` с пустым items), её карточка с «🤷 Не нашёл, что изменить — сохранил как запись.». Пересланный
   пост (forward_origin) — чужие слова, правкой не бывает: сохраняется как при `save` (пустой items → заметка).
   Правка с `items` (модель нашла в сообщении и новое: «купил молоко, хлеба не было — купить завтра») — после правки
   / списка кандидатов эти записи сохраняются как при `save`.
8. `raw.status=done`, `processed_at`. Разбудить планировщик (`ctx.scheduler_wakeup.set()`).
Итерация 3, §6 — голосовые: любой ответ на голосовое/аудио показывает расшифровку строкой «🎤 «…»» (`views.heard_line`,
≤ 120 символов, экранируется): карточка — из `CardExtras.heard` (`query.heard`: транскрипт raw записи, у альбома — всех
голосовых частей; переживает перерисовку), карточка после голосовой правки reply — транскрипт этой правки
(`cards.patch_card(heard=…)`), ответ на вопрос, уточняющий вопрос, список кандидатов, `edit_applied` — строка сверху
(`views.with_heard`), карточка разбивки на шаги по голосовому — транскрипт этого голосового
(`cards.card_view(heard=…)`), «😢 Не смог …» (`_tell` / `_give_up`) — строка сверху. Голосовое, давшее только записи
без карточек (заметки/идеи; обычно лишь 👌), получает ещё и `views.heard_item` на каждую запись: «🎤 «…» → 💡 Идея
«…»» с [✏️ Изменить] [🗑 Не то — удалить] (ItemCb edit/del), `purpose="heard"`, item_id этой записи (кнопки и reply —
про неё одну; старые сообщения с несколькими записями и [✏️ N] [🗑 N] продолжают работать как раньше).
Итоговая реакция (👌/🤔/✍/😢) ставится на все части альбома (capture поставил 👀 на каждую). Все сообщения processor'а
в тихие часы (`quiet.in_quiet_hours(now)`) уходят без звука (`silent=True`): ночью разбираются отложенные бюджетом
или недоступностью ИИ сообщения. Бюджет исчерпан → `RetryLater(at=shift_out_of_quiet(next_month_start))`.
Ошибки: `AIUnavailable` → реакция ✍ (один раз), пробросить (очередь повторит). `AIConfigError` → уведомление владельцу
(один раз в сутки, once_key), `RetryLater(1 ч)`. `AIBadResponse` (модели ответили, но годного результата нет;
temperature 0 и фиксированный seed — повтор дал бы то же за те же деньги) → без повторов: сообщение сохраняется без ИИ
(текст/транскрипт → заметка, как при пустом items; без текста → запись по типу «Фото»/«Голосовое сообщение»), raw
`done`, ответ «😢 Не смог обработать сообщение — оно сохранено…» + реакция 😢. Для `clarify` записи первого разбора
остаются как есть (тот же ответ + 😢); для `edit`/`edit_time` — сразу «😢 Не смог применить изменение…», raw `failed`.
`ExtractOutput.missing_model` (основная модель 404, ответила запасная) → уведомление «модели X нет, работаю на
запасной» один раз в сутки (once_key `aimodel:YYYY-MM-DD`), обработка продолжается.

`handle_album(ctx, {"media_group_id"})` — все raw с этим media_group_id (по message_id), одна обработка: подпись(и) +
до 4 картинок; результат привязывается к первому raw; остальные raw → done.
`handle_edit(ctx, {"raw_id","item_id"})` — `extract(reply_item=item_snapshot)` → `apply_patch` → обновить карточку
(новое сообщение-карточка reply на правку) + реакция 👌. `edit.action=delete` → карточка «🗑 Удалено» с кнопкой «Вернуть».
Итерация 3, §3: выбранный кандидат правки описанием (item_id есть в `CANDIDATES_KEY`, есть `EDIT_KEY`) получает уже
понятую правку — без второго `extract` и без проверки бюджета (`split` — сразу вызов split), ответ как при правке
сразу (`_apply_edit`, once_key `edit:{raw}:{item}` в той же транзакции); запись удалили — «🤷 Не нашёл эту запись».
Невалидный сохранённый patch → прежний путь через ИИ.
`handle_edit_time(ctx, {"item_id","text"})` — из кнопки «🕒 Время»: текст пользователя → `extract(reply_item=…,
text="перенеси на: …")` → `apply_patch` только с when/rrule/lead из ответа модели (title/kind/action отбрасываются).
Нет годного времени (нет when/rrule/lead или when не разрешается) → ничего не меняется, ответ «🤷 Не понял время —
напиши, например, «завтра в 15:00».» + реакция 🤔. С `message_id` — не более одного раза (once_key
`edit-time-done:{item}:{msg}` коммитится вместе с правкой и проверяется в начале). После правки —
`cards.after_steps` (новое время отмеченного шага открывает закрытого родителя, его карточка перерисовывается).
Итерация 2, C: перед `extract()` — `query.related_candidates` по тексту сообщения (не для `?`-вопросов) →
`ExtractInput.related`; в `create_items` уходят их id (`related_allowed`). Карточку получает и запись с чек-листом или
похожими. `edit.action == "split"` (ответ на карточку или правка по target) и job `"split"` `{item_id, message_id,
now}` от кнопки «🪄 Разбить на шаги» (`now` нажатия — только если job запустился в течение часа, иначе время запуска)
→ `split_steps` (бюджет проверяется перед вызовом) → `items.add_steps` →
карточка с чек-листом (кнопка — правка этой карточки; ответ — новая карточка reply). Нечего разбивать / уже есть шаги /
нельзя → заметка над карточкой. `AIUnavailable` для кнопки — один раз «✍ ИИ сейчас недоступен…», `AIBadResponse` —
«😢 Не смог разбить на шаги». После правок, закрывших родителя последним шагом, — `cards.after_steps` (🎉).
Итерация 2, D: сообщение из Mini App (`raw.source == "app"`) идёт тем же путём, но в чат ничего не отправляется
(ни карточек, ни реакций, ни вопроса, ни ответа, ни списка кандидатов): заметка, ответ на «?» и изменённые записи —
в `raw_json[APP_KEY]`, вопрос и кандидаты — под обычными ключами. `edit_time` / `split` с `app: true` возвращают итог
`{ok, reason, message, …}` (→ `Job.result`). `ask_question(ctx, question, prefs, now) -> QueryAnswer` — `POST /api/ask`
(бюджет → `extract(force_query)` → `run_question`: строки QuerySpec и ответ словами).
`handle_clarify(ctx, {"raw_id","option"})` — повтор `extract(clarification=option)` → `replace_items_for_raw` → карточки.
Не более одного раза на raw: once_key `clarify-done:{raw_id}` коммитится вместе с новыми записями и проверяется в начале
(повторный запуск job после рестарта не платит за ИИ и не заменяет записи снова).

### 4.15 services/digest.py  [workers]

```python
@dataclass
class DigestData:
    kind: Literal["morning", "evening"]
    agenda: Agenda
    recurring_today: int
    yesterday: Agenda | None = None    # итерация 3, §5: утренний — вчерашний день, если его вечерний итог проспан
async def build_digest(session, kind: str, prefs: Prefs, now: datetime) -> DigestData | None   # None, если пусто
async def digest_data(session, kind: str, prefs: Prefs, now: datetime) -> DigestData   # итерация 3: и пустой
                                                                                       # (перерисовка после [✅ N])
def folded_key(day: date) -> str          # once_key "digest:evening-folded:YYYY-MM-DD": итог day — в утреннем day+1
async def missed_evening(session, day: date, prefs: Prefs) -> Agenda | None   # day_summary(day-1), если итог свёрнут
async def day_summary(session, day: date, prefs: Prefs) -> Agenda   # повестка day; done — и отмеченные позже
                                                                    # (срок в day: придержанное до утра напоминание)
def has_summary(agenda: Agenda) -> bool   # есть open или done: только тогда итог сворачивается в утренний
```

### 4.16 workers/scheduler.py  [workers]

```python
class Scheduler:
    def __init__(self, ctx: AppContext) -> None
    async def run(self) -> None            # цикл: tick(); спать до min(interval, ближайший fire_at) или wake
    async def tick(self, now: datetime) -> None
```

`tick`: (0) итерация 3, §5: время тика → `app_state` `scheduler:last_tick` (`app_state.swap`); долгий тик (flood
control) по ходу дела (перед каждым напоминанием и в конце, не чаще `PROGRESS_EVERY` = 1 мин) пишет туда, докуда
дошёл: тик_now + прошедшее по `time.monotonic` (пауза ≥ `GAP_AFTER` между отметками — процесс, возможно, был заморожен:
до конца тика больше не пишется); последнее записанное время `GAP_AFTER` (15 мин) назад и раньше (сон ПК, Docker стоял;
в том числе первый тик после рестарта) → `Downtime(start, end)` (ещё один разрыв до уведомления продлевает его),
в той же транзакции → `app_state` `scheduler:downtime` {start, end}; первый тик нового процесса подхватывает его
(процесс умер до свёртки итога / уведомления), после уведомления (или «ничего не задело») ключ удаляется;
(1) `sending` с `claimed_at < now-2мин` → `pending`;
(2) claim pending `fire_at ≤ now` (SKIP LOCKED, ≤20):
item deleted / occurrence не open → `cancelled`; `now - fire_at > prefs.missed_grace_minutes` → `missed`;
now в тихих часах и (`auto` или опоздало больше `HOLD_AFTER` = 30 мин) → `pending` с
`fire_at = shift_out_of_quiet(now)` и `held_from` = исходное время (grace на конце тихих часов не проверяется: ничего
не теряется); напоминание с `held_from` уходит беззвучно, «опоздал на …» — от `held_from`; иначе
`views.reminder_message` → `sender.send(purpose="reminder")` → `sent`, `tg_message_id`; временная ошибка (сеть,
5xx Telegram, flood control дольше 60 с, обрыв соединения с БД) → `pending` без attempts++, остаток пачки возвращается
в `pending` до следующего обычного тика (без немедленного повтора): сбой переживается до `missed_grace_minutes`, потом
`missed`; прочая ошибка отправки → attempts++, `pending` (после 5 попыток → `failed`); (3) `items.ensure_recurring`;
(4) дайджесты: для morning/evening, если включён:
целевое время = `shift_out_of_quiet(local digest time)`, окно [цель, цель+3ч), в тихие часы не отправляется (опоздавший
в них утренний пропадает, когда окно закроется; вечерний — см. ниже), ключ `digest:{kind}:{YYYY-MM-DD}` в once_keys
(вставка ON CONFLICT DO NOTHING → только вставивший отправляет); `build_digest` → None → не отправлять;
итерация 3: перед этим — вечерний итог, проспанный ботом (между прошлым тиком / началом простоя и целью тиков не было),
который уже нельзя отправить (тихие часы или окно закрылось): его ключ занимается (поздно не придёт), и, если у дня
есть что подытожить (`digest.has_summary(day_summary)`: что-то было на этот день — сделано или нет; одни просроченные
не в счёт, они и так в утреннем), `digest.folded_key(day)` — утренний дайджест day+1 показывает «🌙 Вчерашний итог (не
успел прислать)»; ключ занят раньше (свёрнут до рестарта) и `folded_key` есть → в уведомлении всё равно «Итог дня —
в утреннем дайджесте»;
(5) после простоя, когда его напоминания обработаны (нет бэклога и ждущих Telegram): одно беззвучное
`views.downtime_notice` (`purpose="notice"`), только если что-то задело — опоздавшие (отправлены поздно / ждут
конца тихих часов), устаревшие (`missed`), свёрнутый итог; не ушло — повтор на следующем тике (в пределах grace);
(6) heartbeat: записать время в `settings.heartbeat_file`.
`run` регистрирует `"scheduler"` в `ctx.watchdog` (лимит `max(15 мин, 3 × scheduler_interval_s)`) и отмечается перед
каждым тиком, шагом и напоминанием.

### 4.17 bot/sender.py  [bot]

```python
async def send(ctx: AppContext, text: str, markup: InlineKeyboardMarkup | ForceReply | None = None, *,
               reply_to: int | None = None, silent: bool = False, purpose: str,
               item_id: int | None = None, occurrence_id: int | None = None,
               raw_message_id: int | None = None) -> int
    """send_message в ctx.chat_id (HTML), reply_parameters(allow_sending_without_reply=True), повтор при
    TelegramRetryAfter (до 3 раз), запись BotMessage в своей сессии. Возвращает message_id."""
async def edit(ctx: AppContext, message_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> None
    # "message is not modified" игнорировать
async def react(ctx: AppContext, message_id: int, emoji: str) -> None   # best-effort, ошибки глушить
async def lookup(session, chat_id: int, message_id: int) -> BotMessage | None
```
Эмодзи реакций (только из разрешённого списка Telegram): 👀 получено, 👌 сохранено, ✍ отложено (ИИ недоступен/бюджет),
🤔 нужен ответ, 😢 не смог обработать.
`bot_messages.purpose` сообщений, ждущих ответа (константы `PURPOSE_*`): `ask` (уточняющий вопрос, raw_message_id),
`pick` (список «Какую запись изменить?», raw_message_id), `search_prompt` / `ask_prompt` (/search и /ask без текста,
ForceReply). `edited` (итерация 3): ответ на правку описанием с [↩️ Отменить] (item_id — reply правит запись; не
`card`, чтобы перерисовка чек-листа не превратила его в карточку). `heard` (итерация 3, §6): «🎤 «…» → 💡 Идея «…»»
голосового без карточек (item_id, если запись одна).

### 4.18 bot/views.py  [bot]

Все функции чистые (без I/O), возвращают `(text_html, markup | None)`.

```python
# 2026-10-07: строка времени — моменты уведомлений по дням: «🔔 сегодня 13:00», «🔔 завтра 09:30 и 10:15»,
# «🔔 вс 11 окт 10:00, пн 12 окт 09:00»,
# «⏳ до пт 9 окт · 🔔 чт 19:00, пт 10:00», «🗓 на этой неделе (до вс 11 окт) · 🔔 пт 19:00»; без напоминаний —
# «🔕 завтра 13:00» (у времени по умолчанию «🔕 без напоминания»), прошедшее — «⏰ вчера 18:00 · просрочено»,
# сделанное — «⏰ сегодня 13:00».
# Итерация 2, C: item_card(..., *, extras: CardExtras | None = None) — «↳ шаг из «Переезд»», чек-лист «📋 Шаги: 1/4»,
# «☑ 1. Упаковать вещи», «☐ 2. Заказать машину · чт 8 окт 15:00» с кнопками [☐ N]/[☑ N] (SubCb), «🔗 похоже на: «…»
# (12 сен)» (2 названия + «и ещё N») и [🔗 Похожее]; [🪄 Разбить на шаги] — открытая задача без шагов, не повтор, не шаг
# (только если extras переданы). reminder_message(..., parent_title=None); steps_closed_text(title) — 🎉.
# Строки списков: «📌 Заказать машину (из «Переезд»)», «📌 Переезд (2/5)».
def item_card(item: Item, occ: Occurrence | None, reminders: Sequence[Reminder], prefs: Prefs, now: datetime,
              warnings: Sequence[str] = ()) -> tuple[str, InlineKeyboardMarkup | None]
    # "📌 <b>Купить наушники</b>\n🔔 сегодня 13:00\n🔁 каждый пн\n🏷 Покупка · #электроника\n⚠️ …"
    # кнопки: [✏️ Изменить] [🔕 Без напоминания] [🗑 Удалить] (+ [⏹ Стоп повтор] если rec_active); [✅ Готово] — не в карточке, а в edit_menu (ACTIONABLE) и в напоминании
def deleted_card(item: Item) -> tuple[str, InlineKeyboardMarkup]          # "🗑 Удалено: …" [↩️ Вернуть]
def edit_menu(item: Item) -> tuple[str, InlineKeyboardMarkup]             # [🕒 Время] [🏷 Категория] [#️⃣ Теги]
                                                                          # [✏️ Название] [← Назад] («⏱ Напомнить за…»
                                                                          # убрано 2026-10-07)
def kind_menu(item: Item) -> tuple[str, InlineKeyboardMarkup]             # 9 категорий
def reminder_message(item: Item, occ: Occurrence, reminder: Reminder, prefs: Prefs, now: datetime)
    # "🔔 <b>Купить наушники</b>" (+ «🗓 на этой неделе (до вс 11 окт)» / «⏳ срок: завтра»; без «через 5 мин»;
    # пришедшее позже своего времени больше чем на 2 мин — «⏰ сегодня 13:00 · опоздал на 5 ч»)
    # [✅ Готово] / [⏰ 15 мин] [⏰ 1 час] [📅 Завтра]; итерация 3, §4: пока до `slot_evening` больше часа
    # (`offers_evening`) — [✅ Готово] / [⏰ 1 час] [🌙 Вечером] [📅 Завтра]
def done_text(...)/ отметки: после "Готово" сообщение редактируется: "✅ <s>Купить наушники</s>" [↩️ Не сделано]
def reminder_snoozed(item: Item, reminder: Reminder, prefs: Prefs, now: datetime) -> tuple[str, None]
    # после [⏰ 15 мин] / [⏰ 1 час] / [📅 Завтра]: "🔔 <b>…</b>\n💤 отложено · напомню сегодня в 13:15" без кнопок
    # (отложенное напоминание придёт заново со своими; время записи уже переехало на это время)
def morning_digest(data: DigestData, prefs: Prefs, now: datetime) -> tuple[str, InlineKeyboardMarkup | None]
    # + раздел «🗓 На этой неделе (до вс 11 окт)» / «🗓 В этом месяце» / «🗓 Этим летом» (period_sections)
def evening_digest(data: DigestData, prefs: Prefs, now: datetime) -> tuple[str, InlineKeyboardMarkup | None]
    # вечерний: сделано / не сделано (уже наступило) / «Ещё сегодня» (due > now, не считается в «Сделано X из Y»)
    # / просрочено + [📅 Перенести невыполненное на завтра] (DigestCb с днём дайджеста), если есть что переносить
def rows_list(title: str, rows: Sequence[Row], prefs: Prefs, now: datetime, *, empty: str = "Пусто")
# Итерация 3, §2: today_list(agenda, …) (/today), tasks_list(rows, …) (/tasks), morning_digest, evening_digest —
# открытые строки пронумерованы «1. 13:00 · 📌 Купить хлеб», под списком [✅ 1] [✅ 2] … (ListCb, по 5 в ряд, ≤ 20;
# остальные — «•» и подсказка, только если кнопки кончились: строки, добавленные после отправки, — без неё); закрытая
# строка — «1. ✅ 13:00 · 📌 <s>Купить хлеб</s>» с [↩️ 1]. Kwarg `lines` (pin_lines: номера и разделы из кнопок
# сообщения + новые строки без номера) — перерисовка после нажатия; строка «🗓 На этой неделе», у записи которой срока
# больше нет (отложена / перенесена на точное время), остаётся со своим номером и временем после разделов сроков.
def edit_applied(item, occ, reminders, patch: EditPatch, prefs, now, *, title_before: str, moved: bool,
                 changed: bool = True, undo: tuple[int, int] | None = None, steps_closed: int = 0,
                 warnings=()) -> View
    # итерация 3, §3: «✅ Закрыл «…» (и 2 шага / · следующий раз …)», «🗑 Удалил «…»», «↩️ Снова открыл «…»»,
    # «🕒 Перенёс «…» на завтра 10:00» (+ строка времени карточки, если она говорит больше), «🌙 Отложил «…» на
    # когда-нибудь», «🔔 Напомню о «…» …», «🔁 «…» — каждый пн», «✏️ Переименовал», «🏷 … — теперь идея», «#️⃣ Теги»,
    # «🔕 Выключил напоминания», «⏹ Остановил повтор», предупреждения времени; [↩️ Отменить] (UndoCb raw, item)
# Итерация 3, §5: downtime_notice(start, end, prefs, *, late, held, held_until, missed, folded, morning) -> str —
# «😴 Меня не было с 19:00 до 23:19 (компьютер спал?). Опоздало напоминаний: 1 — пришлю в 08:00. Итог дня — в утреннем
# дайджесте.» (+ «Не прислал устаревших напоминаний: N — эти дела в /today»); morning_sections(data) — разделы утреннего
# с SEC_YESTERDAY «y» (вчерашнее не сделанное; из «Просрочено» убирается), в morning_digest — «🌙 Вчерашний итог (не
# успел прислать)», «Сделано X из Y», строки с [✅ N]; напоминание с held_from — «⏰ вчера 20:00 · опоздал на 12 ч».
# §6: heard_line(text) «🎤 «…»» (≤ HEARD_MAX = 120), item_card — из extras.heard (после тегов), with_heard(view, heard)
# — строка сверху, heard_item(heard, item) — «🎤 «…» → 💡 Идея «…»» [✏️ Изменить] [🗑 Не то — удалить].
def ask_message(ask: Ask, raw_id: int) -> tuple[str, InlineKeyboardMarkup]
def edit_candidates(rows: Sequence[Row], raw_id: int, prefs: Prefs, now: datetime)
def usage_text(summary: UsageSummary) -> str
def settings_menu(prefs: Prefs) -> tuple[str, InlineKeyboardMarkup]
def help_text() -> str
```
Длинные тексты обрезать до 4096 символов (с «…»). callback_data ≤ 64 байт (только id и короткие коды).

### 4.19 bot/routers  [bot]

Порядок: `commands` → `settings` (FSM-ввод) → `edit` (FSM-ввод) → `callbacks` → `capture` (всё остальное).

Команды (`set_my_commands` со scope владельца): `/today`, `/tasks`, `/someday`, `/recent`, `/search <текст>`,
`/ask <вопрос>`, `/recurring`, `/settings`, `/usage`, `/help`, `/start`. Неизвестная команда → подсказка.
`/start` при `OWNER_ID=0`: отвечает любому «Твой Telegram ID: N — впиши его в .env как OWNER_ID и перезапусти».

`capture`: всё, что не команда. `?…` → force_query. Reply разбирается по тому, на что он: сообщение бота с item_id
(карточка, напоминание) → `reply_item_id`; своё сообщение → записи из его raw (альбом — из всех частей): одна →
`reply_item_id`, несколько → `views.edit_candidates` (`purpose="pick"`, reply raw → done, ids в
`raw_json[CANDIDATES_KEY]`), ни одной → обычное сообщение; текст на `purpose="ask"` → job "clarify"
{raw_id, option: текст}, dedupe `clarify:{raw_id}` (как у кнопок); число на `purpose="pick"` → job "edit"
{raw_id списка, item_id}, dedupe `pick:{raw_id}:{item_id}` (как PickCb); в обоих случаях reply raw → done, на вопросе
показан ответ «✅ …» без кнопок, реакция 👌, а если ответ уже принят (dedupe) — обычное сообщение;
`search_prompt` → `commands.send_search(текст)` (raw не сохраняется); `ask_prompt` → force_query.
`edited_message` — игнорировать. Сохранение → реакция 👀 → enqueue → `ctx.jobs_wakeup.set()`.
`/search`, `/ask` без текста (выбраны в меню команд) → подсказка с `ForceReply` (`purpose` search_prompt/ask_prompt).

Итерация 2, C: `SubCb` (`c:done|undo:<step id>`) — отметить / снять шаг, карточка родителя перерисовывается; ItemCb `split`
(job "split", пока такой job queued/running — «Уже разбиваю на шаги…») и `rel` (карточки до 3 похожих новыми
сообщениями). После любого коммита, закрывшего родителя последним шагом (в том числе «Готово» в напоминании шага),
`cards.after_steps`: перерисовать последнюю карточку родителя и прислать «🎉 Все шаги выполнены — «…» закрыт».
`callbacks` (CallbackData в `cbdata.py`): done/undone/snooze (OccCb), edit/del/undel/norem/stoprec/kind/back (ItemCb),
выбор категории (KindCb), ответ на Ask (AskCb → job "clarify"), выбор записи для правки (PickCb → job "edit"),
перенос невыполненного (DigestCb: день дайджеста D из данных кнопки, у старых кнопок `d:tmr` — дата сообщения;
переносятся open-повторения дня D с due ≤ now и просроченные, на D+1 — `move_to_tomorrow(day=D+1, keep_ahead=True)`;
то, что ещё впереди вечером, и то, чьё названное время впереди, не трогается), настройки (SetCb). Изменения → `edit` текущего сообщения (карточки) + `answer()` с тостом.
Итерация 3: OccCb `eve` [🌙 Вечером] — `snooze(at=tonight)`; нажатое после `slot_evening` — тост «🌙 Вечер уже
наступил…» и напоминание с [⏰ 15 мин]. ListCb `ld:<a>:<k>:<d>:<s>:<n>:<o>:<i>` ([✅ N] / [↩️ N] под /today, /tasks,
дайджестами): `done` — `mark_done` occurrence строки (у серии — этот раз, у родителя — и открытые шаги; без occurrence —
`apply_patch(done)`), `undo` — `mark_undone` / `apply_patch(undone)`, без ИИ; затем список (`bot.lists.render`)
перерисовывается по кнопкам сообщения (`lists.snapshot`): те же номера и разделы, состояние — текущее; нажатая строка
привязывается к occurrence, на которое подействовала (строка /tasks с оставленным `missed` повторением серии —
`list_rows(current=True)` — закрывает текущее повторение и показывает его с [↩️ N]). [↩️ N] шага, закрытого [✅]
родителя (`done_at` шага = `done_at` родителя и тем же нажатием закрыты и другие шаги), отменяет весь тот [✅]:
`apply_patch(undone)` родителя возвращает его и шаги, закрытые с ним (тост «↩️ Снова открыто: «Переезд» и 2 шага»).
Уже закрытое / открытое / удалённое — только тост (+ перерисовка); сообщение без кнопок (недоступное) — свежий
список. [📅 Перенести] вечернего дайджеста перерисовывает его так же по кнопкам (`lists.render(…, drop=перенесённые
occurrences)`): закрытые строки сохраняют номер и [↩️ N], перенесённые пропадают, внизу «📅 Перенесено на завтра: N».
UndoCb `u:<raw>:<item>` [↩️ Отменить] (§3) — `undo.undo_edit`: вернулось → сообщение становится карточкой записи,
тост «↩️ Отменено»; второе нажатие — «Уже отменено»; устарело — «🤷 Запись уже изменилась — отменить нельзя», кнопка
убирается, ничего не меняется; снимка нет — «Кнопка устарела».
Кнопки убранного «⏱ Напомнить за…» в старых сообщениях (ItemCb `lead`, `LeadCb` `l:…`, SetCb `lead`, `ask`
`lead_minutes`) отвечают «Кнопка устарела» и ничего не меняют.

`edit` (FSM): «🕒 Время» → ждём текст → job "edit_time" (голосовое → как голосовой reply на карточку, job "edit");
«✏️ Название» → текст → `set_title`; «#️⃣ Теги» → текст (через пробел/запятую) → `set_tags`; название и теги
подтверждаются коротким reply («✏️ Название изменено: …», с item_id). Ответ — только `common.is_answer` (не пересланное,
не «?…», reply только на сам запрос, в пределах `ANSWER_TTL`/`PROMPT_TTL`); иначе — и для любого не-текста —
диалог закрывается (запрос снова становится карточкой), сообщение идёт в capture. `/cancel` или кнопка — выход.
В `settings` то же; значение, которое не разобралось, получает одно «Попробуй ещё раз», второе — в capture.

`settings`: меню (views.settings_menu): часовой пояс (кнопки Europe/Warsaw, Europe/Berlin, Europe/Minsk, Europe/Kyiv +
ввод IANA), задача без времени, слоты (утро/обед/день/вечер/после работы/ночь, выходные, дата без времени),
тихие часы (вкл/выкл, начало, конец), дайджесты (вкл/выкл, время), бюджет ($),
«🎙 Голос»: язык (авто/ru/en) и режим распознавания (`stt_mode`: авто/локальный/только chutes; «локальный» с пометкой
«(не настроен)», пока пуст `LOCAL_STT_URL`). Ввод времени `HH:MM` валидируется локально. После сохранения — `ctx.scheduler_wakeup.set()`.
Существующие напоминания при смене настроек не перепланируются (сказать об этом в тексте меню).

### 4.20 bot/app.py и __main__.py  [bot]

`main()`: logging → Settings → engine/sessionmaker → aiohttp ClientSession (общая для ChutesClient/STT/linkinfo) →
Bot(DefaultBotProperties(parse_mode=HTML, link_preview_options(is_disabled=True))) → Dispatcher(MemoryStorage) →
middlewares (OwnerOnly на update outer; DI: ctx в data["ctx"]) → routers → startup: set_my_commands, запуск JobWorker и
Scheduler (`add_done_callback` → при исключении лог + `os._exit(1)`) и поток `ctx.watchdog` (проверка раз в 15 с:
цикл молчит дольше лимита → лог + `logging_setup.flush()` + `os._exit(1)`), shutdown: остановка watchdog, ожидание
обрабатываемых апдейтов (до `DRAIN_TIMEOUT_S` = 10 с), отмена задач, закрытие клиентов, `engine.dispose()`
(`stop_grace_period: 30s` в compose.yaml покрывает это до SIGKILL).

Бесплатный хостинг (README, «Бесплатный хостинг: Render + Supabase»): `MIGRATE_ON_START` — `main()` до старта
выполняет `alembic upgrade head` в процессе (`alembic.ini` в рабочей папке; ошибка → лог с traceback и выход с кодом
3; после миграций логирование настраивается заново: `fileConfig` из alembic.ini его заменяет). `KEEPALIVE_URL` —
`KeepAlive` (`dumpbot/keepalive.py`) на общей aiohttp-сессии раз в `KEEPALIVE_INTERVAL_S` (60–840 с) делает GET
своего публичного `/healthz`: Render усыпляет бесплатный сервис после 15 минут без входящих запросов, а long polling
— исходящий. `Lifecycle` запускает его рядом с веб-сервером и останавливает перед ним; в watchdog он не входит, ошибки —
WARNING не чаще раза в час, только тип ошибки или HTTP-статус. `DATABASE_URL` вида `postgres://` / `postgresql://`
получает драйвер asyncpg (`db.engine.normalize_url`, в `Settings` и в migrations/env.py); ошибки БД в логах —
`ProgrammingError (UndefinedTableError 42P01) at …` (тип ошибки драйвера и SQLSTATE, без текста).

```python
class Watchdog:                                            # dumpbot/watchdog.py, потокобезопасный
    def __init__(self, *, check_every_s: float = 15.0, monotonic: Callable[[], float] = time.monotonic,
                 suspend_after_s: float = 60.0) -> None
    def watch(self, name: str, limit_s: float) -> None      # с этого момента name должен отмечаться не реже limit_s
    def beat(self, name: str) -> None
    def stalled(self) -> dict[str, float]                   # молчащие дольше лимита → секунды с последней отметки
    def check(self) -> dict[str, float]                     # проверка потока: stalled(), но если она опоздала больше
                                                            # suspend_after_s (процесс замирал: сон ПК) — все циклы
                                                            # начинают отсчёт заново, ничего не сообщается
    def start(self, on_stall: Callable[[dict[str, float]], object]) -> None   # daemon-поток; первый stall → on_stall
    def stop(self) -> None                                  # остановить поток и забыть зарегистрированные циклы
```
`dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())`, `drop_pending_updates` НЕ использовать.

## 5. Тесты

- `tests/domain/` — чистые unit-тесты: все правила таблицы 4.1, DST (2026-10-25 и 2027-03-28), night_cutoff, повторы, тихие часы.
- `tests/ai/` — фейковый HTTP (aiohttp test server или подмена метода), валидация, повтор, фолбэк, учёт стоимости.
- `tests/services/`, `tests/workers/` — интеграционные с Postgres (`TEST_DATABASE_URL`, фикстуры из `tests/conftest.py`).
- `tests/bot/` — рендер views, cbdata ≤ 64 байт, разбор Message → raw (на собранных вручную aiogram-объектах).
- `tests/botapp/` — диспетчер, middlewares, роутеры, FSM, жизненный цикл (фейковая сессия aiogram).
- `tests/e2e/` — всё приложение (`build_app` + `__main__.poll`) против локальных фейков Telegram Bot API и chutes.ai
  на 127.0.0.1: голос → карточка → напоминание → «Готово», запросы, правки, сбой ИИ, фото, альбом, дайджесты,
  бюджет, мёртвая задача. Время заморожено (`clock.utcnow`), планировщик вызывается через `Scheduler.tick(now)`.
  Фейк Telegram отклоняет невалидный HTML / слишком длинный текст / callback_data > 64 байт, как настоящий.
- `tests/test_migration_0005.py` — `alembic upgrade` по-настоящему: своя временная база `<тестовая>_mig0005` на том же
  Postgres, данные на ревизии 0004, затем 0005, `alembic check`, downgrade (no-op) и повторный upgrade.
  `tests/test_migration_0006.py` — так же для 0006 (`<тестовая>_mig0006`): app_state, held_from, downgrade и снова.
- Запуск: `TEST_DATABASE_URL=postgresql+asyncpg://dump:dump@127.0.0.1:55432/dump_test uv run pytest`.
