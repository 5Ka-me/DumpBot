# Mini App API (итерация 2, D)

Контракт бэкенда Telegram Mini App «📱 Свалка». Решения и общая схема — [iteration-2.md, D](iteration-2.md).
TypeScript-типы этого контракта — [`webapp/src/api/types.ts`](../../webapp/src/api/types.ts) (держать в синхроне).
Реализация: `src/dumpbot/web/` (`server.py`, `auth.py`, `api_lists.py`, `api_items.py`, `api_inbox.py`, `files.py`,
`serialize.py`), тесты — `tests/web/`.

## Общее

- Веб-сервер живёт в процессе бота (aiohttp), включается `WEBAPP_ENABLED=true`, слушает `WEBAPP_HOST:WEBAPP_PORT`
  (`0.0.0.0:8080`). Наружу его публикует Tailscale Funnel: `https://dump.<tailnet>.ts.net` → `http://bot:8080`.
- Всё, что не `/api/*`, — статика фронтенда из `WEBAPP_STATIC_DIR` (`/app/webapp`): существующий файл отдаётся как есть
  (`assets/*` — `Cache-Control: public, max-age=31536000, immutable`, остальное — `no-cache`), любой другой путь без
  расширения файла — `index.html` (роутинг SPA), путь с расширением, которого нет, — 404.
- **Аутентификация каждого `/api` запроса**: заголовок `Authorization: tma <initData>`, где `initData` —
  `Telegram.WebApp.initData` как есть (строка query-параметров). Сервер проверяет подпись по алгоритму Telegram
  (data-check-string из всех полей, кроме `hash`, отсортированных по ключу, через `\n`; ключ —
  HMAC-SHA256("WebAppData", bot_token); `hash` = hex HMAC-SHA256(data-check-string, ключ)), `auth_date` — не старше
  24 ч (и не больше чем на 5 мин в будущем), `user.id == OWNER_ID`.
  - нет заголовка / не `tma` / битая или чужая подпись → **401** `unauthorized`;
  - `auth_date` старше 24 ч → **401** `expired` (фронту: закрыть и открыть приложение заново);
  - подпись верная, но пользователь не владелец (или `OWNER_ID=0`) → **403** `forbidden`.
  initData и токен не попадают ни в ответы, ни в логи.
- Тела запросов и ответов — JSON (UTF-8), тело запроса ≤ 256 КБ. Ответы `/api` — `Cache-Control: no-store` (кроме
  картинок). Строка с символом NUL (U+0000) в теле или в параметрах ленты — 400 с `field`: Postgres её не примет.
- Время:
  - моменты — ISO 8601 в UTC с `Z`: `"2026-10-06T08:00:00Z"`;
  - локальные даты — `"YYYY-MM-DD"`;
  - локальное время на стене (часовой пояс из настроек) — `"YYYY-MM-DDTHH:MM"` (поля `*_local`);
  - время суток в настройках — `"HH:MM"`.
- Тексты для человека (`when_text`, `repeat_text`, `period.label`, `message`, `note`, ошибки) — по-русски, простым
  текстом (не HTML), теми же словами, что в чате.
- Изменения из приложения будят планировщик (и обработчик очереди, если поставлена задача) так же, как кнопки в боте.
  Карточки в чате при этом не перерисовываются, кроме случаев с чек-листом: закрытие родителя последним шагом
  присылает в чат «🎉 Все шаги выполнены» и обновляет последнюю карточку родителя (как кнопки ☐/☑).

### Ошибки

Любая ошибка `/api`:

```json
{"error": {"code": "bad_request", "message": "Название — от 1 до 200 символов", "field": "title"}}
```

| HTTP | `code` | Когда |
|---|---|---|
| 400 | `bad_request` | неверный JSON / параметр; `field` — какой |
| 401 | `unauthorized`, `expired` | см. «Аутентификация» |
| 402 | `budget_exhausted` | `POST /api/ask`, `POST /api/items/{id}/split` при исчерпанном бюджете ИИ |
| 403 | `forbidden` | не владелец |
| 404 | `not_found`, `not_a_step`, `too_big` | нет записи / шага / картинки; картинка больше 20 МБ |
| 405 | `method_not_allowed` | метод не поддерживается этим путём |
| 409 | `deleted`, `has_steps`, `cant_split`, `cant_have_steps`, `checklist_full`, `nothing_to_snooze`, `already_answered`, `no_question`, `not_listed` | действие невозможно в текущем состоянии |
| 500 | `internal` | сбой сервера (текст исключения не раскрывается) |
| 502 | `ai_config`, `ai_failed`, `telegram_unavailable` | chutes отклоняет запросы / модель ответила негодным; Telegram не отдал картинку |
| 503 | `ai_unavailable` | ИИ временно недоступен (`POST /api/ask`) |

