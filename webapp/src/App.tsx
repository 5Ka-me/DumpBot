/** The Mini App: start-up checks (inside Telegram, signed in), then the tabs and their screen stacks. */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { ApiError, api, onAuthError } from "./api";
import type { Me } from "./api/types";
import { MainButtonBar, TabBar, Toaster } from "./components/Chrome";
import { ErrorBox, Spinner } from "./components/ui";
import { hasDrafts } from "./lib/drafts";
import { MeContext } from "./lib/me";
import { NavProvider, useNav, type Route } from "./lib/nav";
import { ScreenActive, markStale } from "./lib/store";
import { AddScreen } from "./screens/Add";
import { AskScreen } from "./screens/Ask";
import { CardScreen } from "./screens/card/CardScreen";
import { FeedScreen } from "./screens/Feed";
import { MoreScreen } from "./screens/More";
import { SettingsScreen } from "./screens/Settings";
import { TasksScreen } from "./screens/Tasks";
import { TodayScreen } from "./screens/Today";
import { UsageScreen } from "./screens/Usage";
import { closeApp, insideTelegram } from "./telegram";

export const DRAFTS_KEPT_TEXT = "Несохранённый текст не пропадёт: он вернётся, когда откроешь приложение снова.";

/** Back after this long in the background: everything is refreshed. */
const REFRESH_AFTER_MS = 60_000;

function screenOf(route: Route) {
  switch (route.name) {
    case "today":
      return <TodayScreen />;
    case "feed":
      return <FeedScreen initialStatus={route.status} />;
    case "add":
      return <AddScreen />;
    case "tasks":
      return <TasksScreen />;
    case "more":
      return <MoreScreen />;
    case "item":
      return <CardScreen id={route.id} />;
    case "ask":
      return <AskScreen />;
    case "settings":
      return <SettingsScreen />;
    case "usage":
      return <UsageScreen />;
  }
}

function Shell() {
  const nav = useNav();
  const root = nav.stack.length === 1;
  return (
    <div className={root ? "app app--tabs" : "app"}>
      <main className="screens">
        {nav.stack.map((entry, i) => {
          const top = i === nav.stack.length - 1;
          return (
            <ScreenActive.Provider key={entry.key} value={top}>
              <div className={top ? "screen screen--top" : "screen screen--hidden"} aria-hidden={!top} inert={!top}>
                <div className="screen-inner">{screenOf(entry.route)}</div>
              </div>
            </ScreenActive.Provider>
          );
        })}
      </main>
      {root && <TabBar />}
      <MainButtonBar />
      <Toaster />
    </div>
  );
}

export function Gate({ emoji, title, text, action }: { emoji: string; title: string; text: string; action?: ReactNode }) {
  return (
    <div className="gate">
      <div className="gate-emoji" aria-hidden="true">
        {emoji}
      </div>
      <h1 className="gate-title">{title}</h1>
      <p className="gate-text">{text}</p>
      {action}
    </div>
  );
}

type Boot = { state: "loading" } | { state: "ready"; me: Me } | { state: "failed"; error: unknown };

export function App({ outside = !insideTelegram() && !import.meta.env.DEV }: { outside?: boolean }) {
  const [boot, setBoot] = useState<Boot>({ state: "loading" });
  const [authError, setAuthError] = useState<ApiError | null>(null);

  const load = useCallback(async () => {
    setBoot({ state: "loading" });
    try {
      setBoot({ state: "ready", me: await api.me() });
    } catch (error) {
      setBoot({ state: "failed", error });
    }
  }, []);

  useEffect(() => {
    onAuthError(setAuthError);
    return () => onAuthError(null);
  }, []);

  useEffect(() => {
    if (!outside) void load();
  }, [outside, load]);

  useEffect(() => {
    let hiddenAt = 0;
    const onVisibility = () => {
      if (document.visibilityState === "hidden") hiddenAt = Date.now();
      else if (hiddenAt && Date.now() - hiddenAt > REFRESH_AFTER_MS) markStale("");
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  const meState = useMemo(
    () => ({
      me: boot.state === "ready" ? boot.me : null,
      setTz: (tz: string) => setBoot((b) => (b.state === "ready" ? { ...b, me: { ...b.me, tz } } : b)),
    }),
    [boot],
  );

  if (outside) {
    return (
      <Gate
        emoji="📱"
        title="Свалка"
        text="Приложение открывается внутри Telegram: в чате с ботом нажми кнопку «📱 Свалка» слева от поля ввода."
      />
    );
  }
  if (authError) {
    // The screens unmount; their unsaved input stays in lib/drafts (localStorage) for the reopened app.
    const expired = authError.code === "expired";
    return (
      <Gate
        emoji={expired ? "⏳" : "🔒"}
        title={expired ? "Сессия устарела" : "Нет доступа"}
        text={expired && hasDrafts() ? `${authError.message} ${DRAFTS_KEPT_TEXT}` : authError.message}
        action={
          insideTelegram() ? (
            <button type="button" className="button" onClick={closeApp}>
              Закрыть
            </button>
          ) : undefined
        }
      />
    );
  }
  if (boot.state === "loading") {
    return (
      <div className="gate">
        <Spinner />
      </div>
    );
  }
  if (boot.state === "failed") {
    return (
      <div className="gate">
        <div className="gate-emoji" aria-hidden="true">
          🔌
        </div>
        <ErrorBox error={boot.error} onRetry={() => void load()} />
      </div>
    );
  }
  return (
    <MeContext.Provider value={meState}>
      <NavProvider>
        <Shell />
      </NavProvider>
    </MeContext.Provider>
  );
}
