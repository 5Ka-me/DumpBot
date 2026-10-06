/** Short messages over the app ("✅ Готово", errors); shown by components/Toaster. */
import { useSyncExternalStore } from "react";
import { errorText, isAbort } from "../api/client";
import { haptic } from "../telegram";

export type Tone = "info" | "success" | "error";

export interface Toast {
  id: number;
  text: string;
  tone: Tone;
}

let toasts: Toast[] = [];
let nextId = 1;
const listeners = new Set<() => void>();
const SHOW_MS = 2800;
const MAX = 3;

function emit() {
  listeners.forEach((l) => l());
}

export function dismissToast(id: number): void {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

export function toast(text: string | null | undefined, tone: Tone = "info"): void {
  if (!text) return;
  const item = { id: nextId++, text, tone };
  toasts = [...toasts, item].slice(-MAX);
  emit();
  setTimeout(() => dismissToast(item.id), tone === "error" ? SHOW_MS * 1.6 : SHOW_MS);
}

/** An error as a toast with the error haptic (aborted requests are silent). */
export function toastError(error: unknown): void {
  if (isAbort(error)) return;
  haptic.error();
  toast(errorText(error), "error");
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => toasts,
  );
}

/** Test helper. */
export function resetToasts(): void {
  toasts = [];
  emit();
}
