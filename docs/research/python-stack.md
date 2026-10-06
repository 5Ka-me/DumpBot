# Research: Python libraries, versions and patterns (checked 2026-10-06)

## 1. Pinned versions

| Package | Pin | Released | Notes |
|---|---|---|---|
| Python | `3.12` (image `python:3.12-slim`, currently 3.12.15, trixie) | — | 3.12 now gets security fixes only. Everything below also supports 3.13. |
| aiogram | `3.31.0` | 2026-08-26 | Supports Bot API 10.3 and Python `>=3.10,<3.15`. **Requires `pydantic<2.14`**. 3.29.0 and 3.29.1 were yanked. |
| SQLAlchemy | `sqlalchemy[asyncio]==2.1.3` | 2026-10-02 | 2.1.0 went GA on 2026-09-24. Conservative fallback: `2.0.54` (2026-09-15). |
| greenlet | 3.5.6 (comes in through the `[asyncio]` extra) | — | **In 2.1 greenlet is no longer installed by default.** Without the `[asyncio]` extra, async breaks. |
| asyncpg | `0.31.0` | 2025-11-24 | Tested against PostgreSQL 9.5–18 |
| alembic | `1.20.0` | 2026-09-11 | Needs SQLAlchemy >= 2.0. Has `async` and `pyproject_async` init templates. |
| pydantic | `2.13.5` | 2026-08-28 | Capped below 2.14 by aiogram |
| pydantic-settings | `2.15.0` | 2026-08-07 | Needs pydantic>=2.7 |
| openai | `3.24.0` | 2026-10-02 | **3.0.0 (2026-08-12) changed the default HTTP client from `httpx` to `httpx2`. `httpx` and `certifi` are no longer installed.** TLS now uses the OS trust store. |
| httpx2 | 2.13.1 (transitive) | 2026-09-23 | Pydantic's fork that continues HTTPX |
| httpx | **do not add** | 0.28.1 (2024-12-06) | 1.0 is still `.devN`. Use `httpx2` (pulled in by openai) or `aiohttp` (pulled in by aiogram, 3.14.4) instead. |
| python-dateutil | `2.9.0.post0` | 2024-03-01 | Still the latest release. Stable but slow-moving. |
| tzdata (PyPI) | `2026.5` | 2026-10-03 | Needed on the Windows dev machine. A fallback in Docker. |
| structlog | `26.1.0` | 2026-06-06 | Python >=3.10 |
| APScheduler | **do not use** | 3.11.3 stable (2026-06-28), 4.0.0a6 alpha (2025-04-27) | 4.x is still alpha and its docs say "should NOT be used in production". |
| uv | `0.12.23` (also the image `ghcr.io/astral-sh/uv:0.12.23`) | 2026-10-03 | Still 0.x, but marked "Production/Stable" |
| poetry | (alternative) 2.5.1 | 2026-09-20 | — |
| PostgreSQL | `postgres:18.6` | — | **The PG18 image moved its data path. Mount the volume at `/var/lib/postgresql`, not `.../data`.** |

**SQLAlchemy 2.0 vs 2.1:** I recommend **2.1.3**. The 2.0-style API (`Mapped`, `mapped_column`, `AsyncSession`, `select`) is unchanged, and a new project avoids a later migration. Three things to know:
- Install `sqlalchemy[asyncio]`.
- Python 3.11 or newer is required.
- The default PG driver is now psycopg, so always use an explicit `postgresql+asyncpg://` URL.

If you see a regression, drop to 2.0.54 with no code changes. 2.1 has had three patch releases in its first eight days.

## 2. aiogram 3.31 patterns

**Whitelist.** Register an `update` outer middleware. `Dispatcher.__init__` registers ErrorsMiddleware, then UserContextMiddleware, then FSM as update outer middlewares. A middleware you register afterwards therefore already sees `data["event_from_user"]`.

```python
class OwnerOnly(BaseMiddleware):
    def __init__(self, owner_id: int): self.owner_id = owner_id
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u is None or u.id != self.owner_id:
            return None                      # silently drop
        return await handler(event, data)
dp.update.outer_middleware(OwnerOnly(settings.owner_id))
```

