import { describe, expect, it, vi } from "vitest";
import type { JobStatus } from "./types";
import {
  ApiError,
  BAD_RESPONSE_TEXT,
  INTERNAL_TEXT,
  NETWORK_TEXT,
  TIMEOUT_TEXT,
  UNAVAILABLE_TEXT,
  createApi,
  feedParams,
  isAbort,
  pollJob,
} from "./client";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function apiWith(fetchImpl: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>, onAuthError = vi.fn()) {
  const fetchMock = vi.fn(fetchImpl);
  const api = createApi({ initData: () => "query_id=1&user=%7B%22id%22%3A7%7D&hash=abc", fetch: fetchMock, onAuthError, timeoutMs: 1000 });
  return { api, fetchMock, onAuthError };
}

describe("requests", () => {
  it("sends the initData as `tma` on every request", async () => {
    const { api, fetchMock } = apiWith(async () => json(200, { rows: [] }));
    await api.tasks();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/tasks");
    const headers = init!.headers as Record<string, string>;
    expect(headers.Authorization).toBe("tma query_id=1&user=%7B%22id%22%3A7%7D&hash=abc");
    expect(headers.Accept).toBe("application/json");
    expect(headers["Content-Type"]).toBeUndefined();
    expect(init!.method).toBe("GET");
  });

  it("sends JSON bodies with their content type", async () => {
    const { api, fetchMock } = apiWith(async () => json(200, { item: {}, message: "✅ Сохранено" }));
    await api.patchItem(12, { title: "Новое", tags: ["дом"] });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/items/12");
    expect(init!.method).toBe("PATCH");
    expect((init!.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(JSON.parse(String(init!.body))).toEqual({ title: "Новое", tags: ["дом"] });
  });

  it("maps every endpoint to its path and method", async () => {
    const { api, fetchMock } = apiWith(async () => json(200, {}));
    await api.whenText(3, "завтра в 10");
    await api.whenAt(3, "2026-10-07T10:00");
    await api.snooze(3, { tomorrow: true });
    await api.toggleStep(9);
    await api.toggleStep(9, true);
    await api.deleteStep(9);
    await api.inboxPick(500, 3);
    await api.action(3, "stoprec");
    const seen = fetchMock.mock.calls.map(([url, init]) => `${init!.method} ${String(url)} ${String(init!.body ?? "")}`);
    expect(seen).toEqual([
      'POST /api/items/3/when {"text":"завтра в 10"}',
      'POST /api/items/3/when {"due_local":"2026-10-07T10:00"}',
      'POST /api/items/3/snooze {"tomorrow":true}',
      "POST /api/steps/9/toggle {}",
      'POST /api/steps/9/toggle {"done":true}',
      "DELETE /api/steps/9 ",
      'POST /api/inbox/500/pick {"item_id":3}',
      "POST /api/items/3/stoprec {}",
    ]);
  });

  it("loads a picture as a blob with the same authorization", async () => {
    const { api, fetchMock } = apiWith(async () => new Response("png", { status: 200, headers: { "Content-Type": "image/png" } }));
    const blob = await api.image("/api/files/3/0");
    expect(await blob.text()).toBe("png");
    expect((fetchMock.mock.calls[0]![1]!.headers as Record<string, string>).Authorization).toMatch(/^tma /);
  });

  it("lets the browser cache pictures (the server allows a day) but never API answers", async () => {
    const { api, fetchMock } = apiWith(async (input) =>
      String(input).startsWith("/api/files/") ? new Response("jpg", { status: 200 }) : json(200, { rows: [] }),
    );
    await api.image("/api/files/3/0?size=thumb");
    await api.tasks();
    expect(fetchMock.mock.calls.map(([url, init]) => `${String(url)} ${init!.cache}`)).toEqual([
      "/api/files/3/0?size=thumb default",
      "/api/tasks no-store",
    ]);
  });

  it("sends the occurrence of a list row with done / undone, and the day to move", async () => {
    const { api, fetchMock } = apiWith(async () => json(200, {}));
    await api.occurrenceAction(3, "done", 41);
    await api.occurrenceAction(3, "undone", 40);
    await api.action(3, "done");
    await api.moveToTomorrow("2026-10-06");
    await api.moveToTomorrow();
    const seen = fetchMock.mock.calls.map(([url, init]) => `${init!.method} ${String(url)} ${String(init!.body ?? "")}`);
    expect(seen).toEqual([
      'POST /api/items/3/done {"occurrence_id":41}',
      'POST /api/items/3/undone {"occurrence_id":40}',
      "POST /api/items/3/done {}",
      'POST /api/today/move {"day":"2026-10-06"}',
      "POST /api/today/move {}",
    ]);
  });
});

describe("feedParams", () => {
  it("joins lists with commas and leaves out empty values and the default status", () => {
    expect(feedParams({})).toBe("");
    expect(feedParams({ q: "  ", status: "any" })).toBe("");
    const query = feedParams({ q: "кофе", kinds: ["task", "idea"], tags: ["дом", "работа"], status: "done", from: "2026-10-01", cursor: "abc", limit: 50 });
    const params = new URLSearchParams(query.slice(1));
    expect(query.startsWith("?")).toBe(true);
    expect(params.get("q")).toBe("кофе");
    expect(params.get("kinds")).toBe("task,idea");
    expect(params.get("tags")).toBe("дом,работа");
    expect(params.get("status")).toBe("done");
    expect(params.get("from")).toBe("2026-10-01");
    expect(params.get("to")).toBeNull();
    expect(params.get("cursor")).toBe("abc");
    expect(params.get("limit")).toBe("50");
  });
});

describe("errors", () => {
  it("turns the error body into ApiError with code, message and field", async () => {
    const { api, onAuthError } = apiWith(async () =>
      json(400, { error: { code: "bad_request", message: "Название — от 1 до 200 символов", field: "title" } }),
    );
    const error = await api.patchItem(1, { title: "" }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 400, code: "bad_request", message: "Название — от 1 до 200 символов", field: "title" });
    expect((error as ApiError).isAuth).toBe(false);
    expect(onAuthError).not.toHaveBeenCalled();
  });

  it("reports 401 and 403 to the auth listener", async () => {
    for (const [status, code] of [
      [401, "expired"],
      [401, "unauthorized"],
      [403, "forbidden"],
    ] as const) {
      const { api, onAuthError } = apiWith(async () => json(status, { error: { code, message: "нет" } }));
      const error = await api.me().catch((e: unknown) => e);
      expect(error).toMatchObject({ status, code });
      expect((error as ApiError).isAuth).toBe(true);
      expect(onAuthError).toHaveBeenCalledWith(error);
    }
  });

  it("explains a proxy's error page and other bodies that are not ours", async () => {
    const page = (status: number) => async () => new Response("<html>Bad gateway</html>", { status, headers: { "Content-Type": "text/html" } });
    await expect(apiWith(page(502)).api.today()).rejects.toMatchObject({ code: "unavailable", message: UNAVAILABLE_TEXT });
    await expect(apiWith(page(500)).api.today()).rejects.toMatchObject({ code: "internal", message: INTERNAL_TEXT });
    await expect(apiWith(async () => json(404, { detail: "x" })).api.item(5)).rejects.toMatchObject({ status: 404, code: "internal" });
  });

  it("refuses an OK answer that is not JSON", async () => {
    const { api } = apiWith(async () => new Response("not json", { status: 200 }));
    await expect(api.today()).rejects.toMatchObject({ code: "bad_response", message: BAD_RESPONSE_TEXT });
  });

  it("names a lost connection and a timeout", async () => {
    await expect(apiWith(async () => Promise.reject(new TypeError("Failed to fetch"))).api.today()).rejects.toMatchObject({
      status: 0,
      code: "network",
      message: NETWORK_TEXT,
    });
    const hanging = (_: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      });
    const api = createApi({ initData: () => "x", fetch: hanging, timeoutMs: 20 });
    await expect(api.today()).rejects.toMatchObject({ code: "timeout", message: TIMEOUT_TEXT });
  });

  it("rethrows the caller's own abort untouched", async () => {
    const hanging = (_: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      });
    const api = createApi({ initData: () => "x", fetch: hanging, timeoutMs: 5000 });
    const controller = new AbortController();
    const pending = api.today({ signal: controller.signal });
    controller.abort();
    const error = await pending.catch((e: unknown) => e);
    expect(isAbort(error)).toBe(true);
  });
});

describe("pollJob", () => {
  const status = (s: JobStatus["status"], reason: JobStatus["reason"] = null): JobStatus => ({
    id: 4,
    kind: "split",
    status: s,
    retry_at: null,
    reason,
    result: s === "done" ? { ok: true, steps: 3 } : null,
  });

  it("polls until the job is done and reports every state", async () => {
    const answers = [status("queued"), status("running"), status("done")];
    const job = vi.fn(async () => answers.shift()!);
    const seen: string[] = [];
    const final = await pollJob({ job }, 4, { intervalMs: 1, onUpdate: (s) => seen.push(s.status) });
    expect(final.status).toBe("done");
    expect(seen).toEqual(["queued", "running", "done"]);
    expect(job).toHaveBeenCalledWith(4, expect.anything());
  });

  it("stops at dead and at the timeout", async () => {
    expect((await pollJob({ job: async () => status("dead", "failed") }, 4, { intervalMs: 1 })).status).toBe("dead");
    const waiting = await pollJob({ job: async () => status("queued", "budget") }, 4, { intervalMs: 5, timeoutMs: 20 });
    expect(waiting).toMatchObject({ status: "queued", reason: "budget" });
  });
});
