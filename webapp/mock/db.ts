/**
 * In-memory data of the mock API (npm run dev): items with occurrences, reminders, checklists, related items and
 * pictures, built relative to "now" so Today / the week / overdue always have something to show. As in the bot, an
 * item's time is its notification time (no "напомнить за" by default; "за день и за час" = `earlier` + `due`, the
 * last notification).
 */
import type {
  Forward,
  ItemDetail,
  ItemStatus,
  Kind,
  OccurrenceStatus,
  Period,
  PeriodKind,
  Precision,
  Prefs,
  Reminder,
  Role,
  Row,
  SourceKind,
  Step,
} from "../src/api/types.ts";
import {
  MONTHS_IN,
  addDays,
  daysBetween,
  fmtDate,
  fmtDateTime,
  localDateTime,
  todayIn,
  weekday,
  zoned,
  zonedToUtc,
} from "../src/lib/format.ts";

export const TZ = "Europe/Warsaw";
const MIN = 60_000;
const DAY = 86_400_000;
const ACTIONABLE = new Set<Kind>(["task", "event", "purchase"]);

export interface MockImage {
  w: number;
  h: number;
  hue: number;
  label: string;
}

export interface MItem {
  id: number;
  kind: Kind;
  title: string;
  details: string | null;
  tags: string[];
  status: ItemStatus;
  created: number;
  updated: number;
  doneAt: number | null;
  deletedAt: number | null;
  urls: string[];
  linkTitles: Record<string, string>;
  /** 🔕 */
  remindersOff: boolean;
  /** Notifications before the item's time ("за день и за час до 10:30": due = 09:30, earlier = [the day before]). */
  earlier: number[];
  rrule: string | null;
  recActive: boolean;
  repeatText: string | null;
  period: Period | null;
  someday: boolean;
  role: Role;
  needsConfirm: boolean;
  parent: number | null;
  position: number | null;
  due: number | null;
  precision: Precision;
  occStatus: OccurrenceStatus | null;
  occId: number | null;
  occDoneAt: number | null;
  related: number[];
  sourceKind: SourceKind;
  sourceText: string | null;
  forward: Forward | null;
  rawId: number | null;
  fromApp: boolean;
  images: MockImage[];
}

export const DEFAULT_PREFS: Prefs = {
  tz: TZ,
  slot_morning: "09:00",
  slot_lunch: "13:00",
  slot_afternoon: "15:00",
  slot_evening: "19:00",
  slot_after_work: "18:30",
  slot_night: "22:00",
  weekend_time: "11:00",
  bare_date_time: "10:00",
  task_default_time: "19:00",
  soon_days: 2,
  quiet_enabled: true,
  quiet_start: "23:00",
  quiet_end: "08:00",
  night_cutoff: "04:00",
  digest_morning_enabled: true,
  digest_morning_time: "09:00",
  digest_evening_enabled: true,
  digest_evening_time: "21:00",
  budget_usd: 5,
  stt_language: null,
  stt_mode: "auto",
  missed_grace_minutes: 360,
};

const iso = (ms: number | null) => (ms === null ? null : new Date(ms).toISOString().replace(".000Z", "Z"));
const at = (date: string, time: string) => zonedToUtc(`${date}T${time}`, TZ).getTime();
const DATE_ONLY = new Set<Precision>(["date", "fallback"]);