**Routers.** Use one router per feature (`capture`, `commands`, `settings`, `callbacks`) and attach them with `dp.include_routers(...)`. Put command and callback routers *before* the catch-all capture router.

**CallbackData factories.** `class ItemCb(CallbackData, prefix="it"): action: str; item_id: int`. Build buttons with `InlineKeyboardBuilder().button(text=..., callback_data=ItemCb(...))` and handle them with `@router.callback_query(ItemCb.filter(F.action=="done"))`. Telegram limits callback data to **64 bytes**, so put DB ids in it, never text.

**Reactions.** Use `await message.react([ReactionTypeEmoji(emoji="👀")])` or `bot.set_message_reaction(...)`. A bot can set only one reaction per message. The emoji has to be on Telegram's allowed reaction list; I could not fetch that list, so check each emoji before relying on it. A workable flow is 👀 while processing, then 👌 or ✍ when saved.

**FSM storage.** The built-in options are MemoryStorage (the default, lost on restart), RedisStorage, and PyMongoStorage (MongoStorage is deprecated). There is **no SQL storage**.
- For the MVP, use **MemoryStorage**. Settings and edit dialogs are short, so losing them on restart does no harm.
- If you later want persistence, write a ~40-line `PostgresStorage(BaseStorage)` that implements `set_state`, `get_state`, `set_data`, `get_data` and `close`, keyed by `StorageKey(bot_id, chat_id, user_id, thread_id, business_connection_id, destiny)`.

**Albums (media groups).** aiogram has no built-in support. Each photo arrives as a separate update, and third-party options exist (`aiogram_album`, Raito).

I recommend grouping in the DB instead:
1. Save every raw message with its `media_group_id`.
2. Enqueue the job `album:{media_group_id}` using `ON CONFLICT DO NOTHING` and `run_after=now()+3s`.
3. The worker collects all rows that share that `media_group_id`.

This survives crashes and keeps no state in memory. An in-memory debounce middleware also works, but only because `handle_as_tasks=True` is the default; with it set to False, the sleeping first update blocks the rest.

**Forwards.** Store `message.forward_origin`, one of `MessageOriginChannel`, `MessageOriginUser`, `MessageOriginHiddenUser` or `MessageOriginChat`. For a channel, keep `chat.id`, `chat.username` and `message_id`, which gives you the link `t.me/{username}/{message_id}`.

**Polling and lifecycle.** The signature is `start_polling(*bots, polling_timeout=10, handle_as_tasks=True, allowed_updates=UNSET, handle_signals=True, close_bot_session=True, tasks_concurrency_limit=None, **kwargs)`.
- `allowed_updates` is resolved from your handlers by default. Pass `dp.resolve_used_update_types()` explicitly. Add `"message_reaction"` only if you later want "react ✅ = done"; Telegram never sends it unless you ask.
- **Do not drop pending updates.** Telegram keeps updates for up to 24 h, so messages sent while the bot was down are not lost.
- An update is confirmed only by the next `getUpdates` offset, so it can be delivered twice after a crash. Add `UNIQUE(chat_id, message_id)` with `ON CONFLICT DO NOTHING`.

**Background tasks.** Start them in `dp.startup` and cancel them in `dp.shutdown`. aiogram's SIGTERM handler stops polling, which then fires the shutdown hooks.

```python
async def on_startup(dispatcher: Dispatcher, bot: Bot):
    dispatcher["bg"] = [asyncio.create_task(job_worker(bot), name="jobs"),
                        asyncio.create_task(scheduler_loop(bot), name="sched")]
    for t in dispatcher["bg"]:
        t.add_done_callback(crash_if_failed)   # log and exit(1) so Docker restarts
async def on_shutdown(dispatcher: Dispatcher):
    for t in dispatcher["bg"]: t.cancel()
    await asyncio.gather(*dispatcher["bg"], return_exceptions=True)
    await engine.dispose()
dp.startup.register(on_startup); dp.shutdown.register(on_shutdown)
```

Also call `bot.set_my_commands(..., scope=BotCommandScopeChat(chat_id=owner_id))` at startup.

## 3. Data layer

