/**
 * A tiny stale-while-revalidate cache for API reads: `useResource(key, load)` shows the cached data at once and
 * refreshes it when the screen is active and the data is old or marked stale. After a change, `markStale("today",
 * …)` refreshes visible screens now and hidden ones (under the card) when they come back.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useSyncExternalStore } from "react";

export interface Snapshot<T> {
  data?: T;
  error?: unknown;
  loading: boolean;
  stale: boolean;
  fetchedAt: number;
  /** Bumped by `setCached`: a fetch that started before it is dropped. */
  version: number;
}

/** Data younger than this is not refetched on mount. */
const FRESH_MS = 3000;
const EMPTY: Snapshot<never> = { loading: false, stale: true, fetchedAt: 0, version: 0 };

const entries = new Map<string, Snapshot<unknown>>();
const listeners = new Map<string, Set<() => void>>();
const inflight = new Map<string, Promise<void>>();
const staleListeners = new Set<(prefixes: string[]) => void>();

/** The screen is on top (hidden screens under a card stay mounted but do not fetch). */
export const ScreenActive = createContext(true);

function emit(key: string): void {
  listeners.get(key)?.forEach((listener) => listener());
}

function update<T>(key: string, patch: Partial<Snapshot<T>>): void {
  entries.set(key, { ...(entries.get(key) ?? EMPTY), ...patch });
  emit(key);
}

function subscribe(key: string, listener: () => void): () => void {
  let set = listeners.get(key);
  if (!set) listeners.set(key, (set = new Set()));
  set.add(listener);
  return () => set.delete(listener);
}

export function getCached<T>(key: string): T | undefined {
  return entries.get(key)?.data as T | undefined;
}

/** Put data into the cache (a change's answer, an optimistic update); fresh, no error. */
export function setCached<T>(key: string, value: T | ((prev: T | undefined) => T | undefined)): void {
  const prev = entries.get(key) as Snapshot<T> | undefined;
  const data = typeof value === "function" ? (value as (p: T | undefined) => T | undefined)(prev?.data) : value;
  update<T>(key, {
    data,
    error: undefined,
    stale: false,
    fetchedAt: Date.now(),
    version: (prev?.version ?? 0) + 1,
  });
}

/** Mark every cached key starting with one of `prefixes` stale ("" — all). */
export function markStale(...prefixes: string[]): void {
  for (const key of [...entries.keys()]) {
    if (prefixes.some((p) => key.startsWith(p))) update(key, { stale: true });
  }
  staleListeners.forEach((listener) => listener(prefixes));
}

/** Fetch `key` now (one request at a time per key). */
export function revalidate<T>(key: string, load: (signal: AbortSignal) => Promise<T>): Promise<void> {
  const running = inflight.get(key);
  if (running) return running;
  const startVersion = entries.get(key)?.version ?? 0;
  update(key, { loading: true });
  const promise = (async () => {
    try {
      const data = await load(new AbortController().signal);
      if ((entries.get(key)?.version ?? 0) !== startVersion) {
        update(key, { loading: false, stale: true }); // changed meanwhile: this answer may be older
        return;
      }
      update(key, { data, error: undefined, loading: false, stale: false, fetchedAt: Date.now() });
    } catch (error) {
      update(key, { error, loading: false, stale: false, fetchedAt: Date.now() });
    } finally {
      inflight.delete(key);
    }
  })();
  inflight.set(key, promise);
  return promise;
}

export interface Resource<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => Promise<void>;
}

/** Cached data of `key`, loaded by `load` (fresh on mount when older than a few seconds, and when stale). */
export function useResource<T>(key: string, load: (signal: AbortSignal) => Promise<T>, enabled = true): Resource<T> {
  const active = useContext(ScreenActive);
  const snap = useSyncExternalStore(
    useCallback((listener: () => void) => subscribe(key, listener), [key]),
    () => (entries.get(key) ?? EMPTY) as Snapshot<T>,
  );
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });
  const reload = useCallback(() => revalidate(key, (signal) => loadRef.current(signal)), [key]);
  const { stale, fetchedAt } = snap;
  useEffect(() => {
    if (!enabled || !active) return;
    if (stale || Date.now() - fetchedAt > FRESH_MS) void reload();
    // fetchedAt is read once per activation: a fresh answer must not trigger another fetch
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, active, enabled, stale, reload]);
  return { data: snap.data, error: snap.error, loading: snap.loading, reload };
}

/** Calls `onStale` when one of `prefixes` is marked stale: at once if the screen is active, else when it becomes. */
export function useStaleSignal(prefixes: string[], onStale: () => void): void {
  const active = useContext(ScreenActive);
  const pending = useRef(false);
  const callback = useRef(onStale);
  const activeRef = useRef(active);
  useEffect(() => {
    callback.current = onStale;
    activeRef.current = active;
  });
  const key = prefixes.join("|");
  useEffect(() => {
    const own = key.split("|");
    const listener = (marked: string[]) => {
      if (!marked.some((p) => own.some((o) => o.startsWith(p) || p.startsWith(o)))) return;
      if (activeRef.current) callback.current();
      else pending.current = true;
    };
    staleListeners.add(listener);
    return () => {
      staleListeners.delete(listener);
    };
  }, [key]);
  useEffect(() => {
    if (active && pending.current) {
      pending.current = false;
      callback.current();
    }
  }, [active]);
}

/** Every list that shows rows (refreshed after any change of an item). */
export const LIST_KEYS = ["today", "week", "tasks", "someday", "recurring", "feed"] as const;

/** After a change of an item: lists and other cards are refreshed when shown. */
export function itemsChanged(): void {
  markStale(...LIST_KEYS, "item:");
}

/** Test helper: forget everything. */
export function resetStore(): void {
  entries.clear();
  inflight.clear();
  listeners.clear();
  staleListeners.clear();
}