/** Bounds and words of a fuzzy span ("this_week", "2026-11", "summer", "soon"). */
export function periodOf(value: string, today: string, soonDays: number): { period: Period | null; due: number; precision: Precision } {
  const monday = addDays(today, -weekday(today));
  const monthStart = (y: number, m: number) => `${y}-${String(m).padStart(2, "0")}-01`;
  const monthEnd = (y: number, m: number) => addDays(m === 12 ? monthStart(y + 1, 1) : monthStart(y, m + 1), -1);
  const [ty, tm] = today.split("-").map(Number) as [number, number];
  let kind: PeriodKind = "week";
  let start = monday;
  let end = addDays(monday, 6);
  let label = "на этой неделе";
  if (value === "next_week") {
    start = addDays(monday, 7);
    end = addDays(monday, 13);
    label = "на следующей неделе";
  } else if (value === "this_month" || value === "next_month" || /^\d{4}-\d{2}$/.test(value)) {
    let [y, m] = [ty, tm];
    if (value === "next_month") [y, m] = tm === 12 ? [ty + 1, 1] : [ty, tm + 1];
    if (/^\d{4}-\d{2}$/.test(value)) [y, m] = value.split("-").map(Number) as [number, number];
    kind = "month";
    start = monthStart(y, m);
    end = monthEnd(y, m);
    label = value === "this_month" ? "в этом месяце" : `в ${MONTHS_IN[m - 1]}`;
  } else if (["spring", "summer", "autumn", "winter"].includes(value)) {
    const first = { spring: 3, summer: 6, autumn: 9, winter: 12 }[value as "spring"]!;
    const year = ty + (first + 2 < tm ? 1 : 0);
    kind = "season";
    start = monthStart(year, first);
    end = first === 12 ? monthEnd(year + 1, 2) : monthEnd(year, first + 2);
    label = { spring: "этой весной", summer: "этим летом", autumn: "этой осенью", winter: "этой зимой" }[value as "spring"]!;
  } else if (value === "soon") {
    const day = addDays(today, soonDays);
    return { period: { kind: "soon", start: day, end: day, label: "на днях" }, due: at(day, "19:00"), precision: "soon" };
  }
  let reminderDay = end;
  while (weekday(reminderDay) > 4 && reminderDay > start) reminderDay = addDays(reminderDay, -1);
  return { period: { kind, start, end, label }, due: at(reminderDay, "19:00"), precision: "period" };
}

export class MockDb {
  items = new Map<number, MItem>();
  prefs: Prefs = { ...DEFAULT_PREFS };
  nextId = 1;
  nextOcc = 1000;

  constructor(now = Date.now()) {
    seed(this, now);
  }

  today(now = Date.now()): string {
    return todayIn(this.prefs.tz, now);
  }

  add(partial: Partial<MItem> & { title: string; kind: Kind }, now = Date.now()): MItem {
    const id = partial.id ?? this.nextId++;
    this.nextId = Math.max(this.nextId, id + 1);
    const item: MItem = {
      id,
      details: null,
      tags: [],
      status: "open",
      created: now,
      updated: now,
      doneAt: null,
      deletedAt: null,
      urls: [],
      linkTitles: {},
      remindersOff: false,
      earlier: [],
      rrule: null,
      recActive: false,
      repeatText: null,
      period: null,
      someday: false,
      role: "event",
      needsConfirm: false,
      parent: null,
      position: null,
      due: null,
      precision: "none",
      occStatus: null,
      occId: null,
      occDoneAt: null,
      related: [],
      sourceKind: "text",
      sourceText: null,
      forward: null,
      rawId: null,
      fromApp: false,
      images: [],
      ...partial,
    };
    if (item.due !== null && item.occId === null) {
      item.occId = this.nextOcc++;
      item.occStatus = item.occStatus ?? "open";
    }
    this.items.set(id, item);
    return item;
  }

  get(id: number): MItem | undefined {
    return this.items.get(id);
  }

  steps(parent: number): MItem[] {
    return [...this.items.values()]
      .filter((i) => i.parent === parent && i.status !== "deleted")
      .sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
  }

  /** Give an item a time (a new open occurrence): it notifies then, and only then (reminders on again). */
  setDue(item: MItem, due: number | null, precision: Precision): void {
    item.due = due;
    item.precision = due === null ? "none" : precision;
    item.occId = due === null ? null : this.nextOcc++;
    item.occStatus = due === null ? null : "open";
    item.occDoneAt = null;
    item.earlier = [];
    item.remindersOff = false;
  }

  /** «⏰ Отложить»: the item's time moves to `fire` (the same occurrence), one notification there — never earlier: to
   * before its time, the item keeps it and gets one more notification then (as the bot's `items.snooze`). */
  snooze(item: MItem, fire: number): void {
    if (item.due !== null && fire < item.due) {
      item.earlier = [...item.earlier.filter((t) => t > fire), fire].sort((a, b) => a - b);
      return;
    }
    item.due = fire;
    if (!["exact", "slot", "relative", "date", "fallback"].includes(item.precision)) item.precision = "exact";
    item.period = null;
    item.earlier = [];
  }

