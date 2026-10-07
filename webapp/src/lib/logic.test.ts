/** Pure helpers of the screens: rows, settings diff, card choices, inbox and job words. */
import { describe, expect, it } from "vitest";
import type { InboxStatus, JobStatus, Prefs, Row } from "../api/types";
import { row } from "../test/fixtures";
import { inboxStateText } from "../screens/Add";
import { canBeDone } from "../screens/card/CardScreen";
import { addTags } from "../screens/card/TagsEditor";
import { laterMonth } from "../screens/card/TimeSheet";
import { jobOutcome } from "../screens/card/useJob";
import { NO_FILTERS, activeFilters, toQuery } from "../screens/Feed";
import { diffPrefs, toDraft } from "../screens/Settings";
import { splitByDue } from "../screens/Tasks";
import { periodGroups } from "../screens/Today";
import { usageRatio } from "../screens/Usage";
import { jobStuck, jobWaitText } from "./jobs";
import { canCheck, isDone } from "./kinds";
import { mapRows, withDone } from "./rows";
import { DEFAULT_PREFS } from "../../mock/db.ts";

describe("rows", () => {
  it("marks the occurrence done; a series stays open", () => {
    expect(withDone(row(), true)).toMatchObject({ status: "done", occ_status: "done" });
    expect(withDone(row({ rrule: "FREQ=DAILY" }), true)).toMatchObject({ status: "open", occ_status: "done" });
    expect(withDone(row({ occurrence_id: null, occ_status: null }), true)).toMatchObject({ status: "done", occ_status: null });
    expect(withDone(row({ status: "done", occ_status: "done" }), false)).toMatchObject({ status: "open", occ_status: "open" });
  });

  it("changes rows inside every list shape and keeps untouched lists", () => {
    const flip = (r: Row) => (r.id === 2 ? { ...r, title: "Новое" } : r);
    const agenda = { day: "2026-10-06", open: [row({ id: 2 })], overdue: [row()], done: [], period_items: [] };
    const changed = mapRows(agenda, flip);
    expect(changed.open[0]!.title).toBe("Новое");
    expect(changed.overdue).toBe(agenda.overdue);
    expect(mapRows({ rows: [row({ id: 2 })] }, flip).rows[0]!.title).toBe("Новое");
    expect(mapRows({ days: [agenda] }, flip).days[0]!.open[0]!.title).toBe("Новое");
    expect(mapRows(undefined, flip)).toBeUndefined();
  });

  it("knows which rows can be ticked", () => {
    expect(canCheck(row())).toBe(true);
    expect(canCheck(row({ kind: "idea", occurrence_id: null }))).toBe(false);
    expect(canCheck(row({ kind: "note", occurrence_id: 3 }))).toBe(true);
    expect(canCheck(row({ status: "deleted" }))).toBe(false);
    expect(isDone(row({ occ_status: "done" }))).toBe(true);
  });

  it("groups running spans by label and end, earliest first", () => {
    const week = { kind: "week" as const, start: "2026-10-05", end: "2026-10-11", label: "на этой неделе" };
    const month = { kind: "month" as const, start: "2026-10-01", end: "2026-10-31", label: "в этом месяце" };
    const groups = periodGroups([row({ id: 1, period: month }), row({ id: 2, period: week }), row({ id: 3, period: week })]);
    expect(groups.map((g) => [g.title, g.rows.length])).toEqual([
      ["на этой неделе", 2],
      ["в этом месяце", 1],
    ]);
  });

  it("splits open tasks into dated and undated", () => {
    const { dated, undated } = splitByDue([row(), row({ id: 2, due_at: null }), row({ id: 3, due_at: null, period: { kind: "soon", start: "2026-10-08", end: "2026-10-08", label: "на днях" } })]);
    expect(dated.map((r) => r.id)).toEqual([1, 3]);
    expect(undated.map((r) => r.id)).toEqual([2]);
  });
});

describe("feed filters", () => {
  it("counts filters and builds the query", () => {
    expect(activeFilters(NO_FILTERS)).toBe(0);
    const f = { kinds: ["task" as const], tags: ["дом"], status: "done" as const, from: "2026-10-01", to: "" };
    expect(activeFilters(f)).toBe(4);
    expect(toQuery("  кофе ", f)).toEqual({ q: "кофе", kinds: ["task"], tags: ["дом"], status: "done", from: "2026-10-01", to: undefined });
    expect(toQuery("", NO_FILTERS)).toEqual({ q: undefined, kinds: undefined, tags: undefined, status: "any", from: undefined, to: undefined });
  });
});

