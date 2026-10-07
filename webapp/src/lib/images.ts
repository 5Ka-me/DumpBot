/**
 * Picture previews (`GET /api/files/{id}/{n}`, list thumbnails with `?size=thumb`): fetched with the Authorization
 * header into blob URLs, at most three at a time (the bot downloads each from Telegram). The cache keeps the most
 * recently used — at most `MAX_CACHED` pictures and `MAX_BYTES` together (a picture sent as a file may weigh
 * 20 MB) — and frees the blob of everything it drops. The browser's HTTP cache answers repeats across launches.
 */
import { useEffect, useRef, useState } from "react";
import { api } from "../api";

export const MAX_CACHED = 60;
export const MAX_BYTES = 24 * 1024 * 1024;
const PARALLEL = 3;

interface Entry {
  url: Promise<string>;
  /** Size of the blob once loaded (0 before). */
  bytes: number;
}

const cache = new Map<string, Entry>();
let cachedBytes = 0;
let running = 0;
const waiting: (() => void)[] = [];

async function limited<T>(fn: () => Promise<T>): Promise<T> {
  if (running >= PARALLEL) await new Promise<void>((resolve) => waiting.push(resolve));
  running++;
  try {
    return await fn();
  } finally {
    running--;
    waiting.shift()?.();
  }
}

/** Forget `path` and free its blob URL. */
function drop(path: string): void {
  const entry = cache.get(path);
  if (!entry) return;
  cache.delete(path);
  cachedBytes -= entry.bytes;
  void entry.url.then((u) => URL.revokeObjectURL(u)).catch(() => undefined);
}

/** The oldest pictures out until both limits hold (`keep`: the one just loaded stays even when it alone is over). */
function evict(keep?: string): void {
  for (const path of [...cache.keys()]) {
    if (cache.size <= MAX_CACHED && cachedBytes <= MAX_BYTES) return;
    if (path !== keep) drop(path);
  }
}

/** The blob URL of a preview path (cached; a failed load is forgotten so it can be retried). */
export function imageUrl(path: string): Promise<string> {
  const cached = cache.get(path);
  if (cached) {
    cache.delete(path); // most recently used last
    cache.set(path, cached);
    return cached.url;
  }
  const entry: Entry = { url: Promise.resolve(""), bytes: 0 };
  entry.url = limited(() => api.image(path)).then((blob) => {
    if (cache.get(path) === entry) {
      entry.bytes = blob.size;
      cachedBytes += blob.size;
    }
    return URL.createObjectURL(blob);
  });
  cache.set(path, entry);
  entry.url.then(
    () => evict(path),
    () => cache.get(path) === entry && cache.delete(path),
  );
  evict(path);
  return entry.url;
}

/** Drop a preview (a broken image is loaded again), freeing its blob. */
export function forgetImage(path: string): void {
  drop(path);
}

/** Test helper: the cache's size. */
export function imageCacheSize(): { count: number; bytes: number } {
  return { count: cache.size, bytes: cachedBytes };
}

/** Test helper: forget every picture. */
export function resetImages(): void {
  for (const path of [...cache.keys()]) drop(path);
  cachedBytes = 0;
}

export interface ImageState {
  url?: string;
  failed: boolean;
}

/** The blob URL of `path`, loaded once `visible` (lazy thumbnails); `retry` after a broken image. */
export function useImage(path: string | null, visible = true): ImageState & { retry: () => void } {
  const [state, setState] = useState<ImageState & { path?: string }>({ failed: false });
  const [attempt, setAttempt] = useState(0);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    if (!path || !visible) return;
    imageUrl(path).then(
      (url) => alive.current && setState({ url, failed: false, path }),
      () => alive.current && setState({ failed: true, path }),
    );
  }, [path, visible, attempt]);
  const current = state.path === path ? state : { failed: false };
  return {
    ...current,
    retry: () => {
      if (!path) return;
      forgetImage(path);
      if (attempt >= 1) setState({ failed: true, path });
      else setAttempt((a) => a + 1);
    },
  };
}