## Общие объекты

### `Row` — строка списка

| Поле | Тип | Смысл |
|---|---|---|
| `id` | number | id записи |
| `kind` | `Kind` | `task` `event` `idea` `note` `purchase` `link` `meme` `quote` `other` |
| `title` | string | |
| `tags` | string[] | без `#` |
| `status` | `"open" \| "done" \| "deleted"` | статус записи |
| `due_at` | string \| null | срок текущего (или найденного) повторения, UTC |
| `occurrence_id` | number \| null | |
| `occ_status` | `"open" \| "done" \| "missed" \| "cancelled"` \| null | |
| `rrule` | string \| null | правило повтора, только у активного повтора |
| `repeat_text` | string \| null | «каждый понедельник» |
| `created_at` | string | когда сохранена (время сообщения) |
| `snippet` | string \| null | до 200 символов подробностей / исходного текста |
| `urls` | string[] | |
| `period` | `Period` \| null | размытый срок (неделя / месяц / сезон / «на днях») |
| `someday` | boolean | «когда-нибудь» |
| `parent_id` | number \| null | шаг чек-листа → id родителя |
| `parent_title` | string \| null | шаг → название родителя («(из «Переезд»)») |
| `steps_done`, `steps_total` | number | у родителя — прогресс чек-листа («2/5»), иначе 0 |
| `when_text` | string \| null | «сегодня 13:00», «пт 9 окт 10:00», «на этой неделе», «когда-нибудь» |
| `images` | number | сколько картинок можно показать превью (`GET /api/files/{id}/{n}`) |

### `Period`

`{kind: "week" | "month" | "season" | "soon", start: "YYYY-MM-DD", end: "YYYY-MM-DD", label: string}` — границы
включительно, `label`: «на этой неделе», «в ноябре», «этой осенью», «на днях».

### `ItemDetail` — полная карточка

| Поле | Тип | Смысл |
|---|---|---|
| `id`, `kind`, `title`, `tags`, `status` | как в `Row` | |
| `details` | string \| null | подробности |
| `created_at`, `updated_at` | string | |
| `done_at`, `deleted_at` | string \| null | |
| `urls` | string[] | |
| `link_titles` | `{[url]: string}` | заголовки страниц по ссылкам |
| `reminders_off` | boolean | напоминания выключены (🔕) |
| `rrule`, `repeat_text` | string \| null | правило (и после остановки), его описание |
| `rec_active` | boolean | повтор идёт |
| `period` | `Period` \| null | |
| `someday` | boolean | |
| `role` | `"event" \| "remind" \| "deadline"` | роль времени («до пятницы» — `deadline`) |
| `needs_confirm` | boolean | бот не уверен во времени («проверь, правильно ли я понял») |
| `parent` | `{id, title}` \| null | запись — шаг этого родителя |
| `position` | number \| null | место шага в чек-листе |
| `occurrence` | `Occurrence` \| null | текущее повторение (ближайшее открытое, иначе последнее); `null` у «когда-нибудь». Его `due_at` — время напоминания (время записи) |
| `reminders` | `Reminder[]` | ожидающие напоминания текущего повторения |
| `when_text` | string \| null | строка времени карточки — когда придут напоминания: «🔔 завтра 13:00», «🔔 вс 11 окт 10:00 · пн 12 окт 09:00» («за день и за час»), «⏳ до пт 9 окт · 🔔 чт 19:00, пт 10:00», «🗓 на этой неделе (до вс 11 окт) · 🔔 пт 19:00», «🔕 завтра 13:00», «⏰ сегодня 13:00 · просрочено», «🌙 когда-нибудь» |
| `steps` | `Step[]` | чек-лист (у родителя) |
| `steps_done`, `steps_total` | number | |
| `related` | `RelatedRef[]` | «🔗 похоже на» в обе стороны, новые первыми |
| `source` | `Source` | откуда запись |
| `images` | `ImageRef[]` | картинки сообщения (всех частей альбома); у шага — `[]` |
| `can_split` | boolean | показывать «🪄 Разбить на шаги» (открытая задача без шагов, не повтор, не шаг) |
| `split_pending` | boolean | разбивка уже идёт |
| `warnings` | string[] | только в ответах на изменения: «дата в прошлом» и т.п. |

