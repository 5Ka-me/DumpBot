/**
 * Picture previews (`GET /api/files/{id}/{n}`): fetched with the Authorization header into blob URLs, at most three
 * at a time (the bot downloads each from Telegram), the last `MAX_CACHED` kept.
 */
import { useEffect, useRef, useState } from "react";
import { api } from "../api";

const MAX_CACHED = 60;
const PARALLEL = 3;

const cache = new Map<string, Promise<string>>();
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

function evict(): void {
  while (cache.size > MAX_CACHED) {
    const [oldest, url] = cache.entries().next().value as [string, Promise<string>];
    cache.delete(oldest);
    void url.then((u) => URL.revokeObjectURL(u)).catch(() => undefined);
  }
}

/** The blob URL of a preview path (cached; a failed load is forgotten so it can be retried). */
export function imageUrl(path: string): Promise<string> {
  const cached = cache.get(path);
  if (cached) {
    cache.delete(path); // most recently used last
    cache.set(path, cached);
    return cached;
  }
  const promise = limited(() => api.image(path)).then((blob) => URL.createObjectURL(blob));
  cache.set(path, promise);
  promise.catch(() => cache.delete(path));
  evict();
  return promise;
}

export function forgetImage(path: string): void {
  cache.delete(path);
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
