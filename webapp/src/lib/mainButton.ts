/**
 * Telegram's MainButton for the active screen's main action ("Сохранить", "Закинуть в свалку"). Only the screen on
 * top may own it; outside Telegram (or before Bot API 6.0) the app draws the same button itself
 * (`useMainButtonState` + components/MainButtonBar).
 */
import { useContext, useEffect, useRef, useSyncExternalStore } from "react";
import { supports, webApp } from "../telegram";
import { ScreenActive } from "./store";

export interface MainButtonSpec {
  text: string;
  onClick: () => void;
  disabled?: boolean;
  /** A spinner on the button (the action is running). */
  progress?: boolean;
}

interface Owner {
  id: number;
  spec: MainButtonSpec;
}

let owner: Owner | null = null;
let ownerId = 1;
let bound = false;
const listeners = new Set<() => void>();

export function nativeMainButton(): boolean {
  return supports("6.0");
}

function press(): void {
  const spec = owner?.spec;
  if (spec && !spec.disabled && !spec.progress) spec.onClick();
}

function sync(): void {
  listeners.forEach((l) => l());
  const app = webApp();
  if (!app || !nativeMainButton()) return;
  const button = app.MainButton;
  if (!bound) {
    button.onClick(press);
    bound = true;
  }
  const spec = owner?.spec;
  if (!spec) {
    button.hideProgress();
    button.setParams({ is_visible: false });
    return;
  }
  button.setParams({ text: spec.text, is_visible: true, is_active: !spec.disabled });
  if (spec.progress) button.showProgress(false);
  else button.hideProgress();
}

/** Show `spec` as the main button while this screen is on top (null: no button). */
export function useMainButton(spec: MainButtonSpec | null): void {
  const active = useContext(ScreenActive);
  const id = useRef(0);
  const click = useRef<() => void>(() => undefined);
  useEffect(() => {
    if (spec) click.current = spec.onClick;
  });
  const shown = active && spec !== null;
  const text = spec?.text ?? "";
  const disabled = Boolean(spec?.disabled);
  const progress = Boolean(spec?.progress);
  useEffect(() => {
    if (!id.current) id.current = ownerId++;
    if (shown) {
      owner = { id: id.current, spec: { text, disabled, progress, onClick: () => click.current() } };
      sync();
    } else if (owner?.id === id.current) {
      owner = null;
      sync();
    }
  }, [shown, text, disabled, progress]);
  useEffect(
    () => () => {
      if (owner && owner.id === id.current) {
        owner = null;
        sync();
      }
    },
    [],
  );
}

/** The current main button (for the in-app fallback). */
export function useMainButtonState(): MainButtonSpec | null {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => owner?.spec ?? null,
  );
}

/** Press the current main button (the fallback's click). */
export function pressMainButton(): void {
  press();
}
