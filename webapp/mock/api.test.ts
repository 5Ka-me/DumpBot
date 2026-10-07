/** The dev mock API keeps the backend's rules where screens depend on them. */
import { describe, expect, it } from "vitest";
import { MockApi } from "./api.ts";

const AUTH = "tma test";

describe("PATCH /api/items/{id} period", () => {
  it("turns a deadline item into that span, like the backend", () => {
    const api = new MockApi();
    const deadline = api.db.get(21)!; // «Сдать отчёт», role deadline
    expect(deadline.role).toBe("deadline");

    const answer = api.handle({ method: "PATCH", path: "/api/items/21", body: { period: "next_week" }, query: new URLSearchParams(), authorization: AUTH });

    expect(answer.status).toBe(200);
    const { item } = answer.json as { item: { role: string; period: { kind: string } | null; occurrence: { precision: string } | null } };
    expect(item.role).toBe("event");
    expect(item.period?.kind).toBe("week");
    expect(item.occurrence?.precision).toBe("period");
  });
});

describe("the item's time is its notification time (2026-10-07)", () => {
  const NOW = Date.parse("2026-10-06T08:00:00Z"); // Tue 10:00 in Warsaw
  const call = (api: MockApi, method: string, path: string, body?: unknown) =>
    api.handle({ method, path, body, query: new URLSearchParams(), authorization: AUTH }, NOW);
  type Detail = { item: { when_text: string; occurrence: { due_local: string }; reminders: { fire_at: string }[] }; message: string };

  it("snooze moves the time and leaves one notification there", () => {
    const api = new MockApi(NOW);
    const answer = call(api, "POST", "/api/items/1/snooze", { minutes: 60 }).json as Detail;
    expect(answer.message).toBe("⏰ Напомню сегодня 11:00");
    expect(answer.item.occurrence.due_local).toBe("2026-10-06T11:00");
    expect(answer.item.reminders.map((r) => r.fire_at)).toEqual(["2026-10-06T09:00:00Z"]);
    expect(answer.item.when_text).toBe("🔔 сегодня 11:00");
    const today = call(api, "GET", "/api/today").json as { open: { id: number; due_at: string }[] };
    expect(today.open.find((r) => r.id === 1)?.due_at).toBe("2026-10-06T09:00:00Z");

    const tomorrow = call(api, "POST", "/api/items/1/snooze", { tomorrow: true }).json as Detail;
    expect(tomorrow.item.occurrence.due_local).toBe("2026-10-07T11:00");
  });

  it("lists several notification times and has no «напомнить за»", () => {
    const api = new MockApi(NOW);
    const detail = call(api, "GET", "/api/items/2").json as Record<string, unknown> & Detail["item"];
    expect(detail.when_text).toBe("🔔 завтра 09:30 · завтра 10:15");
    expect("lead_minutes" in detail).toBe(false);
    const refused = call(api, "PATCH", "/api/items/2", { lead: [30] });
    expect(refused.status).toBe(400);
    const { prefs } = call(api, "PATCH", "/api/settings", { lead_minutes: 15, soon_days: 3 }).json as { prefs: Record<string, unknown> };
    expect(prefs.soon_days).toBe(3);
    expect("lead_minutes" in prefs).toBe(false);
  });
});
