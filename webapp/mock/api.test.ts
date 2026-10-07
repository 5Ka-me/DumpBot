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
