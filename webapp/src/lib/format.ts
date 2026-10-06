/**
 * Russian formatting of dates, times, durations and money — the same words as the bot (src/dumpbot/domain/fmt.py):
 * "сегодня 13:00", "пт 9 окт", "1 ч 30 мин", "$0.42". Dates and times are shown in the owner's timezone
 * (`Me.tz` / `Prefs.tz`), not the phone's. Pure functions: no DOM, used by the mock API too.
 */

export const WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"] as const;
export const MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"] as const;
export const MONTHS_GENITIVE = [
  "января",
  "февраля",
  "марта",
  "апреля",
  "мая",
  "июня",
  "июля",
  "августа",
  "сентября",
  "октября",
  "ноября",
  "декабря",
] as const;

/** "в октябре": the prepositional case. */
export const MONTHS_IN = [
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
] as const;

/** Wall-clock parts of a moment in a timezone. */
export interface Zoned {
  /** "YYYY-MM-DD" */
  date: string;
  /** "HH:MM" */
  time: string;
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
}

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(tz: string): Intl.DateTimeFormat {
  let f = formatters.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    formatters.set(tz, f);
  }
  return f;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** A moment (ISO string, Date or ms) as wall-clock parts in `tz`. */
export function zoned(moment: string | Date | number, tz: string): Zoned {
  const date = moment instanceof Date ? moment : new Date(moment);
  const parts: Record<string, number> = {};
  for (const p of formatter(tz).formatToParts(date)) {
    if (p.type !== "literal") parts[p.type] = Number(p.value);
  }
  const year = parts.year ?? 1970;
  const month = parts.month ?? 1;
  const day = parts.day ?? 1;
  const hour = (parts.hour ?? 0) % 24;
  const minute = parts.minute ?? 0;
  return {
    date: `${year}-${pad(month)}-${pad(day)}`,
    time: `${pad(hour)}:${pad(minute)}`,
    year,
    month,
    day,
    hour,
    minute,
  };
}

/** Today's local date in `tz`. */
export function todayIn(tz: string, now: Date | number = Date.now()): string {
  return zoned(now, tz).date;
}

/** "YYYY-MM-DDTHH:MM" of a moment in `tz` (the API's `*_local`). */
export function localDateTime(moment: string | Date | number, tz: string): string {
  const z = zoned(moment, tz);
  return `${z.date}T${z.time}`;
}

function dateParts(date: string): [number, number, number] {
  const [y, m, d] = date.slice(0, 10).split("-").map(Number);
  return [y ?? 1970, m ?? 1, d ?? 1];
}

function utcDay(date: string): number {
  const [y, m, d] = dateParts(date);
  return Date.UTC(y, m - 1, d) / 86_400_000;
}

/** Days from `a` to `b` (local dates). */
export function daysBetween(a: string, b: string): number {
  return Math.round(utcDay(b) - utcDay(a));
}

