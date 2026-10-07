/**
 * The mock Mini App API for `npm run dev` (docs/features/miniapp-api.md): the same routes, shapes and error codes,
 * in memory. AI work is faked: jobs finish after ~2 s, «закинуть в свалку» turns the text into an item, words in
 * the text trigger the other outcomes ("?…" — an answer, "уточни" — a question, "перенеси" — pick an item,
 * "бюджет" — waits for the budget, "ошибка" — fails). Authorization: any "tma …" ("tma expired" → 401 expired,
 * "tma stranger" → 403).
 */
import type { InboxStatus, ItemPatch, JobStatus, Kind, Prefs, Row } from "../src/api/types.ts";
import { addDays, fmtDateTime, parseClock, zoned, zonedToUtc } from "../src/lib/format.ts";
import { MockDb, periodOf, type MItem } from "./db.ts";

export interface MockRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  authorization: string | undefined;
  body: unknown;
}

export interface MockResponse {
  status: number;
  json?: unknown;
  bytes?: { data: string; type: string };
}

class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly field?: string,
  ) {
    super(message);
  }
}

const bad = (message: string, field?: string) => new HttpError(400, "bad_request", message, field);
const conflict = (code: string, message: string) => new HttpError(409, code, message);
const KINDS: Kind[] = ["task", "event", "idea", "note", "purchase", "link", "meme", "quote", "other"];
const LABELS: Record<Kind, [string, string]> = {
  task: ["Задача", "📌"],
  event: ["Событие", "📅"],
  idea: ["Идея", "💡"],
  note: ["Заметка", "📝"],
  purchase: ["Покупка", "🛒"],
  link: ["Ссылка", "🔗"],
  meme: ["Мем", "😂"],
  quote: ["Цитата", "💬"],
  other: ["Другое", "📦"],
};

interface MJob {
  id: number;
  kind: "edit_time" | "split" | "inbox";
  created: number;
  itemId: number;
  text: string;
  result: Record<string, unknown> | null;
  finished: boolean;
  stuck: boolean;
}

interface MRaw {
  id: number;
  text: string;
  created: number;
  readyAt: number;
  jobId: number;
  resolved: boolean;
  status: InboxStatus["status"];
  items: number[];
  edited: number[];
  note: string | null;
  ask: { question: string; options: string[] } | null;
  candidates: number[] | null;
  answer: { text: string | null; title: string; rows: number[] } | null;
  stuck: boolean;
  /** An answer / pick was applied: the next "processing" ends without parsing again. */
  replied: boolean;
}

export class MockApi {
  db: MockDb;
  jobs = new Map<number, MJob>();
  raws = new Map<number, MRaw>();
  nextJob = 1;
  nextRaw = 500;
  spent = 0.4213;

  constructor(now = Date.now()) {
    this.db = new MockDb(now);
  }

  handle(req: MockRequest, now = Date.now()): MockResponse {
    try {
      this.auth(req.authorization);
      return this.route(req, now);
    } catch (e) {
      if (e instanceof HttpError) {
        return { status: e.status, json: { error: { code: e.code, message: e.message, ...(e.field ? { field: e.field } : {}) } } };
      }
      return { status: 500, json: { error: { code: "internal", message: "😢 Что-то пошло не так — попробуй ещё раз." } } };
    }
  }

  private auth(header: string | undefined): void {
    const [scheme, ...rest] = (header ?? "").split(" ");
    const data = rest.join(" ").trim();
    if (scheme?.toLowerCase() !== "tma" || !data) throw new HttpError(401, "unauthorized", "Открой приложение кнопкой «📱 Свалка» в чате с ботом.");
    if (data === "expired") throw new HttpError(401, "expired", "Сессия устарела — закрой приложение и открой его снова.");
    if (data === "stranger") throw new HttpError(403, "forbidden", "Это приложение только для владельца бота.");
  }

