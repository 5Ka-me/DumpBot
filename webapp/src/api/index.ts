/** The app's API instance: Telegram's initData on every request, 401 / 403 reported to the auth listener. */
import { initData } from "../telegram";
import { createApi, type ApiError } from "./client";

let authListener: ((error: ApiError) => void) | null = null;

/** Called on 401 / 403 (the app shows "open it again"). */
export function onAuthError(listener: ((error: ApiError) => void) | null): void {
  authListener = listener;
}

export const api = createApi({ initData, onAuthError: (error) => authListener?.(error) });

export { ApiError, errorText, isAbort, pollJob } from "./client";
export type * from "./types";