**Engine and session:**
- `create_async_engine("postgresql+asyncpg://...", pool_size=5, pool_pre_ping=True)`
- `async_sessionmaker(engine, expire_on_commit=False)`
- Inject sessions with a DB middleware, or open one per unit of work in the workers.

**Times:** every column is `DateTime(timezone=True)`, i.e. `timestamptz`, stored in UTC. User-facing values (tz, quiet hours, the 19:00 default, lead time, digest times) live in a `user_settings` table edited from the in-bot menu. Env only supplies the defaults.

**Alembic:**
- Run `alembic init -t pyproject_async migrations`, or use `-t async`.
- Pattern: `async_engine_from_config(..., poolclass=NullPool)`, then `await conn.run_sync(do_run_migrations)`.
- In `env.py`, take the URL from pydantic-settings rather than `alembic.ini`.

**pydantic-settings:**

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    bot_token: SecretStr; owner_id: int
    database_url: str                         # postgresql+asyncpg://
    chutes_api_key: SecretStr
    chutes_base_url: str = "https://llm.chutes.ai/v1"
    default_tz: str = "Europe/Berlin"         # IANA city zone, not "CET"
```

**Timezone name:** prefer a city zone such as `Europe/Berlin` or `Europe/Warsaw` over `"CET"`. As far as I recall, since tzdata 2024b `CET` is only a backward-compatibility link to `Europe/Brussels`; I did not re-verify this.

**LLM client (openai 3.x):**
- `AsyncOpenAI(base_url=s.chutes_base_url, api_key=..., timeout=60, max_retries=1)`.
- `https://llm.chutes.ai/v1` comes from chutes.ai's `llms.txt`. It says chutes supports chat completions, JSON mode and structured outputs.
- Retry these through your own queue rather than the SDK: `APIConnectionError`, `APITimeoutError`, `RateLimitError`, `InternalServerError`.
- For an out-of-credit response, pause the queue and notify the user.
- Always validate the model output with `Model.model_validate_json`, whatever `response_format` returns.
- Store `usage` tokens per call to enforce the $5/month cap.

## 4. Redis or a queue: not justified

At ~30 messages a day, Redis would add a second stateful service whose only uses would be FSM storage and a queue. Use **Postgres as the single source of truth**, with in-process asyncio workers.

**Jobs table:** `jobs(id, kind, dedupe_key UNIQUE NULL, payload jsonb, status, attempts, run_after, locked_until, last_error)`.

**Claiming work:**
```sql
UPDATE jobs SET status='running', attempts=attempts+1, locked_until=now()+interval '5 min'
WHERE id IN (SELECT id FROM jobs WHERE status IN ('queued','running')
             AND run_after<=now() AND (locked_until IS NULL OR locked_until<now())
             ORDER BY run_after LIMIT 5 FOR UPDATE SKIP LOCKED)
RETURNING *;
```
The SQLAlchemy equivalent is `.with_for_update(skip_locked=True)`. With one process, SKIP LOCKED isn't strictly needed, but it costs nothing and makes a second worker safe later. The PostgreSQL docs describe it as suited to "queue-like" tables.

**Failure handling:** on failure set `run_after = now() + min(2**attempts min, 6 h)`. If chutes is down, the raw message stays saved and is processed later.

**Wake-up:** the handler sets an `asyncio.Event` after enqueueing, so new work starts immediately instead of waiting for the next poll.

## 5. Reminder scheduler: a DB-polling loop, not APScheduler

Why not APScheduler:
- 4.x is still alpha.
- The 3.x `SQLAlchemyJobStore` is sync and pickles jobs.
- It would create a second source of truth next to the reminders table.
- It cannot prevent a double send if the process crashes after sending.

**The loop:** sleep until `min(next fire_at, 30 s)` or until the reminder-changed Event fires. Then:
1. Claim due reminders with `UPDATE … SET status='sending', claimed_at=now() … FOR UPDATE SKIP LOCKED RETURNING`.
2. Send them.
3. Set `status='sent', tg_message_id=…`.

**Restart recovery:** set rows back to `pending` when `status='sending' AND claimed_at < now()-2 min`. This is at-least-once delivery. A duplicate can only happen if the process dies between Telegram's 200 response and the commit, which is acceptable for a personal bot.