/** `date` plus `n` days. */
export function addDays(date: string, n: number): string {
  const t = new Date((utcDay(date) + n) * 86_400_000);
  return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}`;
}

/** Monday = 0 … Sunday = 6. */
export function weekday(date: string): number {
  const [y, m, d] = dateParts(date);
  return (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7;
}

/** "сегодня" / "завтра" / "вчера" (with `today`), "пт 9 окт", "9 окт 2027" (another year than `today`). */
export function fmtDate(date: string, today?: string, relative = true): string {
  const [y, m, d] = dateParts(date);
  if (today) {
    const delta = daysBetween(today, date);
    if (relative) {
      const word = delta === 0 ? "сегодня" : delta === 1 ? "завтра" : delta === -1 ? "вчера" : null;
      if (word) return word;
    }
    if (y !== dateParts(today)[0]) return `${d} ${MONTHS_SHORT[m - 1]} ${y}`;
  }
  return `${WEEKDAYS_SHORT[weekday(date)]} ${d} ${MONTHS_SHORT[m - 1]}`;
}

/** A day header: "сегодня", "завтра", else "ср 8 окт". */
export function fmtDayHeader(date: string, today: string): string {
  const delta = daysBetween(today, date);
  if (delta === 0) return `Сегодня, ${fmtLongDate(date)}`;
  if (delta === 1) return `Завтра, ${fmtLongDate(date)}`;
  return capitalize(fmtLongDate(date, true));
}

/** "6 октября" (with the weekday: "вт, 6 октября"). */
export function fmtLongDate(date: string, withWeekday = false): string {
  const [, m, d] = dateParts(date);
  const text = `${d} ${MONTHS_GENITIVE[m - 1]}`;
  return withWeekday ? `${WEEKDAYS_SHORT[weekday(date)]}, ${text}` : text;
}

/** "сегодня 13:00", "пт 9 окт 15:00", "9 окт 2027 15:00" in `tz`. */
export function fmtDateTime(moment: string | Date | number, tz: string, now: Date | number = Date.now()): string {
  const z = zoned(moment, tz);
  return `${fmtDate(z.date, todayIn(tz, now))} ${z.time}`;
}

/** "13:00" in `tz`. */
export function fmtClock(moment: string | Date | number, tz: string): string {
  return zoned(moment, tz).time;
}

/** When something was saved: "сегодня 13:05", "вчера 21:10", "6 окт", "6 окт 2025". */
export function fmtStamp(moment: string | Date | number, tz: string, now: Date | number = Date.now()): string {
  const z = zoned(moment, tz);
  const today = todayIn(tz, now);
  const delta = daysBetween(today, z.date);
  if (delta === 0 || delta === -1) return `${fmtDate(z.date, today)} ${z.time}`;
  const [y, m, d] = dateParts(z.date);
  return y === dateParts(today)[0] ? `${d} ${MONTHS_SHORT[m - 1]}` : `${d} ${MONTHS_SHORT[m - 1]} ${y}`;
}

/** "5 мин", "1 ч", "1 ч 30 мин", "1 дн", "2 дн 3 ч" (as the bot). */
export function fmtDuration(minutes: number): string {
  const sign = minutes < 0 ? "-" : "";
  const abs = Math.abs(Math.trunc(minutes));
  const days = Math.floor(abs / 1440);
  const hours = Math.floor((abs % 1440) / 60);
  const mins = abs % 60;
  const text = [days ? `${days} дн` : "", hours ? `${hours} ч` : "", mins ? `${mins} мин` : ""]
    .filter(Boolean)
    .join(" ");
  return text ? sign + text : "0 мин";
}

/** "напомнить за" choice: 0 -> "в момент", else a duration. */
export function leadLabel(minutes: number): string {
  return minutes === 0 ? "в момент" : `за ${fmtDuration(minutes)}`;
}

/** "$0.42"; tiny sums with four digits ("$0.0004"), as /usage in the bot. */
export function fmtMoney(usd: number): string {
  return usd > 0 && usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

/** The Russian plural form for `n`: plural(5, "задача", "задачи", "задач") -> "задач". */
export function plural(n: number, one: string, few: string, many: string): string {
  const rest = Math.abs(n) % 100;
  if (rest >= 11 && rest <= 14) return many;
  const last = rest % 10;
  if (last === 1) return one;
  if (last >= 2 && last <= 4) return few;
  return many;
}

/** "3 записи" */
export function count(n: number, one: string, few: string, many: string): string {
  return `${n} ${plural(n, one, few, many)}`;
}

export function capitalize(text: string): string {
  return text ? text[0]!.toUpperCase() + text.slice(1) : text;
}

/** The UTC moment of a wall time "YYYY-MM-DDTHH:MM" in `tz` (the nearest valid one across a DST gap). */
export function zonedToUtc(local: string, tz: string): Date {
  const [datePart, timePart = "00:00"] = local.split("T");
  const [y, m, d] = dateParts(datePart ?? "1970-01-01");
  const [hh, mm] = timePart.split(":").map(Number);
  const wanted = Date.UTC(y, m - 1, d, hh ?? 0, mm ?? 0);
  let guess = wanted;
  for (let i = 0; i < 3; i++) {
    const z = zoned(guess, tz);
    const shown = Date.UTC(z.year, z.month - 1, z.day, z.hour, z.minute);
    const diff = wanted - shown;
    if (diff === 0) break;
    guess += diff;
  }
  return new Date(guess);
}

/** Tags typed by hand: split on spaces / commas, without "#", lower case, no repeats. */
export function normalizeTags(text: string): string[] {
  const tags = text
    .split(/[\s,;]+/)
    .map((t) => t.replace(/^#+/, "").trim().toLowerCase())
    .filter(Boolean);
  return [...new Set(tags)];
}

/** "HH:MM" (also "9:30", "09.30" — as the bot accepts); null when it is not a time of day. */
export function parseClock(text: string): string | null {
  const m = /^\s*(\d{1,2})[:.](\d{2})\s*$/.exec(text);
  if (!m) return null;
  const h = Number(m[1]);
  const min = Number(m[2]);
  if (h > 23 || min > 59) return null;
  return `${pad(h)}:${pad(min)}`;
}

/** Clip `text` to `max` characters with "…". */
export function clip(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}