- `Occurrence`: `{id, due_at, due_local: "YYYY-MM-DDTHH:MM", precision, status, done_at}`; `precision` —
  `exact` `slot` `date` `default` `relative` `fallback` `period` `soon` (`date`/`fallback` — дата без времени суток;
  `default` — время выбрал бот, «без срока»).
- `Reminder`: `{id, fire_at, lead_minutes, auto, status}` (`lead_minutes` — за сколько до времени повторения:
  у ранних напоминаний срока, иначе 0; `auto` — время выбрано автоматически, сдвигается из тихих часов).
- Время записи — это время напоминания (2026-10-07): «встреча в 15» → 15:00, без «за 5 минут»; «напомни за 30 минут
  до встречи в 15» → 14:30 (15:00 остаётся в названии). Поля `lead_minutes` у записи больше нет (а в `Prefs` —
  настройки «напоминать за»).
- `Step`: `{id, position, title, done, due_at, precision, when_text}` (`position` — 1..n по порядку; `due_at` — только
  у шага со своим временем).
- `RelatedRef`: `{id, kind, title, created_at}`.
- `Source`: `{kind: "text"|"voice"|"photo"|"forward"|"link"|"other", text, forward, raw_id, from_app, message_date}`;
  `text` — полный текст / транскрипт / OCR; `forward` — `{type, name, url, date}` \| null у пересланного поста;
  `from_app` — запись из `POST /api/inbox`.
- `ImageRef`: `{n, url: "/api/files/{id}/{n}", mime_type, width, height}` (`width`/`height` могут быть `null`).

### `ItemResponse` — ответ на изменение

`{item: ItemDetail, message: string | null}`; `message` — тост бота («✅ Готово», «⏰ Напомню завтра 13:00»).
Ответы про шаги дополнительно содержат `closed: boolean` — этот шаг закрыл родителя (🎉).

### `JobAccepted` — поставлена задача ИИ

**202** `{job_id: number, pending: boolean}`; `pending: true` — такая задача уже шла, это её id.
Дальше фронт опрашивает `GET /api/jobs/{job_id}` (раз в 1–2 с), пока статус не `done`/`dead`, затем перечитывает запись.

## Эндпоинты

### `GET /api/me`

→ `{user: {id, first_name, last_name, username}, tz, now, today, kinds: [{kind, label, emoji}], local_stt}`.
Проверка входа и данные для старта: часовой пояс (`Europe/Warsaw`), «сейчас» сервера, категории с подписями
(«Задача», 📌) в порядке меню, настроен ли локальный Whisper.

### `GET /api/feed`

Лента: все записи, новые первыми, постранично.

| Параметр | Значение |
|---|---|
| `q` | поиск (≤ 200): полнотекстовый как `/search` (рус./англ. стемминг, подстрока названия, тег); с `q` находятся и шаги |
| `kinds` | через запятую: `task,idea` |
| `tags` | через запятую, `#` и регистр не важны |
| `status` | `any` (по умолчанию: всё, кроме удалённых), `open`, `done`, `deleted` (корзина) |
| `from`, `to` | `YYYY-MM-DD`, локальные даты сохранения, включительно |
| `cursor` | из `next_cursor` предыдущей страницы |
| `limit` | 1…100, по умолчанию 30 |

→ `{rows: Row[], next_cursor: string | null}` (`null` — дальше нет). Без `q` шаги чек-листа показываются, только если
у них есть своё время (как в других списках).

### `GET /api/today`

→ `Agenda`: `{day, open: Row[], overdue: Row[], done: Row[], period_items: Row[]}` — на сегодня (по времени),
просроченные за 30 дней, сделанные сегодня, записи с идущим размытым сроком (раздел «🗓 На этой неделе»; группировать
по `period.label` + `period.end`).

### `POST /api/today/move`

«📅 Перенести невыполненное на завтра», как кнопка вечернего дайджеста (тот же сервис `digest.move_not_done`):
`{day?: "YYYY-MM-DD"}` — сегодня (по умолчанию) или вчера (список открыт до полуночи). Открытые и пропущенные
повторения этого дня, чьё время уже наступило, и просроченные переезжают на `day` + 1 в то же время суток (с
напоминаниями); ещё не наступившие остаются. → `{moved: number, message: string}` («📅 Перенёс на завтра: 3»,
«Нечего переносить»). Другой день — 400 `day`.

### `GET /api/week`

→ `{days: Agenda[7]}` — сегодня и 6 следующих дней (`overdue` только у сегодняшнего).