  // --- views -----------------------------------------------------------------------------------------------------

  rowWhen(item: MItem, now: number): string | null {
    if (item.someday) return "когда-нибудь";
    if (item.period) return item.period.label;
    if (item.due === null) return null;
    const z = zoned(item.due, this.prefs.tz);
    const when = DATE_ONLY.has(item.precision) ? fmtDate(z.date, this.today(now)) : fmtDateTime(item.due, this.prefs.tz, now);
    return item.role === "deadline" ? `до ${when}` : when;
  }

  /** The pending notifications: the earlier ones, the item's time itself, a deadline's evening before. */
  reminders(item: MItem, now: number): Reminder[] {
    if (item.due === null || item.occStatus !== "open" || item.someday || item.remindersOff) return [];
    const auto = !["exact", "slot", "relative"].includes(item.precision);
    const due = item.due;
    const times = [...item.earlier, due].map((fire) => ({ fire, lead: Math.max(0, Math.round((due - fire) / MIN)), auto }));
    if (item.role === "deadline") {
      const eve = addDays(zoned(item.due, this.prefs.tz).date, -1);
      const fire = zonedToUtc(`${eve}T${this.prefs.task_default_time}`, this.prefs.tz).getTime();
      times.push({ fire, lead: Math.round((item.due - fire) / MIN), auto: true });
    }
    return times
      .filter((t) => t.fire > now)
      .sort((a, b) => a.fire - b.fire)
      .map((t, i) => ({ id: item.id * 100 + i, fire_at: iso(t.fire)!, lead_minutes: t.lead, auto: t.auto, status: "pending" }));
  }

  /** The card's time line, as the bot's: "🔔 завтра 13:00", "🔔 завтра 09:30 и 10:15", "⏳ до пт 9 окт · 🔔 чт 19:00,
   * пт 10:00" — the notifications grouped by day. */
  cardWhen(item: MItem, now: number): string | null {
    if (item.someday) return "🌙 когда-нибудь";
    const tz = this.prefs.tz;
    const today = this.today(now);
    const reminders = this.reminders(item, now);
    const shortDay = (date: string) => {
      const delta = daysBetween(today, date);
      return delta === 0 ? "сегодня" : delta === 1 ? "завтра" : delta < 7 ? (fmtDate(date).split(" ")[0] ?? "") : fmtDate(date, today, false);
    };
    const byDay = (label: (date: string) => string) => {
      const days = new Map<string, string[]>();
      for (const r of reminders) {
        const z = zoned(r.fire_at, tz);
        const times = days.get(z.date) ?? [];
        if (!times.includes(z.time)) times.push(z.time);
        days.set(z.date, times);
      }
      const joined = (times: string[]) => (times.length === 1 ? times[0] : `${times.slice(0, -1).join(", ")} и ${times[times.length - 1]}`);
      return [...days].map(([date, times]) => `${label(date)} ${joined(times)}`).join(", ");
    };
    const notify = reminders.length ? ` · 🔔 ${byDay(shortDay)}` : "";
    if (item.period) return `🗓 ${item.period.label} (до ${fmtDate(item.period.end, today, false)})${notify}`;
    if (item.due === null) return null;
    if (item.role === "deadline") return `⏳ до ${this.rowWhen(item, now)?.replace(/^до /, "")}${notify}`;
    if (item.occStatus === "done") return `⏰ ${this.rowWhen(item, now)}`;
    if (reminders.length) return `🔔 ${byDay((date) => fmtDate(date, today))}`;
    if (item.due <= now) return `⏰ ${fmtDateTime(item.due, tz, now)} · просрочено`;
    return `🔕 ${fmtDateTime(item.due, tz, now)}`;
  }

  row(item: MItem, now: number): Row {
    const steps = this.steps(item.id);
    const parent = item.parent !== null ? this.get(item.parent) : undefined;
    return {
      id: item.id,
      kind: item.kind,
      title: item.title,
      tags: item.tags,
      status: item.status,
      due_at: iso(item.due),
      occurrence_id: item.occId,
      occ_status: item.occStatus,
      rrule: item.recActive ? item.rrule : null,
      repeat_text: item.recActive ? item.repeatText : null,
      created_at: iso(item.created)!,
      snippet: (item.details ?? item.sourceText)?.slice(0, 200) ?? null,
      urls: item.urls,
      period: item.period,
      someday: item.someday,
      parent_id: item.parent,
      parent_title: parent?.title ?? null,
      steps_done: steps.filter((s) => s.status === "done").length,
      steps_total: steps.length,
      when_text: this.rowWhen(item, now),
      images: item.images.length,
    };
  }