**Missed reminders:** if `now - fire_at > grace` (for example 6 h), mark the reminder `missed` and fold it into the next digest instead of sending a stale ping.

**Digests:** keep a table `digest_runs(kind, local_date) PRIMARY KEY`. Insert the row before sending; send only if `ON CONFLICT DO NOTHING` actually inserted it. Digests are therefore sent at most once.

**Recurring tasks:**
- Store the RRULE text, `dtstart_local` (naive) and the tz name.
- Compute the next occurrence with `rrulestr(rule, dtstart=dtstart_local).after(last_local)`, then `.replace(tzinfo=ZoneInfo(tz))`, then convert to UTC. Expanding in naive wall time keeps "09:00" at 09:00 across the CET/CEST switch.
- Insert the next occurrence in the same transaction, with `UNIQUE(recurrence_id, occurrence_at)`.

**Quiet hours:** apply them when computing `deliver_at`, not inside the send loop.

**Timezone data:** `python:3.12-slim` installs `ca-certificates`, `netbase` and `tzdata` through apt. That covers both zoneinfo and httpx2's OS trust store. Still add the PyPI `tzdata` for Windows dev. Set `TZ=UTC` in the container and never read the local system zone.

## 6. Logging

Use **structlog 26.1.0** with `structlog.contextvars`. Bind `update_id`, `job_id` and `reminder_id`; they matter for tracing retries. Route aiogram and SQLAlchemy's stdlib logs through `structlog.stdlib.ProcessorFormatter` so all logs share one format: `ConsoleRenderer` in dev, `JSONRenderer` in Docker.

Never log message text or OCR output at INFO; that is the privacy requirement. If you want fewer moving parts, plain stdlib logging is enough for an MVP.

## 7. Tooling, layout, Docker

**Tooling:** **uv** over poetry. It has an official Docker guide, `uv.lock`, `uv sync --locked`, and manages Python versions.

**Layout:**
```
pyproject.toml  uv.lock  .python-version  .env.example  Dockerfile  compose.yaml
migrations/ (env.py, versions/)                      # [tool.alembic] in pyproject
src/dumpbot/
  __main__.py  config.py  logging.py
  db/        engine.py models.py repo/
  bot/       routers/{capture,commands,settings,callbacks}.py middlewares/{owner,db}.py keyboards.py cbdata.py
  ai/        client.py schemas.py prompts/ pipeline.py
  workers/   jobs.py processor.py scheduler.py heartbeat.py
  domain/    timeparse.py recurrence.py quiet_hours.py
tests/
```

**Dockerfile:**
```dockerfile
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
# local OCR (if confirmed): apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng
RUN useradd -r -u 10001 app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY pyproject.toml ./
COPY migrations ./migrations
ENV PATH="/app/.venv/bin:$PATH" TZ=UTC PYTHONUNBUFFERED=1
USER app
CMD ["python", "-m", "dumpbot"]
```

**compose.yaml.** Migrations run in a one-shot `migrate` service, so the bot never starts on a schema that failed to migrate.
```yaml
services:
  db:
    image: postgres:18.6
    environment: {POSTGRES_DB: dump, POSTGRES_USER: dump, POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}"}
    volumes: [pgdata:/var/lib/postgresql]          # PG18+ path
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
    restart: unless-stopped                         # no ports: exposed
  migrate:
    image: dumpbot:latest
    build: .
    command: ["alembic", "upgrade", "head"]
    env_file: .env
    depends_on: {db: {condition: service_healthy}}
    restart: "no"
  bot:
    image: dumpbot:latest
    env_file: .env
    depends_on:
      db: {condition: service_healthy, restart: true}
      migrate: {condition: service_completed_successfully}
    restart: unless-stopped
    healthcheck:   # scheduler loop touches /tmp/heartbeat each tick
      test: ["CMD", "python", "-c", "import os,sys,time;sys.exit(time.time()-os.path.getmtime('/tmp/heartbeat')>120)"]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 30s
volumes: {pgdata: {}}
```

## 8. Open risks and things I could not verify