### `GET /api/tasks`, `GET /api/someday`, `GET /api/recurring`

→ `{rows: Row[]}`. Открытые задачи (сначала со сроком, потом без; без «когда-нибудь»; `?limit=` 1…200, по
умолчанию 200); «когда-нибудь» (новые первыми, `?limit=`); активные повторы с ближайшим повторением.

### `GET /api/items/{id}`

→ `ItemDetail`. Удалённая запись тоже отдаётся (`status: "deleted"`, `deleted_at`) — чтобы показать «Вернуть».
404 — нет записи.

### `PATCH /api/items/{id}`

Тело — любые из полей (всё проверяется до применения, применяется одной транзакцией):

| Поле | Тип | Действие |
|---|---|---|
| `title` | string, 1…200 | переименовать |
| `details` | string ≤ 4000 \| null | подробности (пусто/`null` — убрать) |
| `tags` | string[] ≤ 20 | заменить теги (нормализуются как у ИИ: нижний регистр, без `#`) |
| `kind` | `Kind` | категория; стала задачей без времени → напоминание по умолчанию (19:00), перестала → снимается |
| `period` | `this_week` `next_week` `this_month` `next_month` `YYYY-MM` `spring` `summer` `autumn` `winter` `soon` `someday` | размытый срок без ИИ; роль времени становится `event` (у записи «до пятницы» тоже: чип — это срок, а не «до …», как ответ «перенеси на следующую неделю» в боте) |
| `someday` | boolean | `true` — «когда-нибудь» (без даты и напоминаний); `false` — убрать «когда-нибудь» (задача получит 19:00) |

`period` и `someday` вместе — 400; любое другое поле — 400 с его именем (в том числе прежнее `lead`: «напомнить
за» убрано, напомнить раньше можно словами через `POST …/when {text: "за час"}`). → `ItemResponse`
(`message: "✅ Сохранено"`). Удалённая запись — 409 `deleted`.

### `POST /api/items/{id}/when`

Ровно одно из:

- `{text: string}` (≤ 300) — как «🕒 Время» в боте: задача ИИ `edit_time` (меняется только время / повтор / «за
  час» — напоминание раньше названного времени). → 202 `JobAccepted`. Результат задачи: `{ok: true, item_id, warnings}` или
  `{ok: false, reason: "not_understood" | "gone" | "failed" | "empty", message}`.
- `{due_local: "YYYY-MM-DD" | "YYYY-MM-DDTHH:MM"}` (годы 1970…9998) — из пикера, без ИИ: новое время напоминания
  (или дата → 10:00), прежние «за …» записи сбрасываются, роль времени записи сохраняется, размытый срок
  сбрасывается. → `ItemResponse` (`"🕒 Время изменено"`, `item.warnings`: «дата в
  прошлом» и т.п.).

### Действия: `POST /api/items/{id}/<action>`

Тело не нужно (кроме `snooze` и необязательного `occurrence_id` у `done`/`undone`). → `ItemResponse`. Для удалённой
записи всё, кроме `delete`/`restore`, — 409 `deleted`.

`done` / `undone` со строки списка передают `{occurrence_id}` из `Row` — меняется именно это повторение, как кнопки
под карточкой или напоминанием в чате (`items.mark_done` / `mark_undone`). Это важно для повторов: вечером в
«Сегодня» стоит утренний раз (уже `missed`, следующий создан), в «7 дней» — четверговый, пока среда ещё открыта.
Без `occurrence_id` (кнопка карточки) — текущее повторение / последнее сделанное. Чужое или несуществующее
повторение — 404 `not_found`, не целое число — 400 `occurrence_id`.

| `action` | Что делает | `message` |
|---|---|---|
| `done` | закрыть повторение `occurrence_id` или текущее (или запись без срока); повтор переходит к следующему; родитель закрывает шаги | «✅ Готово» (+ « · следующий раз …», если открыто другое повторение) |
| `undone` | вернуть в «не сделано» повторение `occurrence_id` или последнее сделанное, напоминания перепланируются; родитель снимает отметки с шагов, закрытых вместе с ним | «↩️ Не сделано» |
| `norem` | выключить напоминания (и у следующих повторений) | «🔕 Напоминания выключены» |
| `delete` | удалить (мягко; с шагами) | «🗑 Удалено» |
| `restore` | вернуть удалённое (с теми же шагами) | «↩️ Восстановлено» |
| `stoprec` | остановить повтор (текущее повторение остаётся) | «⏹ Повтор остановлен» |
| `snooze` | тело `{minutes: 1…10080}` или `{tomorrow: true}`: текущее открытое повторение переезжает на это время (завтра — в своё время суток), напоминание одно — в это время; срок «до …» / «на этой неделе», отложенный до наступления срока, остаётся на месте; 409 `nothing_to_snooze` без открытого срока | «⏰ Напомню сегодня 10:15» |

