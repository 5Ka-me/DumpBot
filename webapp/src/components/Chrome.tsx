/** App chrome: the tab bar, toasts, and the in-app main button (when Telegram's MainButton is not available). */
import { nativeMainButton, pressMainButton, useMainButtonState } from "../lib/mainButton";
import { useNav, type Tab } from "../lib/nav";
import { dismissToast, useToasts } from "../lib/toast";
import { Icon, type IconName } from "./Icon";
import { Spinner } from "./ui";

const TABS: { tab: Tab; label: string; icon: IconName }[] = [
  { tab: "today", label: "Сегодня", icon: "today" },
  { tab: "feed", label: "Лента", icon: "feed" },
  { tab: "add", label: "Добавить", icon: "add" },
  { tab: "tasks", label: "Задачи", icon: "tasks" },
  { tab: "more", label: "Ещё", icon: "more" },
];

export function TabBar() {
  const nav = useNav();
  return (
    <nav className="tabbar" aria-label="Разделы">
      {TABS.map(({ tab, label, icon }) => {
        const current = nav.tab === tab;
        return (
          <button
            key={tab}
            type="button"
            className={`${current ? "tab tab--on" : "tab"}${tab === "add" ? " tab--add" : ""}`}
            aria-current={current ? "page" : undefined}
            onClick={() => {
              if (current && nav.stack.length === 1) {
                document.querySelector(".screen--top")?.scrollTo({ top: 0, behavior: "smooth" });
              } else nav.switchTab(tab);
            }}
          >
            <span className="tab-icon">
              <Icon name={icon} size={tab === "add" ? 26 : 22} />
            </span>
            <span className="tab-label">{label}</span>
          </button>
        );
      })}
    </nav>
  );
}

export function Toaster() {
  const toasts = useToasts();
  return (
    <div className="toasts" aria-live="polite" aria-atomic="false">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast--${t.tone}`} role={t.tone === "error" ? "alert" : "status"} onClick={() => dismissToast(t.id)}>
          {t.text}
        </div>
      ))}
    </div>
  );
}

/** Outside Telegram: the screen's main action as a bottom button. */
export function MainButtonBar() {
  const spec = useMainButtonState();
  if (!spec || nativeMainButton()) return null;
  return (
    <div className="mainbar">
      <button type="button" className="button button--wide" disabled={spec.disabled || spec.progress} onClick={pressMainButton}>
        {spec.progress ? <Spinner small label={spec.text} /> : spec.text}
      </button>
    </div>
  );
}