- **openai 3.x is a fresh major version.** It switched to httpx2 on 2026-08-12. Don't pass custom `httpx` clients or transports, and don't rely on `respx` for mocking without checking. I did not confirm that `chat.completions.parse` exists in 3.x; I was told the README doesn't mention it. Using `create` plus pydantic validation avoids the question. A thin `aiohttp` client is a fallback (`openai[aiohttp]` / `DefaultAioHttpClient` exist).
- **SQLAlchemy 2.1 is 12 days old.** Pinning 2.0.54 is the zero-risk fallback.
- **Plain Docker does not restart unhealthy containers.** The healthcheck only shows status. The watchdog has to `exit(1)` on a stalled loop so that `restart: unless-stopped` acts.
- **Chutes:** I did not verify whether it offers an OpenAI-compatible `/v1/audio/transcriptions` endpoint for voice, how `json_schema` support varies per model, or current prices. That belongs to the separate chutes research.
- **Reactions:** I could not fetch the allowed emoji list.
- **`aiogram_album`:** I could not read its PyPI page. That doesn't matter if you take the DB-grouping approach.
- **Dependency coupling:** aiogram caps pydantic below 2.14, so uv will hold pydantic back until aiogram moves.
- **Backups:** add `pg_dump` (cron or a sidecar) early. It is single-user personal data, and losing the volume loses everything.

## Sources (read 2026-10-06)
- https://pypi.org/project/aiogram/ · https://pypi.org/pypi/aiogram/json · https://docs.aiogram.dev/en/latest/changelog.html
- https://docs.aiogram.dev/en/latest/dispatcher/middlewares.html · .../dispatcher/dispatcher.html · .../dispatcher/long_polling.html · .../dispatcher/filters/callback_data.html · .../dispatcher/finite_state_machine/storages.html · .../api/methods/set_message_reaction.html
- https://raw.githubusercontent.com/aiogram/aiogram/dev-3.x/aiogram/dispatcher/dispatcher.py · .../aiogram/fsm/storage/base.py
- https://raito.readthedocs.io/en/stable/plugins/album.html · https://github.com/TypeHintsFun/aiogram_album
- https://core.telegram.org/bots/api#getting-updates
- https://pypi.org/project/SQLAlchemy/ · https://docs.sqlalchemy.org/en/21/changelog/migration_21.html · https://docs.sqlalchemy.org/en/21/changelog/changelog_21.html · https://docs.sqlalchemy.org/en/20/changelog/changelog_20.html
- https://pypi.org/project/asyncpg/ · https://pypi.org/project/alembic/ · https://alembic.sqlalchemy.org/en/latest/cookbook.html · https://alembic.sqlalchemy.org/en/latest/changelog.html
- https://pypi.org/project/pydantic/ · https://pypi.org/pypi/pydantic-settings/json
- https://pypi.org/pypi/openai/json · https://github.com/openai/openai-python/releases · https://raw.githubusercontent.com/openai/openai-python/main/CHANGELOG.md · https://github.com/openai/openai-python/blob/main/README.md
- https://ecorpit.com/openai-python-3-0-httpx2-certifi-tls-container-breakage-2026/ (third-party, about the TLS change)
- https://pypi.org/project/httpx/ · https://pypi.org/pypi/httpx2/json · https://pypi.org/pypi/aiohttp/json · https://pypi.org/pypi/greenlet/json
- https://pypi.org/project/APScheduler/ · https://apscheduler.readthedocs.io/en/master/migration.html
- https://pypi.org/project/python-dateutil/ · https://pypi.org/project/tzdata/ · https://pypi.org/pypi/structlog/json · https://github.com/hynek/structlog/releases
- https://pypi.org/project/uv/ · https://docs.astral.sh/uv/guides/integration/docker/ · https://pypi.org/project/poetry/
- https://github.com/docker-library/python/blob/master/3.12/slim-trixie/Dockerfile · https://hub.docker.com/_/postgres · https://docs.docker.com/compose/how-tos/startup-order/
- https://www.postgresql.org/docs/current/sql-select.html (SKIP LOCKED)
- https://www.chutes.ai/llms.txt (base URL, via search result)