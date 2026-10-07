/**
 * Client of the Mini App API (docs/features/miniapp-api.md): one typed method per endpoint.
 *
 * Every request carries `Authorization: tma <initData>`. Errors become `ApiError` with the server's `code`,
 * Russian `message` and `field`; a lost connection, a timeout or a non-JSON answer (a proxy's error page) get codes
 * of their own. 401 / 403 are also reported to `onAuthError` (the app then shows the "open it again" screen).
 */
import type {
  Agenda,
  AskResponse,
  ErrorCode,
  FeedPage,
  FeedQuery,
  InboxStatus,
  ItemAction,
  ItemDetail,
  ItemPatch,
  ItemResponse,
  JobAccepted,
  JobStatus,
  Me,
  MoveRequest,
  MoveResult,
  NewStep,
  OccurrenceRequest,
  RowsResponse,
  SettingsPatch,
  SettingsResponse,
  SnoozeRequest,
  StepPatch,
  StepResponse,
  Usage,
  WhenRequest,
  Week,
} from "./types";

export type ClientErrorCode = ErrorCode | "network" | "timeout" | "bad_response" | "unavailable";

export const NETWORK_TEXT = "Нет связи с ботом — проверь интернет и попробуй ещё раз.";
export const TIMEOUT_TEXT = "Бот не ответил вовремя — попробуй ещё раз.";
export const UNAVAILABLE_TEXT = "Бот сейчас недоступен — возможно, компьютер с ним выключен. Попробуй позже.";
export const BAD_RESPONSE_TEXT = "Непонятный ответ сервера — попробуй ещё раз.";
export const INTERNAL_TEXT = "😢 Что-то пошло не так — попробуй ещё раз.";

export class ApiError extends Error {
  readonly status: number;
  readonly code: ClientErrorCode;
  readonly field: string | undefined;

  constructor(status: number, code: ClientErrorCode, message: string, field?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.field = field;
  }