### `POST /api/items/{id}/split`

«🪄 Разбить на шаги»: задача ИИ `split` (2–8 шагов). → 202 `JobAccepted` (повторное нажатие, пока идёт, — тот же
`job_id`, `pending: true`). 409: `has_steps`, `cant_split` (не открытая задача, повтор, шаг), `deleted`. 402
`budget_exhausted` — месячный бюджет на ИИ исчерпан (задача не ставится; `message` — до какого числа).
Результат задачи: `{ok: true, steps: N}` или `{ok: false, reason: "no_steps" | "has_steps" | "cant_split" | "gone" |
"failed", message}`.

### Чек-лист

- `POST /api/items/{id}/steps` `{title: string 1…120, due_local?: "YYYY-MM-DD" | "YYYY-MM-DDTHH:MM"}` — шаг в конец
  списка (до 20 шагов; шаг без времени не напоминает). Закрытый родитель открывается снова. → **201** `ItemResponse`
  родителя (`"➕ Шаг добавлен"`). 409: `cant_have_steps` (запись — шаг или повтор), `checklist_full`, `deleted`.
- `PATCH /api/steps/{id}` `{title?: string 1…120, due_local?: string | null}` — переименовать, задать своё время
  (`null` — убрать время). → `ItemResponse` родителя.
- `DELETE /api/steps/{id}` → `ItemResponse` родителя (`"🗑 Шаг удалён"`) + `closed`, как у `toggle`: если остальные
  шаги отмечены, родитель закрывается (🎉). Вернуть шаг — `POST /api/items/{id}/restore` с id шага: неотмеченный шаг
  открывает закрытого родителя, шаг удалённого родителя возвращает родителя.
- `POST /api/steps/{id}/toggle` `{done?: boolean}` — отметить / снять (без `done` — переключить). → `ItemResponse`
  родителя + `closed`: последний шаг закрыл родителя (`message`: «🎉 Все шаги выполнены — «Переезд» закрыт»; то же
  сообщение приходит в чат). Снятие отметки у закрытого родителя открывает его.

Для всех: 404 `not_a_step` — id не шага; 409 `deleted` — шаг или родитель удалены.

### «Закинуть в свалку»

- `POST /api/inbox` `{text: string 1…4096}` — тот же конвейер, что сообщение боту: сырое сообщение с
  `source="app"` (синтетический отрицательный `message_id`), «?» в начале — вопрос, ссылки из текста. В чат ничего
  не приходит (ни карточек, ни реакций). → **202** `InboxStatus`.
- `GET /api/inbox/{raw_id}` → `InboxStatus` (только для сообщений из приложения; иначе 404). Опрашивать, пока
  `pending`.
- `POST /api/inbox/{raw_id}/answer` `{option: number}` — ответ на уточняющий вопрос (индекс варианта с 0), как
  кнопки 🤔. → 202 `InboxStatus`. 409 `no_question`, `already_answered`.
- `POST /api/inbox/{raw_id}/pick` `{item_id: number}` — какую запись менять (из `candidates`), как кнопки «1»…«5».
  → 202 `InboxStatus`. 409 `not_listed`, `already_answered`.

`InboxStatus`:

| Поле | Тип | Смысл |
|---|---|---|
| `raw_id` | number | |
| `text` | string | |
| `status` | `"new" \| "processing" \| "done" \| "deferred" \| "failed" \| "ignored"` | `deferred` — ждёт ИИ / бюджет |
| `pending` | boolean | ещё обрабатывается (или ответ на вопрос / выбор записи) |
| `created_at` | string | |
| `job` | `JobStatus` \| null | последняя задача по сообщению (`reason`: почему ждёт) |
| `items` | `Row[]` | созданные записи (без шагов: они в карточке родителя) |
| `note` | string \| null | что бот написал бы в чате: «🤷 Не нашёл, что изменить — сохранил как запись.», «😢 Не смог обработать…» |
| `ask` | `{question, options: string[]}` \| null | уточняющий вопрос, пока на него не ответили |
| `candidates` | `Row[]` \| null | «Какую запись изменить?», пока не выбрали |
| `answer` | `{text: string \| null, title, rows: Row[]}` \| null | ответ на вопрос («?…»): `text` — словами, иначе список `rows` под заголовком `title` |
| `edited` | `Row[]` | записи, которые изменило это сообщение |

