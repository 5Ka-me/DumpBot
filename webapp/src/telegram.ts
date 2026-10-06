/**
 * Telegram Mini App integration (telegram-web-app.js, core.telegram.org/bots/webapps): the typed part of
 * `window.Telegram.WebApp` the app uses, start-up (ready / expand / theme), haptics, dialogs and links.
 * Every helper is a no-op (or a browser fallback) outside Telegram, so the app also runs in a plain browser in dev.
 */

export interface ThemeParams {
  bg_color?: string;
  text_color?: string;
  hint_color?: string;
  link_color?: string;
  button_color?: string;
  button_text_color?: string;
  secondary_bg_color?: string;
  header_bg_color?: string;
  bottom_bar_bg_color?: string;
  accent_text_color?: string;
  section_bg_color?: string;
  section_header_text_color?: string;
  section_separator_color?: string;
  subtitle_text_color?: string;
  destructive_text_color?: string;
}

export interface BottomButton {
  setParams(params: { text?: string; is_active?: boolean; is_visible?: boolean }): BottomButton;
  onClick(callback: () => void): BottomButton;
  offClick(callback: () => void): BottomButton;
  showProgress(leaveActive?: boolean): BottomButton;
  hideProgress(): BottomButton;
}

export interface BackButtonApi {
  onClick(callback: () => void): BackButtonApi;
  offClick(callback: () => void): BackButtonApi;
  show(): BackButtonApi;
  hide(): BackButtonApi;
}

export interface HapticFeedback {
  impactOccurred(style: "light" | "medium" | "heavy" | "rigid" | "soft"): HapticFeedback;
  notificationOccurred(type: "error" | "success" | "warning"): HapticFeedback;
  selectionChanged(): HapticFeedback;
}

export interface WebApp {
  initData: string;
  version: string;
  platform: string;
  colorScheme: "light" | "dark";
  themeParams: ThemeParams;
  isVersionAtLeast(version: string): boolean;
  ready(): void;
  expand(): void;
  close(): void;
  onEvent(event: string, callback: () => void): void;
  offEvent(event: string, callback: () => void): void;
  setHeaderColor?(color: string): void;
  setBackgroundColor?(color: string): void;
  enableClosingConfirmation?(): void;
  disableClosingConfirmation?(): void;
  disableVerticalSwipes?(): void;
  showConfirm?(message: string, callback: (ok: boolean) => void): void;
  openLink?(url: string): void;
  openTelegramLink?(url: string): void;
  BackButton: BackButtonApi;
  MainButton: BottomButton;
  HapticFeedback: HapticFeedback;
}

declare global {
  interface Window {
    Telegram?: { WebApp?: WebApp };
  }
}

/** The WebApp object, when telegram-web-app.js has loaded. */
export function webApp(): WebApp | undefined {
  return typeof window === "undefined" ? undefined : window.Telegram?.WebApp;
}

/** initData from the launch URL (`#tgWebAppData=…`): used when telegram-web-app.js failed to load. */
function hashInitData(): string {
  if (typeof location === "undefined") return "";
  try {
    return new URLSearchParams(location.hash.slice(1)).get("tgWebAppData") ?? "";
  } catch {
    return "";
  }
}

/** Opened from Telegram (initData is present); outside Telegram the script loads but initData is empty. */
export function insideTelegram(): boolean {
  return Boolean(webApp()?.initData || hashInitData());
}

/** Telegram supports this Bot API version of Mini Apps (false outside Telegram). */
export function supports(version: string): boolean {
  const app = webApp();
  return Boolean(app?.initData) && app!.isVersionAtLeast(version);
}

/** The value of `Authorization: tma …`: Telegram's initData; in dev outside Telegram a stand-in for the mock API. */
export function initData(): string {
  const real = webApp()?.initData || hashInitData();
  if (real) return real;
  return import.meta.env.DEV ? import.meta.env.VITE_DEV_INIT_DATA || "dev" : "";
}

/** Light / dark from Telegram's colorScheme (the CSS reads `--tg-theme-*` set by telegram-web-app.js). */
export function applyTheme(): void {
  const app = webApp();
  const root = document.documentElement;
  if (app && insideTelegram()) {
    root.dataset.tg = "1";
    root.dataset.theme = app.colorScheme;
    root.style.colorScheme = app.colorScheme;
  } else {
    delete root.dataset.tg;
    delete root.dataset.theme;
  }
}

/** Start-up inside Telegram: ready, full height, theme (and its changes), no swipe-to-close while scrolling. */
export function setupTelegram(): void {
  applyTheme();
  const app = webApp();
  if (!app || !insideTelegram()) return;
  app.onEvent("themeChanged", applyTheme);
  if (supports("6.1")) {
    app.setHeaderColor?.("secondary_bg_color");
    app.setBackgroundColor?.("secondary_bg_color");
  }
  if (supports("7.7")) app.disableVerticalSwipes?.();
  app.ready();
  app.expand();
}

/** Haptic feedback on taps and results (Bot API 6.1+). */
export const haptic = {
  tap: () => withHaptics((h) => h.impactOccurred("light")),
  select: () => withHaptics((h) => h.selectionChanged()),
  success: () => withHaptics((h) => h.notificationOccurred("success")),
  warning: () => withHaptics((h) => h.notificationOccurred("warning")),
  error: () => withHaptics((h) => h.notificationOccurred("error")),
};

function withHaptics(fn: (h: HapticFeedback) => void): void {
  const app = webApp();
  if (!app || !supports("6.1")) return;
  try {
    fn(app.HapticFeedback);
  } catch {
    // haptics are best effort
  }
}

/** A yes / no question: Telegram's popup (6.2+), else the browser's confirm. */
export function confirmDialog(message: string): Promise<boolean> {
  const app = webApp();
  if (app?.showConfirm && supports("6.2")) {
    return new Promise((resolve) => app.showConfirm!(message, (ok) => resolve(ok)));
  }
  return Promise.resolve(window.confirm(message));
}

/** Open a link outside the app: t.me links in Telegram itself, others in the browser. */
export function openExternal(url: string): void {
  if (!/^https?:\/\//i.test(url)) return;
  const app = webApp();
  if (app && supports("6.1")) {
    if (/^https:\/\/t\.me\//i.test(url) && app.openTelegramLink) {
      app.openTelegramLink(url);
      return;
    }
    if (app.openLink) {
      app.openLink(url);
      return;
    }
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

let closingConfirmation = false;

/** Ask before closing the app (unsent text); Bot API 6.2+. */
export function setClosingConfirmation(on: boolean): void {
  const app = webApp();
  if (!app || !supports("6.2") || on === closingConfirmation) return;
  closingConfirmation = on;
  if (on) app.enableClosingConfirmation?.();
  else app.disableClosingConfirmation?.();
}

/** Close the Mini App (outside Telegram: nothing to close). */
export function closeApp(): void {
  webApp()?.close();
}