  step(item: MItem, now: number): Step {
    return {
      id: item.id,
      position: item.position ?? 0,
      title: item.title,
      done: item.status === "done",
      due_at: iso(item.due),
      precision: item.due === null ? null : item.precision,
      when_text: item.due === null ? null : this.rowWhen(item, now),
    };
  }

  detail(item: MItem, now: number, warnings: string[] = []): ItemDetail {
    const steps = this.steps(item.id);
    const parent = item.parent !== null ? this.get(item.parent) : undefined;
    const related = [...this.items.values()].filter((i) => i.status !== "deleted" && (item.related.includes(i.id) || i.related.includes(item.id)));
    const canSplit = item.kind === "task" && item.status === "open" && !item.recActive && item.parent === null && steps.length === 0;
    return {
      id: item.id,
      kind: item.kind,
      title: item.title,
      details: item.details,
      tags: item.tags,
      status: item.status,
      created_at: iso(item.created)!,
      updated_at: iso(item.updated)!,
      done_at: iso(item.doneAt),
      deleted_at: iso(item.deletedAt),
      urls: item.urls,
      link_titles: item.linkTitles,
      reminders_off: item.remindersOff,
      rrule: item.rrule,
      rec_active: item.recActive,
      repeat_text: item.repeatText,
      period: item.period,
      someday: item.someday,
      role: item.role,
      needs_confirm: item.needsConfirm,
      parent: parent ? { id: parent.id, title: parent.title } : null,
      position: item.position,
      occurrence:
        item.due === null || item.someday
          ? null
          : {
              id: item.occId!,
              due_at: iso(item.due)!,
              due_local: localDateTime(item.due, this.prefs.tz),
              precision: item.precision,
              status: item.occStatus ?? "open",
              done_at: iso(item.occDoneAt),
            },
      reminders: this.reminders(item, now),
      when_text: this.cardWhen(item, now),
      steps: steps.map((s) => this.step(s, now)),
      steps_done: steps.filter((s) => s.status === "done").length,
      steps_total: steps.length,
      related: related.map((r) => ({ id: r.id, kind: r.kind, title: r.title, created_at: iso(r.created)! })),
      source: {
        kind: item.sourceKind,
        text: item.sourceText,
        forward: item.forward,
        raw_id: item.rawId,
        from_app: item.fromApp,
        message_date: iso(item.created),
      },
      images: item.images.map((img, n) => ({ n, url: `/api/files/${item.id}/${n}`, mime_type: "image/svg+xml", width: img.w, height: img.h })),
      can_split: canSplit,
      split_pending: false,
      warnings,
    };
  }

  // --- lists -----------------------------------------------------------------------------------------------------

  private live(): MItem[] {
    return [...this.items.values()].filter((i) => i.status !== "deleted");
  }

  private listed(i: MItem): boolean {
    return i.parent === null || i.due !== null;
  }

  agenda(now: number, offset = 0) {
    const today = this.today(now);
    const day = addDays(today, offset);
    const tz = this.prefs.tz;
    const local = (ms: number) => zoned(ms, tz).date;
    const byDue = (a: MItem, b: MItem) => (a.due ?? 0) - (b.due ?? 0);
    const items = this.live().filter((i) => this.listed(i));
    const open = items
      .filter((i) => i.occStatus === "open" && i.due !== null && !i.period && !i.someday && local(i.due) === day)
      .sort(byDue);
    const overdue =
      offset === 0
        ? items
            .filter((i) => i.occStatus === "open" && i.due !== null && !i.period && i.due < at(today, "00:00") && daysBetween(local(i.due), today) <= 30)
            .sort(byDue)
        : [];
    const done = items.filter((i) => {
      const doneAt = i.occDoneAt ?? i.doneAt;
      return doneAt !== null && (i.status === "done" || i.occStatus === "done") && local(doneAt) === day;
    });
    const periods = items.filter(
      (i) => i.status === "open" && i.period && i.period.kind !== "soon" && i.period.start <= day && day <= i.period.end,
    );
    const rows = (list: MItem[]) => list.map((i) => this.row(i, now));
    return { day, open: rows(open), overdue: rows(overdue), done: rows(done), period_items: rows(periods) };
  }