### `POST /api/ask`

`{question: string 1…1000}` (ведущий «?» не нужен) — вопрос к базе тем же путём, что «?…» в чате (ИИ → QuerySpec →
поиск → ответ словами, если ИИ попросил), без сохранения сообщения. → `{answer: string | null, title, rows: Row[],
query: QuerySpec}`; `answer: null` — показать `rows` под заголовком `title`. Ошибки: 402 `budget_exhausted`,
503 `ai_unavailable`, 502 `ai_config` / `ai_failed`.

`QuerySpec`: `{text, kinds, status: "open"|"done"|"any", date_field: "due"|"created", range, tags, answer:
"list"|"summary", limit}`.

### `GET /api/jobs/{id}`

→ `JobStatus`: `{id, kind, status: "queued"|"running"|"done"|"dead", retry_at: string | null, reason, result}`;
`reason` — почему ждёт или умерла: `budget`, `ai_unavailable`, `ai_config`, `error`, `failed` (dead), иначе `null`;
`result` — то, что вернула задача (`edit_time`, `split`, см. выше), у остальных `null`.

### Настройки

- `GET /api/settings` → `{prefs: Prefs, local_stt: boolean}`.
- `PATCH /api/settings` — любые поля `Prefs`, проверка как в боте; сохраняются все сразу или ни одно. → то же, что GET.
  Прежнее поле `lead_minutes` (старая версия приложения сохраняет форму целиком) пропускается без ошибки.
  Поднятый бюджет сразу запускает отложенные из-за него сообщения. Уже созданные напоминания не перепланируются.

`Prefs` (все поля):

| Поле | Тип | По умолчанию / проверка |
|---|---|---|
| `tz` | string | `Europe/Warsaw`; имя IANA |
| `slot_morning`, `slot_lunch`, `slot_afternoon`, `slot_evening`, `slot_after_work`, `slot_night` | `"HH:MM"` | 09:00, 13:00, 15:00, 19:00, 18:30, 22:00 |
| `weekend_time`, `bare_date_time`, `task_default_time` | `"HH:MM"` | 11:00, 10:00, 19:00 |
| `soon_days` | int 1…14 | 2 («на днях») |
| `quiet_enabled` | boolean | true |
| `quiet_start`, `quiet_end`, `night_cutoff` | `"HH:MM"` | 23:00, 08:00, 04:00 |
| `digest_morning_enabled`, `digest_evening_enabled` | boolean | true |
| `digest_morning_time`, `digest_evening_time` | `"HH:MM"` | 09:00, 21:00 |
| `budget_usd` | number ≥ 0 | 5 (округляется до центов; 0 — ИИ выключен) |
| `stt_language` | `"ru"` \| `"en"` \| null | null (авто) |
| `stt_mode` | `"auto"` \| `"local"` \| `"chutes"` | `auto` |
| `missed_grace_minutes` | int 1…1 000 000 | 360 |

Время принимается и как «9:30» / «09.30» (как в боте). Неверное значение — 400 с `field`.

### `GET /api/usage`

→ `{month: "октябрь 2026", spent_usd, limit_usd, ratio: number | null, exhausted, calls, by_purpose: [{purpose,
label, calls, usd}]}` — расходы на ИИ за текущий месяц (по убыванию суммы; `label`: «разбор сообщений»,
«картинки», «голосовые», «ответы на вопросы», «разбивка на шаги»).

### `GET /api/files/{item_id}/{n}`

Превью картинки `n` (с 0, как `ItemDetail.images[n]`) сообщения, из которого запись: байты из Telegram (getFile),
кэш в памяти (LRU ≤ 50 МБ, картинка ≤ 10 МБ кэшируется), только фото и JPEG/PNG/WebP/GIF. Авторизация как у API
(фронт грузит через `fetch` с заголовком → blob URL; браузер может отдать повтор из своего кэша). → байты с
`Content-Type` картинки, `Cache-Control: private, max-age=86400`. 404 `not_found` / `too_big`, 502
`telegram_unavailable`.

`?size=thumb` — миниатюра для строк списков: наименьший размер фото, у которого длинная сторона ≥ 160 px, или
миниатюра, которую Telegram сделал для картинки-файла (JPEG); если такой нет — сама картинка. Другое значение — 400
`size`.
