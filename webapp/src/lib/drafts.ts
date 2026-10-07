/**
 * Unsaved input that must outlive its screen: the «Добавить» text when another tab is opened (the tab's screen is
 * unmounted), and every draft — that text, a card's title / details, the settings form — when the session expires
 * (the whole app gives way to «Сессия устарела»). Kept in memory and in localStorage (per device, this origin only)
 * until saved or discarded, so the reopened app brings them back. Older than a week: forgotten.
 */
import { useCallback, useSyncExternalStore } from "react";

const PREFIX = "dump:draft:";
const MAX_AGE_MS = 7 * 86_400_000;

interface Stored {
  value: unknown;
  at: number;
}

const memory = new Map<string, unknown>();
const listeners = new Map<string, Set<() => void>>();

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null; // blocked (private mode, a sandboxed frame): memory only
  }
}

function load<T>(key: string): T | undefined {
  if (memory.has(key)) return memory.get(key) as T | undefined;
  let value: unknown;
  try {
    const raw = storage()?.getItem(PREFIX + key);
    const stored = raw ? (JSON.parse(raw) as Stored) : null;
    if (stored && Date.now() - stored.at <= MAX_AGE_MS) value = stored.value;
    else if (stored) storage()?.removeItem(PREFIX + key);
  } catch {
    value = undefined;
  }
  memory.set(key, value);
  return value as T | undefined;
}

export function getDraft<T>(key: string): T | undefined {
  return load<T>(key);
}

/** Keep `value` as the draft `key` (undefined: forget it). */
export function setDraft<T>(key: string, value: T | undefined): void {
  memory.set(key, value);
  try {
    if (value === undefined) storage()?.removeItem(PREFIX + key);
    else storage()?.setItem(PREFIX + key, JSON.stringify({ value, at: Date.now() } satisfies Stored));
  } catch {
    // storage full or blocked: the draft lives in memory only
  }
  listeners.get(key)?.forEach((listener) => listener());
}

export function clearDraft(key: string): void {
  setDraft(key, undefined);
}

function subscribe(key: string, listener: () => void): () => void {
  let set = listeners.get(key);
  if (!set) listeners.set(key, (set = new Set()));
  set.add(listener);
  return () => set.delete(listener);
}

/** The draft `key` as state that survives the component (undefined: none). */
export function useDraft<T>(key: string): [T | undefined, (value: T | undefined) => void] {
  const value = useSyncExternalStore(
    useCallback((listener: () => void) => subscribe(key, listener), [key]),
    () => load<T>(key),
  );
  const set = useCallback((next: T | undefined) => setDraft(key, next), [key]);
  return [value, set];
}

/** Some unsaved input is kept (the "session expired" screen says it will come back). */
export function hasDrafts(): boolean {
  if ([...memory.values()].some((v) => v !== undefined)) return true;
  try {
    const store = storage();
    if (!store) return false;
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i);
      if (key?.startsWith(PREFIX) && load(key.slice(PREFIX.length)) !== undefined) return true;
    }
  } catch {
    return false;
  }
  return false;
}

/** Test helper: forget every draft. */
export function resetDrafts(): void {
  memory.clear();
  try {
    const store = storage();
    if (!store) return;
    const keys: string[] = [];
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i);
      if (key?.startsWith(PREFIX)) keys.push(key);
    }
    keys.forEach((key) => store.removeItem(key));
  } catch {
    // nothing stored
  }
}