  private route(req: MockRequest, now: number): MockResponse {
    const { method, path } = req;
    const body = (req.body ?? {}) as Record<string, unknown>;
    const m = (re: RegExp) => re.exec(path);
    const ok = (json: unknown, status = 200): MockResponse => ({ status, json });
    let match: RegExpExecArray | null;

    if (method === "GET" && path === "/api/me") {
      return ok({
        user: { id: 1, first_name: "Дима", last_name: null, username: "dima" },
        tz: this.db.prefs.tz,
        now: new Date(now).toISOString(),
        today: this.db.today(now),
        kinds: KINDS.map((k) => ({ kind: k, label: LABELS[k][0], emoji: LABELS[k][1] })),
        local_stt: false,
      });
    }
    if (method === "GET" && path === "/api/feed") return ok(this.feed(req.query, now));
    if (method === "GET" && path === "/api/today") return ok(this.db.agenda(now));
    if (method === "POST" && path === "/api/today/move") return ok(this.moveToTomorrow(body, now));
    if (method === "GET" && path === "/api/week") return ok({ days: Array.from({ length: 7 }, (_, i) => this.db.agenda(now, i)) });
    if (method === "GET" && path === "/api/tasks") return ok({ rows: this.db.tasks(now) });
    if (method === "GET" && path === "/api/someday") return ok({ rows: this.db.someday(now) });
    if (method === "GET" && path === "/api/recurring") return ok({ rows: this.db.recurring(now) });
    if (method === "GET" && path === "/api/settings") return ok({ prefs: this.db.prefs, local_stt: false });
    if (method === "PATCH" && path === "/api/settings") return ok({ prefs: this.patchSettings(body), local_stt: false });
    if (method === "GET" && path === "/api/usage") return ok(this.usage());
    if (method === "POST" && path === "/api/ask") return ok(this.ask(body, now));
    if (method === "POST" && path === "/api/inbox") return ok(this.inbox(body, now), 202);
    if ((match = m(/^\/api\/inbox\/(\d+)$/)) && method === "GET") return ok(this.inboxStatus(Number(match[1]), now));
    if ((match = m(/^\/api\/inbox\/(\d+)\/(answer|pick)$/)) && method === "POST") {
      return ok(this.inboxReply(Number(match[1]), match[2] as "answer" | "pick", body, now), 202);
    }
    if ((match = m(/^\/api\/jobs\/(\d+)$/)) && method === "GET") return ok(this.job(Number(match[1]), now));
    if ((match = m(/^\/api\/files\/(\d+)\/(\d+)$/)) && method === "GET") {
      const size = req.query.get("size") ?? "";
      if (size !== "" && size !== "thumb") throw bad("size — только thumb", "size");
      return this.file(Number(match[1]), Number(match[2]));
    }
    if ((match = m(/^\/api\/items\/(\d+)$/))) {
      const item = this.item(Number(match[1]));
      if (method === "GET") return ok(this.db.detail(item, now));
      if (method === "PATCH") return ok(this.patchItem(item, body, now));
      throw new HttpError(405, "method_not_allowed", "Этот метод здесь не поддерживается");
    }
    if ((match = m(/^\/api\/items\/(\d+)\/(\w+)$/)) && method === "POST") {
      return this.itemAction(this.item(Number(match[1])), match[2]!, body, now);
    }
    if ((match = m(/^\/api\/steps\/(\d+)(\/toggle)?$/))) return this.stepAction(Number(match[1]), method, Boolean(match[2]), body, now);
    throw new HttpError(404, "not_found", "Нет такого адреса");
  }

  // --- items -----------------------------------------------------------------------------------------------------

  private item(id: number): MItem {
    const item = this.db.get(id);
    if (!item) throw new HttpError(404, "not_found", "Запись не найдена");
    return item;
  }

  private alive(item: MItem): void {
    if (item.status === "deleted") throw conflict("deleted", "🗑 Запись удалена — сначала верни её.");
  }

  private answer(item: MItem, message: string | null, now: number, warnings: string[] = [], extra: object = {}, status = 200): MockResponse {
    item.updated = now;
    return { status, json: { item: this.db.detail(item, now, warnings), message, ...extra } };
  }