describe("settings", () => {
  const prefs: Prefs = { ...DEFAULT_PREFS };

  it("sends only what changed, parsed", () => {
    const draft = { ...toDraft(prefs), soon_days: "3", budget_usd: "7,5", slot_morning: "8:30", quiet_enabled: false };
    expect(diffPrefs(prefs, draft)).toEqual({
      patch: { soon_days: 3, budget_usd: 7.5, slot_morning: "08:30", quiet_enabled: false },
      errors: {},
    });
    expect(diffPrefs(prefs, toDraft(prefs))).toEqual({ patch: {}, errors: {} });
  });

  it("names the fields that do not parse", () => {
    const draft = { ...toDraft(prefs), soon_days: "99", budget_usd: "-1", quiet_start: "25:00", tz: " " };
    expect(Object.keys(diffPrefs(prefs, draft).errors).sort()).toEqual(["budget_usd", "quiet_start", "soon_days", "tz"]);
  });
});

describe("card choices", () => {
  it("adds typed tags without repeats", () => {
    expect(addTags(["дом"], "#Дом работа")).toEqual(["дом", "работа"]);
  });

  it("offers the month after next as a span", () => {
    expect(laterMonth("2026-10-06")).toEqual({ value: "2026-12", label: "в декабре" });
    expect(laterMonth("2026-11-20")).toEqual({ value: "2027-01", label: "в январе" });
    expect(laterMonth("2026-12-01")).toEqual({ value: "2027-02", label: "в феврале" });
  });

  it("offers done for actionable kinds and dated items", () => {
    const base = { kind: "idea" as const, status: "open" as const, occurrence: null };
    expect(canBeDone(base as never)).toBe(false);
    expect(canBeDone({ ...base, kind: "task" } as never)).toBe(true);
    expect(canBeDone({ ...base, kind: "task", status: "deleted" } as never)).toBe(false);
  });
});

describe("jobs and the inbox", () => {
  const job = (over: Partial<JobStatus>): JobStatus => ({ id: 1, kind: "split", status: "done", retry_at: null, reason: null, result: null, ...over });

  it("says why a job waits", () => {
    expect(jobWaitText(job({ status: "queued", reason: "budget" }))).toMatch(/Бюджет/);
    expect(jobWaitText(job({ status: "running" }))).toBe("Думаю…");
    expect(jobWaitText(job({ status: "done" }))).toBeNull();
    expect(jobStuck(job({ status: "queued", reason: "ai_unavailable" }))).toBe(true);
    expect(jobStuck(job({ status: "queued", reason: "error" }))).toBe(false);
  });

  it("words the end of a job", () => {
    expect(jobOutcome("split", job({ result: { ok: true, reason: null, message: null, steps: 3 } }))).toMatchObject({ ok: true, text: "🪄 Готово: 3 шага" });
    expect(jobOutcome("edit_time", job({ kind: "edit_time", result: { ok: false, reason: "not_understood", message: "🤷 Не понял время" } }))).toMatchObject({
      ok: false,
      text: "🤷 Не понял время",
    });
    expect(jobOutcome("edit_time", job({ kind: "edit_time", result: { ok: true, reason: null, message: null, warnings: ["дата в прошлом"] } })).warnings).toEqual([
      "дата в прошлом",
    ]);
    expect(jobOutcome("split", job({ status: "dead", reason: "failed" })).ok).toBe(false);
  });

  it("words the state of a sent text", () => {
    const status = (over: Partial<InboxStatus>): InboxStatus => ({
      raw_id: 1,
      text: "x",
      status: "done",
      pending: false,
      created_at: "2026-10-06T10:00:00Z",
      job: null,
      items: [],
      note: null,
      ask: null,
      candidates: null,
      answer: null,
      edited: [],
      ...over,
    });
    expect(inboxStateText(status({ status: "processing", pending: true }))).toBe("⏳ Разбираю…");
    expect(inboxStateText(status({ status: "deferred", pending: true, job: job({ status: "queued", reason: "budget" }) }))).toMatch(/Бюджет/);
    expect(inboxStateText(status({ ask: { question: "?", options: ["a"] }, pending: true }))).toBe("🤔 Нужно уточнить");
    expect(inboxStateText(status({ items: [row(), row({ id: 2 })] }))).toBe("✅ Сохранено: 2");
    expect(inboxStateText(status({ status: "failed" }))).toBe("😢 Не получилось");
  });

  it("fills the spend meter", () => {
    expect(usageRatio({ ratio: 0.25, exhausted: false })).toBe(0.25);
    expect(usageRatio({ ratio: 1.7, exhausted: true })).toBe(1);
    expect(usageRatio({ ratio: null, exhausted: true })).toBe(1);
  });
});