  /** Signed out: the session expired, initData is missing or the user is not the owner. */
  get isAuth(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

export interface ApiOptions {
  /** Telegram's initData, read on every request. */
  initData: () => string;
  fetch?: typeof fetch;
  /** Prefix of every path ("" — the same origin). */
  base?: string;
  /** Default timeout of a request, ms. */
  timeoutMs?: number;
  onAuthError?: (error: ApiError) => void;
}

export interface CallOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
  /** The browser's HTTP cache (default "no-store": API answers are never reused; pictures may be). */
  cache?: RequestCache;
}

const DEFAULT_TIMEOUT_MS = 20_000;
export const AI_TIMEOUT_MS = 120_000;

/** Query string of GET /api/feed: lists comma-separated, empty values and the default status left out. */
export function feedParams(query: FeedQuery): string {
  const params = new URLSearchParams();
  const q = query.q?.trim();
  if (q) params.set("q", q);
  if (query.kinds?.length) params.set("kinds", query.kinds.join(","));
  if (query.tags?.length) params.set("tags", query.tags.join(","));
  if (query.status && query.status !== "any") params.set("status", query.status);
  if (query.from) params.set("from", query.from);
  if (query.to) params.set("to", query.to);
  if (query.cursor) params.set("cursor", query.cursor);
  if (query.limit) params.set("limit", String(query.limit));
  const text = params.toString();
  return text ? `?${text}` : "";
}

function isErrorBody(data: unknown): data is { error: { code: ErrorCode; message: string; field?: string } } {
  if (typeof data !== "object" || data === null || !("error" in data)) return false;
  const error = (data as { error: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as { code?: unknown }).code === "string" &&
    typeof (error as { message?: unknown }).message === "string"
  );
}

/** The ApiError of a non-2xx answer: the JSON error body, else by status (a proxy's HTML page, an empty body). */
export async function errorOf(response: Response): Promise<ApiError> {
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (isErrorBody(data)) {
    const { code, message, field } = data.error;
    return new ApiError(response.status, code, message, field);
  }
  if ([502, 503, 504].includes(response.status)) {
    return new ApiError(response.status, "unavailable", UNAVAILABLE_TEXT);
  }
  return new ApiError(response.status, "internal", INTERNAL_TEXT);
}

/** An AbortSignal that fires on the caller's signal or after `ms`; `timedOut()` tells which. */
function deadline(ms: number, outer?: AbortSignal) {
  const controller = new AbortController();
  let expired = false;
  const timer = setTimeout(() => {
    expired = true;
    controller.abort();
  }, ms);
  const onAbort = () => controller.abort();
  if (outer?.aborted) controller.abort();
  outer?.addEventListener("abort", onAbort, { once: true });
  return {
    signal: controller.signal,
    timedOut: () => expired,
    done: () => {
      clearTimeout(timer);
      outer?.removeEventListener("abort", onAbort);
    },
  };
}

export function createApi(options: ApiOptions) {
  const doFetch = options.fetch ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));
  const base = options.base ?? "";
  const defaultTimeout = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  async function send(method: string, path: string, body: unknown, call: CallOptions = {}): Promise<Response> {
    const headers: Record<string, string> = {
      Authorization: `tma ${options.initData()}`,
      Accept: "application/json",
    };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const limit = deadline(call.timeoutMs ?? defaultTimeout, call.signal);
    let response: Response;
    try {
      response = await doFetch(base + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: limit.signal,
        credentials: "same-origin",
        cache: call.cache ?? "no-store",
      });
    } catch (e) {
      if (limit.timedOut()) throw new ApiError(0, "timeout", TIMEOUT_TEXT);
      if (call.signal?.aborted) throw e; // the caller gave up: rethrow the AbortError as is
      throw new ApiError(0, "network", NETWORK_TEXT);
    } finally {
      limit.done();
    }
    if (!response.ok) {
      const error = await errorOf(response);
      if (error.isAuth) options.onAuthError?.(error);
      throw error;
    }
    return response;
  }

  async function request<T>(method: string, path: string, body?: unknown, call?: CallOptions): Promise<T> {
    const response = await send(method, path, body, call);
    if (response.status === 204) return undefined as T;
    try {
      return (await response.json()) as T;
    } catch {
      throw new ApiError(response.status, "bad_response", BAD_RESPONSE_TEXT);
    }
  }

  const get = <T>(path: string, call?: CallOptions) => request<T>("GET", path, undefined, call);
  const post = <T>(path: string, body: unknown = {}, call?: CallOptions) => request<T>("POST", path, body, call);
  const patch = <T>(path: string, body: unknown, call?: CallOptions) => request<T>("PATCH", path, body, call);

  return {
    me: (call?: CallOptions) => get<Me>("/api/me", call),
    feed: (query: FeedQuery, call?: CallOptions) => get<FeedPage>(`/api/feed${feedParams(query)}`, call),
    today: (call?: CallOptions) => get<Agenda>("/api/today", call),
    week: (call?: CallOptions) => get<Week>("/api/week", call),
    tasks: (call?: CallOptions) => get<RowsResponse>("/api/tasks", call),
    someday: (call?: CallOptions) => get<RowsResponse>("/api/someday", call),
    recurring: (call?: CallOptions) => get<RowsResponse>("/api/recurring", call),

    item: (id: number, call?: CallOptions) => get<ItemDetail>(`/api/items/${id}`, call),
    patchItem: (id: number, body: ItemPatch) => patch<ItemResponse>(`/api/items/${id}`, body),
    /** "🕒 Время" by text: an AI job (poll it). */
    whenText: (id: number, text: string) => post<JobAccepted>(`/api/items/${id}/when`, { text } satisfies WhenRequest),
    /** The time from the picker ("YYYY-MM-DD" or "YYYY-MM-DDTHH:MM", local), no AI. */
    whenAt: (id: number, dueLocal: string) =>
      post<ItemResponse>(`/api/items/${id}/when`, { due_local: dueLocal } satisfies WhenRequest),
    action: (id: number, action: Exclude<ItemAction, "snooze">) => post<ItemResponse>(`/api/items/${id}/${action}`),
    /** ☐ / ☑ of a list row: done / not done of exactly its occurrence (a series' missed morning, Thursday's row). */
    occurrenceAction: (id: number, action: "done" | "undone", occurrenceId: number) =>
      post<ItemResponse>(`/api/items/${id}/${action}`, { occurrence_id: occurrenceId } satisfies OccurrenceRequest),
    /** «📅 Перенести невыполненное на завтра» of `day` (default: today). */
    moveToTomorrow: (day?: string) => post<MoveResult>("/api/today/move", (day ? { day } : {}) satisfies MoveRequest),
    snooze: (id: number, body: SnoozeRequest) => post<ItemResponse>(`/api/items/${id}/snooze`, body),
    split: (id: number) => post<JobAccepted>(`/api/items/${id}/split`),
    addStep: (id: number, body: NewStep) => post<StepResponse>(`/api/items/${id}/steps`, body),
    patchStep: (id: number, body: StepPatch) => patch<StepResponse>(`/api/steps/${id}`, body),
    deleteStep: (id: number) => request<StepResponse>("DELETE", `/api/steps/${id}`),
    toggleStep: (id: number, done?: boolean) =>
      post<StepResponse>(`/api/steps/${id}/toggle`, done === undefined ? {} : { done }),

    inbox: (text: string) => post<InboxStatus>("/api/inbox", { text }),
    inboxStatus: (rawId: number, call?: CallOptions) => get<InboxStatus>(`/api/inbox/${rawId}`, call),
    inboxAnswer: (rawId: number, option: number) => post<InboxStatus>(`/api/inbox/${rawId}/answer`, { option }),
    inboxPick: (rawId: number, itemId: number) => post<InboxStatus>(`/api/inbox/${rawId}/pick`, { item_id: itemId }),

    ask: (question: string, call?: CallOptions) =>
      post<AskResponse>("/api/ask", { question }, { timeoutMs: AI_TIMEOUT_MS, ...call }),
    job: (id: number, call?: CallOptions) => get<JobStatus>(`/api/jobs/${id}`, call),

    settings: (call?: CallOptions) => get<SettingsResponse>("/api/settings", call),
    patchSettings: (body: SettingsPatch) => patch<SettingsResponse>("/api/settings", body),
    usage: (call?: CallOptions) => get<Usage>("/api/usage", call),

    /**
     * A picture preview (`ImageRef.url`, `/api/files/{id}/{n}[?size=thumb]`) as a Blob, with the same authorization.
     * The browser's cache may answer it: the server allows a day (`private, max-age=86400`).
     */
    image: async (path: string, call?: CallOptions): Promise<Blob> =>
      (await send("GET", path, undefined, { cache: "default", ...call })).blob(),
  };
}

export type Api = ReturnType<typeof createApi>;

/** Resolves after `ms`, or rejects with the signal's reason when it aborts first. */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason ?? new DOMException("Aborted", "AbortError"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export interface PollOptions {
  intervalMs?: number;
  /** Give up after this long and return the last status (still queued: e.g. waiting for the AI budget). */
  timeoutMs?: number;
  signal?: AbortSignal;
  onUpdate?: (status: JobStatus) => void;
}

/** Poll GET /api/jobs/{id} until it is done or dead (or the timeout); the last status. */
export async function pollJob(api: Pick<Api, "job">, jobId: number, options: PollOptions = {}): Promise<JobStatus> {
  const interval = options.intervalMs ?? 1500;
  const until = Date.now() + (options.timeoutMs ?? 90_000);
  for (;;) {
    const status = await api.job(jobId, { signal: options.signal });
    options.onUpdate?.(status);
    if (status.status === "done" || status.status === "dead" || Date.now() >= until) return status;
    await sleep(interval, options.signal);
  }
}

/** The caller aborted (a screen closed): such errors are not shown. */
export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/** A user-facing text for any error. */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return INTERNAL_TEXT;
}
