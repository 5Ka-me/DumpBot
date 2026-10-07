/** A line of a list: ☐ toggle (tasks), emoji and title, time / repeat / checklist progress / parent / tags. */
import type { Row } from "../api/types";
import { canCheck, isDone, kindInfo } from "../lib/kinds";
import { AuthImage } from "./AuthImage";

export interface RowItemProps {
  row: Row;
  onOpen: (row: Row) => void;
  /** Show ☐ / ☑ for rows that can be done. */
  onToggle?: (row: Row) => void;
  /** The time is in the past (default: from `due_at`). */
  overdue?: boolean;
  /** Text on the right, e.g. when it was saved (feed). */
  aside?: (row: Row) => string | null;
  /** Show the first picture as a thumbnail. */
  thumbnail?: boolean;
  /** Show the snippet line. */
  snippet?: boolean;
}

/** An open dated row whose time has passed (a span or «когда-нибудь» never is). */
export function isOverdue(row: Row, now: number = Date.now()): boolean {
  return Boolean(row.due_at && !row.period && !row.someday && !isDone(row) && row.status === "open" && Date.parse(row.due_at) < now);
}

export function rowMeta(row: Row): string[] {
  const parts: string[] = [];
  if (row.repeat_text) parts.push(`🔁 ${row.repeat_text}`);
  if (row.steps_total > 0) parts.push(`☑ ${row.steps_done}/${row.steps_total}`);
  if (row.parent_title) parts.push(`из «${row.parent_title}»`);
  if (row.tags.length) parts.push(row.tags.map((t) => `#${t}`).join(" "));
  return parts;
}

export function RowItem({ row, onOpen, onToggle, overdue, aside, thumbnail, snippet }: RowItemProps) {
  const info = kindInfo(row.kind);
  const done = isDone(row);
  const late = overdue ?? isOverdue(row);
  const checkable = Boolean(onToggle) && canCheck(row);
  const meta = rowMeta(row);
  const asideText = aside?.(row);
  const classes = ["row", done ? "row--done" : "", row.status === "deleted" ? "row--deleted" : ""].join(" ");
  return (
    <div className={classes}>
      {checkable ? (
        <button
          type="button"
          className={done ? "check check--on" : "check"}
          role="checkbox"
          aria-checked={done}
          aria-label={`${done ? "Вернуть в не сделанные" : "Отметить сделанным"}: ${row.title}`}
          onClick={() => onToggle?.(row)}
        >
          <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
          </svg>
        </button>
      ) : (
        <span className="row-emoji" aria-hidden="true">
          {info.emoji}
        </span>
      )}
      <button type="button" className="row-main" onClick={() => onOpen(row)}>
        <span className="row-title">
          {checkable && (
            <span className="row-title-emoji" aria-hidden="true">
              {info.emoji}{" "}
            </span>
          )}
          {row.title}
        </span>
        {(row.when_text || meta.length > 0) && (
          <span className="row-meta">
            {row.when_text && <span className={late ? "row-when row-when--overdue" : "row-when"}>{row.when_text}</span>}
            {meta.map((m) => (
              <span key={m}>{m}</span>
            ))}
          </span>
        )}
        {snippet && row.snippet && row.snippet !== row.title && <span className="row-snippet">{row.snippet}</span>}
        <span className="visually-hidden">, {info.label}</span>
      </button>
      {(asideText || (thumbnail && row.images > 0)) && (
        <span className="row-aside">
          {asideText && <span className="row-aside-text">{asideText}</span>}
          {thumbnail && row.images > 0 && (
            <AuthImage className="thumb" path={`/api/files/${row.id}/0?size=thumb`} alt={`Картинка: ${row.title}`} />
          )}
        </span>
      )}
    </div>
  );
}

/** Rows in a section body; `key` is the item id (+ occurrence: a series may appear twice in a week). */
export function RowList(props: Omit<RowItemProps, "row"> & { rows: Row[] }) {
  const { rows, ...rest } = props;
  return (
    <>
      {rows.map((row) => (
        <RowItem key={`${row.id}:${row.occurrence_id ?? ""}`} row={row} {...rest} />
      ))}
    </>
  );
}
