/** ☐ / ☑ of list rows: exactly the row's occurrence, as the bot's buttons under a card or a reminder. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { row } from "../test/fixtures";
import { toggleRow } from "./actions";

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

function stubFetch(): Sent[] {
  const sent: Sent[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init: RequestInit = {}) => {
    sent.push({ method: init.method ?? "GET", path: String(input), body: JSON.parse(String(init.body ?? "null")) });
    return new Response(JSON.stringify({ item: {}, message: "✅ Готово" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  return sent;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("toggleRow", () => {
  it("closes this morning's missed instance of a series, not the next one", async () => {
    const sent = stubFetch();
    await toggleRow(row({ id: 7, occurrence_id: 70, occ_status: "missed", rrule: "FREQ=DAILY" }));
    expect(sent).toEqual([{ method: "POST", path: "/api/items/7/done", body: { occurrence_id: 70 } }]);
  });

  it("closes Thursday's row of the week while Wednesday's is still open", async () => {
    const sent = stubFetch();
    await toggleRow(row({ id: 7, occurrence_id: 71, occ_status: "open", rrule: "FREQ=DAILY" }));
    expect(sent[0]).toEqual({ method: "POST", path: "/api/items/7/done", body: { occurrence_id: 71 } });
  });

  it("takes back exactly a done row", async () => {
    const sent = stubFetch();
    await toggleRow(row({ id: 7, occurrence_id: 69, occ_status: "done", rrule: "FREQ=DAILY" }));
    expect(sent[0]).toEqual({ method: "POST", path: "/api/items/7/undone", body: { occurrence_id: 69 } });
  });

  it("acts on the item itself without an occurrence of that state", async () => {
    const sent = stubFetch();
    await toggleRow(row({ id: 8, occurrence_id: null, occ_status: null }));
    await toggleRow(row({ id: 9, status: "done", occurrence_id: 90, occ_status: "cancelled" }));
    expect(sent).toEqual([
      { method: "POST", path: "/api/items/8/done", body: {} },
      { method: "POST", path: "/api/items/9/undone", body: {} },
    ]);
  });
});
