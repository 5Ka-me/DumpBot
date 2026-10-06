/**
 * «Закинуть в свалку» from the app: sent texts and how far the bot got with each (POST /api/inbox, then polling
 * GET /api/inbox/{raw_id} while `pending`). Lives outside the screens, so polling goes on when the tab changes;
 * the last raw ids are kept in sessionStorage.
 */
import { useSyncExternalStore } from "react";
import { api, isAbort } from "../api";
import { errorText, sleep } from "../api/client";
import type { InboxStatus } from "../api/types";
import { haptic } from "../telegram";
import { jobStuck } from "./jobs";
import { itemsChanged } from "./store";

export interface InboxEntry {
  rawId: number;
  status: InboxStatus;
  /** Polling now. */
  polling: boolean;
  /** Polling stopped while still pending (waits for the AI budget…): offer a refresh. */
  paused: boolean;
  error: string | null;
}

const KEEP = 10;
const STORAGE_KEY = "dumpbot.inbox";
const FAST_MS = 1200;
const SLOW_MS = 4000;
const FAST_FOR_MS = 20_000;
const GIVE_UP_MS = 180_000;

let entries: InboxEntry[] = [];
const listeners = new Set<() => void>();
const pollers = new Map<number, AbortController>();

function emit() {
  listeners.forEach((l) => l());
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(entries.map((e) => e.rawId)));
  } catch {
    // private mode: not remembered
  }
}

function patch(rawId: number, change: Partial<InboxEntry>) {
  entries = entries.map((e) => (e.rawId === rawId ? { ...e, ...change } : e));
  emit();
}

function upsert(status: InboxStatus) {
  const existing = entries.find((e) => e.rawId === status.raw_id);
  if (existing) patch(status.raw_id, { status, error: null });
  else {
    entries = [{ rawId: status.raw_id, status, polling: false, paused: false, error: null }, ...entries].slice(0, KEEP);
    emit();
  }
}

/** Poll one message until it is processed (fast first, then slower, up to a few minutes). */
async function follow(rawId: number): Promise<void> {
  pollers.get(rawId)?.abort();
  const ctrl = new AbortController();
  pollers.set(rawId, ctrl);
  patch(rawId, { polling: true, paused: false, error: null });
  const started = Date.now();
  let status = entries.find((e) => e.rawId === rawId)?.status;
  try {
    while (status?.pending) {
      if (jobStuck(status.job) || Date.now() - started > GIVE_UP_MS) {
        patch(rawId, { polling: false, paused: true });
        return;
      }
      await sleep(Date.now() - started < FAST_FOR_MS ? FAST_MS : SLOW_MS, ctrl.signal);
      status = await api.inboxStatus(rawId, { signal: ctrl.signal });
      upsert(status);
    }
    patch(rawId, { polling: false });
    if (status) {
      itemsChanged();
      if (status.status === "failed") haptic.error();
      else if (status.ask || status.candidates) haptic.warning();
      else haptic.success();
    }
  } catch (error) {
    if (isAbort(error)) return;
    patch(rawId, { polling: false, paused: true, error: errorText(error) });
  } finally {
    if (pollers.get(rawId) === ctrl) pollers.delete(rawId);
  }
}

/** Send a text to the dump; the entry appears at once and is followed. Throws ApiError. */
export async function sendToInbox(text: string): Promise<InboxStatus> {
  const status = await api.inbox(text);
  upsert(status);
  void follow(status.raw_id);
  return status;
}

export async function answerQuestion(rawId: number, option: number): Promise<void> {
  upsert(await api.inboxAnswer(rawId, option));
  void follow(rawId);
}

export async function pickCandidate(rawId: number, itemId: number): Promise<void> {
  upsert(await api.inboxPick(rawId, itemId));
  void follow(rawId);
}

/** Ask again now (after a pause or an error). */
export async function refreshEntry(rawId: number): Promise<void> {
  try {
    upsert(await api.inboxStatus(rawId));
    void follow(rawId);
  } catch (error) {
    patch(rawId, { error: errorText(error) });
  }
}

export function forgetEntry(rawId: number): void {
  pollers.get(rawId)?.abort();
  entries = entries.filter((e) => e.rawId !== rawId);
  emit();
}

/** The entries of an earlier visit (this Telegram session): loaded again. */
export async function restoreInbox(): Promise<void> {
  let ids: number[];
  try {
    ids = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "[]") as number[];
  } catch {
    ids = [];
  }
  if (entries.length || !Array.isArray(ids)) return;
  const loaded = await Promise.all(
    ids.filter((id) => Number.isInteger(id)).map((id) => api.inboxStatus(id).catch(() => null)),
  );
  for (const status of loaded.reverse()) if (status) upsert(status);
  entries.filter((e) => e.status.pending).forEach((e) => void follow(e.rawId));
}

export function useInbox(): InboxEntry[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => entries,
  );
}

/** Test helper. */
export function resetInbox(): void {
  pollers.forEach((p) => p.abort());
  pollers.clear();
  entries = [];
}
