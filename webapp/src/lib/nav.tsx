/**
 * Navigation: five tabs, each a stack of screens (a card on top of a list…). Screens under the top one stay mounted
 * (their scroll and inputs survive). Telegram's BackButton (or the fallback "‹ Назад" outside Telegram) closes the
 * topmost sheet / viewer first (`useBackHandler`), then pops the stack.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import type { FeedStatus } from "../api/types";
import { haptic, supports, webApp } from "../telegram";
import { ScreenActive } from "./store";

export type Tab = "today" | "feed" | "add" | "tasks" | "more";

export type Route =
  | { name: "today" }
  | { name: "feed"; status?: FeedStatus }
  | { name: "add" }
  | { name: "tasks" }
  | { name: "more" }
  | { name: "item"; id: number }
  | { name: "ask" }
  | { name: "settings" }
  | { name: "usage" };

export interface Entry {
  key: number;
  route: Route;
}

interface State {
  tab: Tab;
  stack: Entry[];
}

type Action =
  | { type: "push"; route: Route }
  | { type: "pop" }
  | { type: "tab"; tab: Tab; route?: Route };

let nextKey = 1;

function rootOf(tab: Tab): Route {
  return { name: tab };
}

export function navReducer(state: State, action: Action): State {
  switch (action.type) {
    case "push":
      return { ...state, stack: [...state.stack, { key: nextKey++, route: action.route }] };
    case "pop":
      return state.stack.length > 1 ? { ...state, stack: state.stack.slice(0, -1) } : state;
    case "tab": {
      const root: Entry = { key: nextKey++, route: rootOf(action.tab) };
      const stack = action.route ? [root, { key: nextKey++, route: action.route }] : [root];
      return { tab: action.tab, stack };
    }
  }
}

export function initialNav(tab: Tab = "today"): State {
  return { tab, stack: [{ key: nextKey++, route: rootOf(tab) }] };
}

interface Nav {
  tab: Tab;
  stack: Entry[];
  push: (route: Route) => void;
  /** Back: the topmost handler (a sheet…), else the previous screen. */
  back: () => void;
  /** The previous screen, ignoring handlers (after "leave without saving?"). */
  pop: () => void;
  switchTab: (tab: Tab, route?: Route) => void;
  canGoBack: boolean;
}

const NavContext = createContext<Nav | null>(null);

export function useNav(): Nav {
  const nav = useContext(NavContext);
  if (!nav) throw new Error("useNav outside NavProvider");
  return nav;
}

// --- back handlers (sheets, the picture viewer, unsaved edits) ----------------------------------------------------

interface Handler {
  id: number;
  fn: () => void;
}

let handlers: Handler[] = [];
const handlerListeners = new Set<() => void>();
let handlerId = 1;

function emitHandlers() {
  handlerListeners.forEach((l) => l());
}

/** While `enabled` (and the screen is on top), Back calls `fn` instead of leaving the screen (the latest wins). */
export function useBackHandler(enabled: boolean, fn: () => void): void {
  const active = useContext(ScreenActive);
  const on = enabled && active;
  const ref = useRef(fn);
  useEffect(() => {
    ref.current = fn;
  });
  useEffect(() => {
    if (!on) return;
    const handler = { id: handlerId++, fn: () => ref.current() };
    handlers = [...handlers, handler];
    emitHandlers();
    return () => {
      handlers = handlers.filter((h) => h.id !== handler.id);
      emitHandlers();
    };
  }, [on]);
}

function useHandlerCount(): number {
  return useSyncExternalStore(
    (l) => {
      handlerListeners.add(l);
      return () => handlerListeners.delete(l);
    },
    () => handlers.length,
  );
}

/** Telegram draws its own Back button (6.1+); otherwise the app shows one. */
export function nativeBackButton(): boolean {
  return supports("6.1");
}

export function NavProvider({ children, initialTab }: { children: ReactNode; initialTab?: Tab }) {
  const [state, dispatch] = useReducer(navReducer, initialTab, initialNav);
  const handlerCount = useHandlerCount();
  const canGoBack = handlerCount > 0 || state.stack.length > 1;

  const back = useCallback(() => {
    const top = handlers[handlers.length - 1];
    if (top) top.fn();
    else dispatch({ type: "pop" });
  }, []);

  useEffect(() => {
    const app = webApp();
    if (!app || !nativeBackButton()) return;
    app.BackButton.onClick(back);
    return () => {
      app.BackButton.offClick(back);
    };
  }, [back]);

  useEffect(() => {
    const app = webApp();
    if (!app || !nativeBackButton()) return;
    if (canGoBack) app.BackButton.show();
    else app.BackButton.hide();
  }, [canGoBack]);

  useEffect(
    () => () => {
      // The screens give way to the "session expired" gate: Telegram's Back would stay visible with no handler.
      const app = webApp();
      if (app && nativeBackButton()) app.BackButton.hide();
    },
    [],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && canGoBack) back();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [back, canGoBack]);

  const nav = useMemo<Nav>(
    () => ({
      tab: state.tab,
      stack: state.stack,
      canGoBack,
      back,
      pop: () => dispatch({ type: "pop" }),
      push: (route) => {
        haptic.tap();
        dispatch({ type: "push", route });
      },
      switchTab: (tab, route) => {
        haptic.select();
        dispatch({ type: "tab", tab, route });
      },
    }),
    [state, canGoBack, back],
  );
  return <NavContext.Provider value={nav}>{children}</NavContext.Provider>;
}