  private patchItem(item: MItem, body: Record<string, unknown>, now: number): unknown {
    this.alive(item);
    const patch = body as ItemPatch;
    if (!Object.keys(patch).length) throw bad("Нечего менять");
    if ("title" in patch) {
      const title = String(patch.title ?? "").trim();
      if (!title || title.length > 200) throw bad("Название — от 1 до 200 символов", "title");
      item.title = title;
    }
    if ("details" in patch) item.details = (patch.details ?? "").trim() || null;
    if (patch.tags) item.tags = [...new Set(patch.tags.map((t) => t.replace(/^#/, "").toLowerCase()))];
    if (patch.kind) {
      if (!KINDS.includes(patch.kind)) throw bad("Неизвестная категория", "kind");
      item.kind = patch.kind;
      if (["task", "event", "purchase"].includes(patch.kind) && item.due === null && !item.someday) {
        this.db.setDue(item, zonedToUtc(`${this.db.today(now)}T${this.db.prefs.task_default_time}`, this.db.prefs.tz).getTime(), "default");
      }
    }
    if ("lead" in body) throw bad("Это поле не меняется", "lead"); // no "напомнить за" any more (2026-10-07)
    if (patch.period && patch.someday !== undefined) throw bad("Либо period, либо someday", "someday");
    if (patch.period === "someday" || patch.someday === true) {
      item.someday = true;
      item.period = null;
      item.role = "event";
      this.db.setDue(item, null, "none");
    } else if (patch.period) {
      // A chip is that span, also on a deadline item ("до пятницы"), as the bot's reply «перенеси на следующую неделю».
      const p = periodOf(patch.period, this.db.today(now), this.db.prefs.soon_days);
      item.someday = false;
      item.period = p.period;
      item.role = "event";
      this.db.setDue(item, p.due, p.precision);
    } else if (patch.someday === false) {
      item.someday = false;
      this.db.setDue(item, zonedToUtc(`${this.db.today(now)}T19:00`, this.db.prefs.tz).getTime(), "default");
    }
    return this.answer(item, "✅ Сохранено", now).json;
  }

  private dueFromLocal(value: unknown, field = "due_local"): { due: number; precision: "exact" | "date" } {
    const text = typeof value === "string" ? value : "";
    const m = /^(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}))?$/.exec(text);
    if (!m) throw bad("Время — ГГГГ-ММ-ДД или ГГГГ-ММ-ДДTЧЧ:ММ", field);
    const time = m[2] ?? this.db.prefs.bare_date_time;
    return { due: zonedToUtc(`${m[1]}T${time}`, this.db.prefs.tz).getTime(), precision: m[2] ? "exact" : "date" };
  }

  /** `{occurrence_id?}` of done / undone: the item's own occurrence (the mock keeps one per item). */
  private occurrenceOf(item: MItem, body: Record<string, unknown>): void {
    const extra = Object.keys(body).find((k) => k !== "occurrence_id");
    if (extra) throw bad("Можно передать только occurrence_id", extra);
    const id = body.occurrence_id;
    if (id === undefined || id === null) return;
    if (typeof id !== "number" || !Number.isInteger(id) || id <= 0) throw bad("occurrence_id — номер повторения", "occurrence_id");
    if (id !== item.occId) throw new HttpError(404, "not_found", "Этого срока у записи нет — обнови список.");
  }

  /** «📅 Перенести невыполненное на завтра»: what is due and not done today (and the overdue) -> tomorrow, same time. */
  private moveToTomorrow(body: Record<string, unknown>, now: number): { moved: number; message: string } {
    const today = this.db.today(now);
    const day = body.day === undefined ? today : body.day;
    if (day !== today && day !== addDays(today, -1)) throw bad("День — сегодня или вчера, ГГГГ-ММ-ДД", "day");
    const agenda = this.db.agenda(now);
    const ids = [...agenda.overdue, ...agenda.open]
      .filter((r) => r.occ_status === "open" && r.due_at !== null && Date.parse(r.due_at) <= now)
      .map((r) => r.id);
    for (const id of ids) {
      const item = this.db.get(id)!;
      const time = zoned(item.due!, this.db.prefs.tz).time;
      item.period = null;
      this.db.setDue(item, zonedToUtc(`${addDays(String(day), 1)}T${time}`, this.db.prefs.tz).getTime(), item.precision);
    }
    return { moved: ids.length, message: ids.length ? `📅 Перенёс на завтра: ${ids.length}` : "Нечего переносить" };
  }

  private itemAction(item: MItem, action: string, body: Record<string, unknown>, now: number): MockResponse {
    if (action === "restore") {
      item.status = item.doneAt ? "done" : "open";
      item.deletedAt = null;
      return this.answer(item, "↩️ Восстановлено", now);
    }
    if (action === "delete") {
      item.status = "deleted";
      item.deletedAt = now;
      return this.answer(item, "🗑 Удалено", now);
    }
    this.alive(item);
    switch (action) {
      case "when": {
        if ("text" in body) return this.enqueue("edit_time", item, String(body.text ?? ""), now);
        const { due, precision } = this.dueFromLocal(body.due_local);
        item.period = null;
        item.someday = false;
        this.db.setDue(item, due, precision);
        item.needsConfirm = false;
        return this.answer(item, "🕒 Время изменено", now, due < now ? ["⚠️ Это время уже прошло"] : []);
      }
      case "done": {
        this.occurrenceOf(item, body);
        if (item.recActive && item.due !== null) {
          this.db.setDue(item, item.due + 7 * 86_400_000, item.precision);
          return this.answer(item, `✅ Готово · следующий раз ${new Date(item.due).toLocaleDateString("ru")}`, now);
        }
        item.status = "done";
        item.doneAt = now;
        if (item.occId !== null) {
          item.occStatus = "done";
          item.occDoneAt = now;
        }
        for (const step of this.db.steps(item.id)) step.status = "done";
        return this.answer(item, "✅ Готово", now);
      }
      case "undone":
        this.occurrenceOf(item, body);
        item.status = "open";
        item.doneAt = null;
        if (item.occId !== null) {
          item.occStatus = "open";
          item.occDoneAt = null;
        }
        return this.answer(item, "↩️ Не сделано", now);
      case "norem":
        item.remindersOff = true;
        item.later = [];
        return this.answer(item, "🔕 Напоминания выключены", now);
      case "stoprec":
        item.recActive = false;
        return this.answer(item, "⏹ Повтор остановлен", now);
      case "snooze": {
        if (item.occStatus !== "open" || item.due === null || item.someday) throw conflict("nothing_to_snooze", "Нечего откладывать: у записи нет открытого срока.");
        if (body.tomorrow !== true && !(Number(body.minutes) >= 1 && Number(body.minutes) <= 10080)) throw bad("Нужно либо minutes (1…10080), либо tomorrow: true", "minutes");
        // The item's time moves there (tomorrow: at its time of day), as the bot's ⏰ buttons.
        const tz = this.db.prefs.tz;
        const fire =
          body.tomorrow === true
            ? zonedToUtc(`${addDays(this.db.today(now), 1)}T${zoned(item.due, tz).time}`, tz).getTime()
            : now + Number(body.minutes) * 60_000;
        this.db.snooze(item, fire);
        return this.answer(item, `⏰ Напомню ${fmtDateTime(fire, tz, now)}`, now);
      }
      case "split":
        if (this.db.steps(item.id).length) throw conflict("has_steps", "У задачи уже есть шаги.");
        if (item.kind !== "task" || item.status !== "open" || item.recActive || item.parent !== null) throw conflict("cant_split", "Разбить можно только открытую задачу.");
        return this.enqueue("split", item, "", now);
      case "steps": {
        if (item.parent !== null || item.recActive) throw conflict("cant_have_steps", "К этой записи нельзя добавить шаги: она сама шаг или повторяется.");
        const title = String(body.title ?? "").trim();
        if (!title || title.length > 120) throw bad("Название — от 1 до 120 символов", "title");
        const steps = this.db.steps(item.id);
        if (steps.length >= 20) throw conflict("checklist_full", "В чек-листе уже 20 шагов.");
        const step = this.db.add({ kind: "task", title, parent: item.id, position: steps.length + 1 }, now);
        if (body.due_local) {
          const { due, precision } = this.dueFromLocal(body.due_local);
          this.db.setDue(step, due, precision);
        }
        if (item.status === "done") item.status = "open";
        return this.answer(item, "➕ Шаг добавлен", now, [], { closed: false }, 201);
      }
      default:
        throw new HttpError(404, "not_found", "Нет такого адреса");
    }
  }

  private stepAction(id: number, method: string, toggle: boolean, body: Record<string, unknown>, now: number): MockResponse {
    const step = this.item(id);
    if (step.parent === null) throw new HttpError(404, "not_a_step", "Это не шаг чек-листа");
    const parent = this.item(step.parent);
    this.alive(parent);
    this.alive(step);
    let message: string | null = "✅ Сохранено";
    let closed = false;
    if (toggle && method === "POST") {
      const done = typeof body.done === "boolean" ? body.done : step.status !== "done";
      step.status = done ? "done" : "open";
      step.doneAt = done ? now : null;
      message = `${done ? "☑" : "☐"} ${step.title}`;
      const steps = this.db.steps(parent.id);
      if (done && steps.every((s) => s.status === "done") && parent.status !== "done") {
        parent.status = "done";
        parent.doneAt = now;
        if (parent.occId !== null) parent.occStatus = "done";
        closed = true;
        message = `🎉 Все шаги выполнены — «${parent.title}» закрыт`;
      } else if (!done && parent.status === "done") {
        parent.status = "open";
        parent.doneAt = null;
        if (parent.occId !== null) parent.occStatus = "open";
      }
    } else if (method === "PATCH") {
      if ("title" in body) {
        const title = String(body.title ?? "").trim();
        if (!title || title.length > 120) throw bad("Название — от 1 до 120 символов", "title");
        step.title = title;
      }
      if ("due_local" in body) {
        if (body.due_local === null) this.db.setDue(step, null, "none");
        else {
          const { due, precision } = this.dueFromLocal(body.due_local);
          this.db.setDue(step, due, precision);
        }
      }
    } else if (method === "DELETE") {
      step.status = "deleted";
      step.deletedAt = now;
      this.db.steps(parent.id).forEach((s, i) => (s.position = i + 1));
      message = "🗑 Шаг удалён";
    } else throw new HttpError(405, "method_not_allowed", "Этот метод здесь не поддерживается");
    return this.answer(parent, message, now, [], { closed });
  }

  // --- jobs ------------------------------------------------------------------------------------------------------

  private enqueue(kind: "edit_time" | "split", item: MItem, text: string, now: number): MockResponse {
    if (kind === "edit_time" && (!text.trim() || text.length > 300)) throw bad("Напиши время словами, до 300 символов", "text");
    const pending = [...this.jobs.values()].find((j) => j.kind === kind && j.itemId === item.id && !j.finished);
    if (pending) return { status: 202, json: { job_id: pending.id, pending: true } };
    const job: MJob = { id: this.nextJob++, kind, created: now, itemId: item.id, text, result: null, finished: false, stuck: false };
    this.jobs.set(job.id, job);
    return { status: 202, json: { job_id: job.id, pending: false } };
  }

  private job(id: number, now: number): JobStatus {
    const job = this.jobs.get(id);
    if (!job) throw new HttpError(404, "not_found", "Нет такой задачи");
    const age = now - job.created;
    if (job.stuck) return { id, kind: job.kind, status: "queued", retry_at: new Date(now + 3_600_000).toISOString(), reason: "budget", result: null };
    if (age < 700) return { id, kind: job.kind, status: "queued", retry_at: null, reason: null, result: null };
    if (age < 2200) return { id, kind: job.kind, status: "running", retry_at: null, reason: null, result: null };
    if (!job.finished) this.finish(job, now);
    return { id, kind: job.kind, status: "done", retry_at: null, reason: null, result: job.result };
  }

  private finish(job: MJob, now: number): void {
    job.finished = true;
    const item = this.db.get(job.itemId);
    if (!item || item.status === "deleted") {
      job.result = { ok: false, reason: "gone", message: "Запись удалена." };
      return;
    }
    if (job.kind === "split") {
      const titles = [`Составить план: ${item.title.toLowerCase()}`, "Собрать всё нужное", "Сделать", "Проверить результат"];
      titles.forEach((title, i) => this.db.add({ kind: "task", title, parent: item.id, position: i + 1 }, now));
      job.result = { ok: true, reason: null, message: null, steps: titles.length };
      return;
    }
    const due = this.parseWhen(job.text, now);
    if (due === null) {
      job.result = { ok: false, reason: "not_understood", message: "🤷 Не понял время — напиши, например, «завтра в 15:00»." };
      return;
    }
    item.period = null;
    item.someday = false;
    this.db.setDue(item, due, "exact");
    job.result = { ok: true, reason: null, message: null, item_id: item.id, warnings: [] };
  }

  /** "завтра в 15:00", "сегодня 18", "через 2 часа" — enough for a demo. */
  private parseWhen(text: string, now: number): number | null {
    const t = text.toLowerCase();
    const inHours = /через (\d+) ч/.exec(t);
    if (inHours) return now + Number(inHours[1]) * 3_600_000;
    const day = t.includes("послезавтра") ? 2 : t.includes("завтра") ? 1 : t.includes("сегодня") ? 0 : null;
    const clock = /(\d{1,2})(?:[:.](\d{2}))?/.exec(t);
    if (day === null && !clock) return null;
    const time = clock ? parseClock(`${clock[1]}:${clock[2] ?? "00"}`) ?? "10:00" : "10:00";
    return zonedToUtc(`${addDays(this.db.today(now), day ?? 1)}T${time}`, this.db.prefs.tz).getTime();
  }

  // --- inbox and questions ---------------------------------------------------------------------------------------

  private inbox(body: Record<string, unknown>, now: number): InboxStatus {
    const text = typeof body.text === "string" ? body.text.trim() : "";
    if (!text) throw bad("Напиши, что сохранить", "text");
    if (text.length > 4096) throw bad("Не длиннее 4096 символов", "text");
    const raw: MRaw = {
      id: this.nextRaw++,
      text,
      created: now,
      readyAt: now + 1800,
      jobId: this.nextJob++,
      resolved: false,
      status: "processing",
      items: [],
      edited: [],
      note: null,
      ask: null,
      candidates: null,
      answer: null,
      stuck: /бюджет/i.test(text),
      replied: false,
    };
    this.raws.set(raw.id, raw);
    return this.inboxStatus(raw.id, now);
  }

  private inboxStatus(id: number, now: number): InboxStatus {
    const raw = this.raws.get(id);
    if (!raw) throw new HttpError(404, "not_found", "Нет такого сообщения");
    if (!raw.resolved && !raw.stuck && now >= raw.readyAt) this.resolve(raw, now);
    const rows = (ids: number[]): Row[] => ids.map((i) => this.db.get(i)).filter((i): i is MItem => Boolean(i)).map((i) => this.db.row(i, now));
    const pending = raw.stuck || (!raw.resolved && raw.status === "processing");
    const jobState = raw.stuck ? "queued" : raw.resolved ? "done" : now - raw.created < 600 ? "queued" : "running";
    return {
      raw_id: raw.id,
      text: raw.text,
      status: raw.stuck ? "deferred" : raw.status,
      pending,
      created_at: new Date(raw.created).toISOString(),
      job: {
        id: raw.jobId,
        kind: "message",
        status: jobState,
        retry_at: raw.stuck ? new Date(now + 3_600_000).toISOString() : null,
        reason: raw.stuck ? "budget" : null,
        result: null,
      },
      items: rows(raw.items),
      note: raw.note,
      ask: raw.ask,
      candidates: raw.candidates ? rows(raw.candidates) : null,
      answer: raw.answer ? { text: raw.answer.text, title: raw.answer.title, rows: rows(raw.answer.rows) } : null,
      edited: rows(raw.edited),
    };
  }

  private resolve(raw: MRaw, now: number): void {
    raw.resolved = true;
    raw.status = "done";
    if (raw.replied) return;
    const text = raw.text;
    if (text.startsWith("?")) {
      const found = this.search(text.slice(1), now);
      raw.answer = { text: null, title: `Нашёл: ${found.length}`, rows: found.map((r) => r.id) };
      return;
    }
    if (/ошибка/i.test(text)) {
      raw.status = "failed";
      raw.note = "😢 Не смог обработать сообщение — попробуй ещё раз.";
      return;
    }
    if (/уточни/i.test(text)) {
      raw.ask = { question: "🤔 Это задача или идея?", options: ["Задача", "Идея", "Заметка"] };
      return;
    }
    if (/^(перенеси|измени)/i.test(text)) {
      raw.candidates = this.db.tasks(now).slice(0, 3).map((r) => r.id);
      return;
    }
    raw.items = [this.create(text, now, raw.id).id];
  }

  private create(text: string, now: number, rawId: number, kind?: Kind): MItem {
    const t = text.toLowerCase();
    const guessed: Kind = kind ?? (/https?:\/\//.test(t) ? "link" : /купить/.test(t) ? "purchase" : /идея/.test(t) ? "idea" : /встреча|врач|стоматолог/.test(t) ? "event" : "task");
    const item = this.db.add({ kind: guessed, title: text.replace(/^(идея|уточни)[:,]?\s*/i, "").slice(0, 120), rawId, fromApp: true, sourceText: text, created: now }, now);
    const due = this.parseWhen(text, now);
    if (/когда-нибудь/.test(t)) item.someday = true;
    else if (/на этой неделе/.test(t)) {
      const p = periodOf("this_week", this.db.today(now), 2);
      item.period = p.period;
      this.db.setDue(item, p.due, p.precision);
    } else if (due !== null && /завтра|сегодня|через|\d/.test(t)) this.db.setDue(item, due, "exact");
    else if (["task", "event", "purchase"].includes(guessed)) {
      this.db.setDue(item, zonedToUtc(`${this.db.today(now)}T19:00`, this.db.prefs.tz).getTime(), "default");
    }
    const url = /https?:\/\/\S+/.exec(text)?.[0];
    if (url) item.urls = [url];
    return item;
  }

  private inboxReply(id: number, kind: "answer" | "pick", body: Record<string, unknown>, now: number): InboxStatus {
    const raw = this.raws.get(id);
    if (!raw) throw new HttpError(404, "not_found", "Нет такого сообщения");
    if (kind === "answer") {
      if (!raw.ask) throw conflict("no_question", "Вопроса к этому сообщению нет");
      const option = Number(body.option);
      const chosen = raw.ask.options[option];
      if (chosen === undefined) throw bad("Нет такого варианта", "option");
      raw.ask = null;
      const kindOf: Record<string, Kind> = { Задача: "task", Идея: "idea", Заметка: "note" };
      raw.items = [this.create(raw.text, now, raw.id, kindOf[chosen] ?? "note").id];
      raw.note = null;
    } else {
      if (!raw.candidates) throw conflict("already_answered", "Ответ уже принят");
      const itemId = Number(body.item_id);
      if (!raw.candidates.includes(itemId)) throw conflict("not_listed", "Этой записи нет в списке");
      raw.candidates = null;
      const item = this.item(itemId);
      const due = this.parseWhen(raw.text, now);
      if (due !== null) this.db.setDue(item, due, "exact");
      raw.edited = [item.id];
      raw.note = due === null ? "🤷 Не понял, что изменить." : null;
    }
    // applied at once; the status stays "processing" for a moment, like the real job
    raw.replied = true;
    raw.resolved = false;
    raw.status = "processing";
    raw.readyAt = now + 1200;
    return this.inboxStatus(id, now);
  }

  private search(text: string, now: number): Row[] {
    const words = text.toLowerCase().replace(/[?!.,]/g, " ").split(/\s+/).filter((w) => w.length > 3);
    const all = this.db.feed(now, { q: "", kinds: [], tags: [], status: "any", from: "", to: "", cursor: "", limit: 500 }).rows;
    const found = all.filter((r) => words.some((w) => r.title.toLowerCase().includes(w.slice(0, -1))));
    return (found.length ? found : this.db.tasks(now)).slice(0, 5);
  }

  private ask(body: Record<string, unknown>, now: number) {
    const question = typeof body.question === "string" ? body.question.trim() : "";
    if (!question) throw bad("Напиши вопрос", "question");
    if (/бюджет/i.test(question)) throw new HttpError(402, "budget_exhausted", "⛔ Бюджет на ИИ исчерпан — увеличь его в настройках или подожди следующего месяца.");
    const rows = this.search(question, now);
    this.spent += 0.0004;
    const summary = /сколько|когда/i.test(question);
    return {
      answer: summary ? `Нашёл ${rows.length} — ближайшее: «${rows[0]?.title ?? "—"}» (${rows[0]?.when_text ?? "без срока"}).` : null,
      title: `По запросу «${question}»`,
      rows,
      query: { text: question, kinds: [], status: "any", date_field: "created", range: null, tags: [], answer: summary ? "summary" : "list", limit: 10 },
    };
  }

  // --- settings, usage, files ------------------------------------------------------------------------------------

  private patchSettings(body: Record<string, unknown>): Prefs {
    if (!Object.keys(body).length) throw bad("Нечего менять");
    const next: Record<string, unknown> = { ...this.db.prefs };
    for (const [key, value] of Object.entries(body)) {
      if (key === "lead_minutes") continue; // retired 2026-10-07: an older app still sends it
      if (!(key in this.db.prefs)) throw bad("Нет такой настройки", key);
      const current = (this.db.prefs as unknown as Record<string, unknown>)[key];
      if (key === "tz") {
        try {
          new Intl.DateTimeFormat("en", { timeZone: String(value) });
        } catch {
          throw bad("Не знаю такой часовой пояс. Нужно название IANA, например Europe/Warsaw.", key);
        }
        next[key] = value;
      } else if (typeof current === "string" && /^\d{2}:\d{2}$/.test(current)) {
        const clock = parseClock(String(value));
        if (!clock) throw bad("Нужно время в формате ЧЧ:ММ, например 09:30.", key);
        next[key] = clock;
      } else if (key === "budget_usd") {
        if (typeof value !== "number" || value < 0) throw bad("Нужна сумма в долларах (0 или больше), например 5 или 7.5.", key);
        next[key] = Math.round(value * 100) / 100;
      } else if (typeof current === "number") {
        if (!Number.isInteger(value) || (value as number) < 0) throw bad("Нужно целое число.", key);
        next[key] = value;
      } else next[key] = value;
    }
    this.db.prefs = next as unknown as Prefs;
    return this.db.prefs;
  }

  private usage() {
    const limit = this.db.prefs.budget_usd;
    const parts = [
      { purpose: "extract", label: "разбор сообщений", calls: 214, usd: this.spent * 0.7 },
      { purpose: "vision", label: "картинки", calls: 12, usd: this.spent * 0.18 },
      { purpose: "stt", label: "голосовые", calls: 31, usd: this.spent * 0.08 },
      { purpose: "answer", label: "ответы на вопросы", calls: 9, usd: this.spent * 0.04 },
    ];
    const month = new Intl.DateTimeFormat("ru", { month: "long", year: "numeric", timeZone: this.db.prefs.tz }).format(new Date()).replace(/\s*г\.?$/, "");
    return {
      month,
      spent_usd: this.spent,
      limit_usd: limit,
      ratio: limit > 0 ? this.spent / limit : null,
      exhausted: limit <= 0 || this.spent >= limit,
      calls: parts.reduce((s, p) => s + p.calls, 0),
      by_purpose: parts,
    };
  }

  private file(itemId: number, n: number): MockResponse {
    const image = this.db.get(itemId)?.images[n];
    if (!image) throw new HttpError(404, "not_found", "Картинки нет");
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${image.w}" height="${image.h}" viewBox="0 0 ${image.w} ${image.h}"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(${image.hue},70%,62%)"/><stop offset="1" stop-color="hsl(${image.hue + 40},70%,42%)"/></linearGradient></defs><rect width="100%" height="100%" fill="url(#g)"/><text x="50%" y="54%" font-size="${Math.min(image.w, image.h) / 3}" text-anchor="middle" dominant-baseline="middle">${image.label}</text></svg>`;
    return { status: 200, bytes: { data: svg, type: "image/svg+xml" } };
  }

  private feed(query: URLSearchParams, now: number) {
    const csv = (name: string) => (query.get(name) ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    const limit = Number(query.get("limit") ?? 30);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw bad("limit — от 1 до 100", "limit");
    return this.db.feed(now, {
      q: (query.get("q") ?? "").trim(),
      kinds: csv("kinds"),
      tags: csv("tags").map((t) => t.replace(/^#/, "").toLowerCase()),
      status: query.get("status") ?? "any",
      from: query.get("from") ?? "",
      to: query.get("to") ?? "",
      cursor: query.get("cursor") ?? "",
      limit,
    });
  }
}