  tasks(now: number): Row[] {
    const open = this.live().filter(
      (i) => i.status === "open" && ACTIONABLE.has(i.kind) && !i.someday && this.listed(i) && (i.occStatus ?? "open") === "open",
    );
    const dated = open.filter((i) => i.due !== null).sort((a, b) => a.due! - b.due!);
    const undated = open.filter((i) => i.due === null).sort((a, b) => b.created - a.created);
    return [...dated, ...undated].map((i) => this.row(i, now));
  }

  someday(now: number): Row[] {
    return this.live()
      .filter((i) => i.someday && i.status === "open")
      .sort((a, b) => b.created - a.created)
      .map((i) => this.row(i, now));
  }

  recurring(now: number): Row[] {
    return this.live()
      .filter((i) => i.rrule && i.recActive)
      .map((i) => this.row(i, now));
  }

  feed(
    now: number,
    q: { q: string; kinds: string[]; tags: string[]; status: string; from: string; to: string; cursor: string; limit: number },
  ): { rows: Row[]; next_cursor: string | null } {
    const words = q.q.toLowerCase().split(/\s+/).filter(Boolean);
    let list = [...this.items.values()].filter((i) => (q.status === "deleted" ? i.status === "deleted" : i.status !== "deleted"));
    if (q.status === "open" || q.status === "done") list = list.filter((i) => i.status === q.status);
    if (!words.length) list = list.filter((i) => this.listed(i));
    if (words.length) {
      list = list.filter((i) => {
        const hay = `${i.title} ${i.details ?? ""} ${i.sourceText ?? ""} ${i.tags.map((t) => `#${t}`).join(" ")}`.toLowerCase();
        return words.every((w) => hay.includes(w.replace(/^#/, "")));
      });
    }
    if (q.kinds.length) list = list.filter((i) => q.kinds.includes(i.kind));
    if (q.tags.length) list = list.filter((i) => q.tags.every((t) => i.tags.includes(t)));
    const tz = this.prefs.tz;
    if (q.from) list = list.filter((i) => zoned(i.created, tz).date >= q.from);
    if (q.to) list = list.filter((i) => zoned(i.created, tz).date <= q.to);
    list.sort((a, b) => b.created - a.created || b.id - a.id);
    if (q.cursor) {
      const [created, id] = q.cursor.split("_").map(Number) as [number, number];
      list = list.filter((i) => i.created < created || (i.created === created && i.id < id));
    }
    const page = list.slice(0, q.limit);
    const last = page.length === q.limit ? page[page.length - 1] : undefined;
    return { rows: page.map((i) => this.row(i, now)), next_cursor: last ? `${last.created}_${last.id}` : null };
  }
}

// --- seed --------------------------------------------------------------------------------------------------------

function seed(db: MockDb, now: number): void {
  const today = db.today(now);
  const day = (n: number) => addDays(today, n);
  const ago = (days: number, hours = 0) => now - days * DAY - hours * 3_600_000;

  db.add({ id: 1, kind: "task", title: "Позвонить в банк", tags: ["финансы"], due: at(today, "15:00"), precision: "exact", created: ago(1, 3) }, now);
  // "напомни за час и за 15 минут": notified at 09:30 and 10:15 (the item's time), the 10:30 itself is in the title
  db.add(
    {
      id: 2,
      kind: "event",
      title: "Стоматолог (10:30)",
      details: "Кабинет 12, взять полис",
      due: at(day(1), "10:15"),
      earlier: [at(day(1), "09:30")],
      precision: "exact",
      created: ago(3),
    },
    now,
  );
  db.add(
    {
      id: 3,
      kind: "purchase",
      title: "Купить наушники Sony",
      tags: ["покупки"],
      due: at(today, "19:00"),
      precision: "default",
      related: [20],
      created: ago(0, 2),
      sourceKind: "voice",
      sourceText: "надо купить наушники сони, старые сломались",
    },
    now,
  );
  const move = periodOf("this_week", today, 2);
  db.add({ id: 4, kind: "task", title: "Переезд", tags: ["дом"], period: move.period, due: move.due, precision: move.precision, created: ago(2) }, now);
  db.add({ id: 5, kind: "task", title: "Упаковать вещи", parent: 4, position: 1, created: ago(2) }, now);
  db.add({ id: 6, kind: "task", title: "Заказать машину", parent: 4, position: 2, status: "done", doneAt: ago(1), created: ago(2) }, now);
  db.add({ id: 7, kind: "task", title: "Отдать ключи хозяйке", parent: 4, position: 3, due: at(day(2), "15:00"), precision: "exact", created: ago(2) }, now);
  db.add({ id: 8, kind: "idea", title: "Приложение для учёта прочитанных книг", details: "Сканировать ISBN камерой, заметки к главам", tags: ["идеи"], created: ago(3, 5) }, now);
  db.add({ id: 9, kind: "note", title: "Размер обуви Пети — 34", tags: ["семья"], created: ago(12) }, now);
  db.add(
    {
      id: 10,
      kind: "link",
      title: "Как варить кофе в турке",
      urls: ["https://example.com/coffee"],
      linkTitles: { "https://example.com/coffee": "Кофе в турке: пошаговый рецепт" },
      sourceKind: "link",
      created: ago(4),
    },
    now,
  );
  db.add(
    {
      id: 11,
      kind: "meme",
      title: "Кот и понедельник",
      images: [{ w: 800, h: 600, hue: 30, label: "🐱" }],
      sourceKind: "forward",
      forward: { type: "channel", name: "Котики", url: "https://t.me/example/1", date: iso(ago(5)) },
      created: ago(5),
    },
    now,
  );
  db.add({ id: 12, kind: "quote", title: "«Лучшее — враг хорошего»", details: "Вольтер", created: ago(6) }, now);
  db.add({ id: 13, kind: "task", title: "Оплатить интернет", tags: ["финансы"], due: at(day(-1), "19:00"), precision: "default", created: ago(4) }, now);
  db.add(
    {
      id: 14,
      kind: "task",
      title: "Вынести мусор",
      rrule: "FREQ=WEEKLY;BYDAY=MO",
      recActive: true,
      repeatText: "каждый пн",
      due: at(addDays(today, (7 - weekday(today)) % 7 || 7), "20:00"),
      precision: "exact",
      created: ago(30),
    },
    now,
  );
  db.add({ id: 15, kind: "task", title: "Выучить испанский", someday: true, created: ago(20) }, now);
  db.add(
    { id: 16, kind: "task", title: "Забрать посылку", due: at(today, "12:00"), precision: "exact", occStatus: "done", occDoneAt: now - 3_600_000, status: "done", doneAt: now - 3_600_000, created: ago(2) },
    now,
  );
  db.add(
    {
      id: 17,
      kind: "other",
      title: "Чек из магазина",
      images: [
        { w: 600, h: 900, hue: 200, label: "🧾" },
        { w: 900, h: 600, hue: 120, label: "🛍" },
      ],
      sourceKind: "photo",
      sourceText: "ИТОГО 42,50 ЗЛ",
      created: ago(1, 6),
    },
    now,
  );
  const tires = periodOf(`${addDays(today, 31).slice(0, 7)}`, today, 2);
  db.add({ id: 18, kind: "task", title: "Поменять шины", tags: ["машина"], period: tires.period, due: tires.due, precision: tires.precision, created: ago(7) }, now);
  db.add({ id: 19, kind: "note", title: "Старая заметка", status: "deleted", deletedAt: ago(1), created: ago(40) }, now);
  db.add({ id: 20, kind: "purchase", title: "Наушники для бега", status: "done", doneAt: ago(60), created: ago(70) }, now);
  db.add({ id: 21, kind: "task", title: "Сдать отчёт", role: "deadline", due: at(day(3), "10:00"), precision: "date", needsConfirm: true, created: ago(0, 5) }, now);
  for (let n = 1; n <= 45; n++) {
    db.add({ kind: n % 3 ? "note" : "idea", title: `Заметка из архива №${n}`, details: "Старая запись, чтобы было что листать.", created: ago(14 + n * 3) }, now);
  }
}
