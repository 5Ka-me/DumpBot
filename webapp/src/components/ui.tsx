/** Small building blocks: screen header, sections, states (loading / empty / error), chips, switches, segments. */
import { useId, type ReactNode } from "react";
import { errorText } from "../api/client";
import { nativeBackButton, useNav } from "../lib/nav";
import { Icon } from "./Icon";

export function Spinner({ label = "Загрузка…", small = false }: { label?: string; small?: boolean }) {
  return <span className={small ? "spinner spinner--small" : "spinner"} role="status" aria-label={label} />;
}

/** The screen's title row; outside Telegram it also has the Back button. */
export function ScreenHeader({ title, actions }: { title: string; actions?: ReactNode }) {
  const nav = useNav();
  const showBack = nav.stack.length > 1 && !nativeBackButton();
  return (
    <header className="screen-header">
      {showBack && (
        <button type="button" className="icon-button" onClick={nav.back} aria-label="Назад">
          <Icon name="back" />
        </button>
      )}
      <h1 className="screen-title">{title}</h1>
      {actions && <div className="screen-actions">{actions}</div>}
    </header>
  );
}

export function Section({
  title,
  aside,
  children,
  footer,
  tone,
}: {
  title?: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  tone?: "danger";
}) {
  const id = useId();
  return (
    <section className={tone ? `section section--${tone}` : "section"} aria-labelledby={title ? id : undefined}>
      {title && (
        <div className="section-head">
          <h2 id={id} className="section-title">
            {title}
          </h2>
          {aside}
        </div>
      )}
      <div className="section-body">{children}</div>
      {footer && <p className="section-footer">{footer}</p>}
    </section>
  );
}

/** Skeleton rows while the first answer loads. */
export function Loading({ rows = 4 }: { rows?: number }) {
  return (
    <div className="section" aria-busy="true" aria-label="Загрузка">
      <div className="section-body">
        {Array.from({ length: rows }, (_, i) => (
          <div key={i} className="skeleton-row">
            <span className="skeleton skeleton--dot" />
            <span className="skeleton skeleton--line" style={{ width: `${55 + ((i * 17) % 35)}%` }} />
          </div>
        ))}
      </div>
    </div>
  );
}

export function Empty({ emoji, title, text, action }: { emoji: string; title: string; text?: string; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-emoji" aria-hidden="true">
        {emoji}
      </div>
      <p className="empty-title">{title}</p>
      {text && <p className="empty-text">{text}</p>}
      {action}
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="error-box" role="alert">
      <p>{errorText(error)}</p>
      {onRetry && (
        <button type="button" className="button button--small" onClick={onRetry}>
          Повторить
        </button>
      )}
    </div>
  );
}

export function Chip({
  pressed,
  onClick,
  children,
  disabled,
  title,
}: {
  pressed?: boolean;
  onClick: () => void;
  children: ReactNode;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      className={pressed ? "chip chip--on" : "chip"}
      aria-pressed={pressed === undefined ? undefined : pressed}
      onClick={onClick}
      disabled={disabled}
      title={title}
    >
      {children}
    </button>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  hint?: string;
  disabled?: boolean;
}) {
  return (
    <label className="field field--row">
      <span className="field-text">
        <span className="field-label">{label}</span>
        {hint && <span className="field-hint">{hint}</span>}
      </span>
      <input
        type="checkbox"
        role="switch"
        className="switch"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
    </label>
  );
}

export interface SegmentOption<T extends string> {
  value: T;
  label: string;
}

/** A row of mutually exclusive options (tabs of a screen). */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: readonly SegmentOption<T>[];
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div className="segmented" role="tablist" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          aria-selected={o.value === value}
          className={o.value === value ? "segment segment--on" : "segment"}
          onClick={() => o.value !== value && onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** The header's ⟳: fetch the screen's data again. */
export function RefreshButton({ onClick, busy }: { onClick: () => void; busy?: boolean }) {
  return (
    <button type="button" className={busy ? "icon-button icon-button--busy" : "icon-button"} onClick={onClick} aria-label="Обновить" disabled={busy}>
      <Icon name="refresh" size={20} />
    </button>
  );
}

/** A tappable list row with a chevron (menus). */
export function LinkRow({ emoji, label, hint, onClick }: { emoji: string; label: string; hint?: string; onClick: () => void }) {
  return (
    <button type="button" className="link-row" onClick={onClick}>
      <span className="row-emoji" aria-hidden="true">
        {emoji}
      </span>
      <span className="link-row-text">
        <span>{label}</span>
        {hint && <span className="field-hint">{hint}</span>}
      </span>
      <span className="link-row-chevron">
        <Icon name="chevron" size={18} />
      </span>
    </button>
  );
}
