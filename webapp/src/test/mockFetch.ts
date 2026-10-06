/** Tests: `fetch` answered by the dev mock API (mock/api.ts), so screens run against realistic data. */
import { vi } from "vitest";
import { MockApi } from "../../mock/api.ts";

export interface FetchCall {
  method: string;
  path: string;
  authorization: string | null;
  body: unknown;
}

/** Replace global fetch with the mock API; returns it and the list of calls. */
export function useMockApi(): { api: MockApi; calls: FetchCall[] } {
  const api = new MockApi();
  const calls: FetchCall[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://localhost");
    const headers = new Headers(init.headers);
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    const call = { method: init.method ?? "GET", path: url.pathname, authorization: headers.get("Authorization"), body };
    calls.push(call);
    const answer = api.handle({ ...call, query: url.searchParams, authorization: call.authorization ?? undefined });
    if (answer.bytes) {
      return new Response(answer.bytes.data, { status: answer.status, headers: { "Content-Type": answer.bytes.type } });
    }
    return new Response(JSON.stringify(answer.json), {
      status: answer.status,
      headers: { "Content-Type": "application/json" },
    });
  });
  return { api, calls };
}
