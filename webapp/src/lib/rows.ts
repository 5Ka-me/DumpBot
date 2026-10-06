/** Optimistic changes of list rows inside any cached list shape (rows / agenda / week / feed page). */
import type { Agenda, Row } from "../api/types";

type RowFn = (row: Row) => Row;

function mapList(rows: Row[], fn: RowFn): Row[] {
  let changed = false;
  const out = rows.map((row) => {
    const next = fn(row);
    if (next !== row) changed = true;
    return next;
  });
  return changed ? out : rows;
}

function mapAgenda(a: Agenda, fn: RowFn): Agenda {
  return {
    ...a,
    open: mapList(a.open, fn),
    overdue: mapList(a.overdue, fn),
    done: mapList(a.done, fn),
    period_items: mapList(a.period_items, fn),
  };
}

function isAgenda(data: object): data is Agenda {
  return "open" in data && "overdue" in data && "period_items" in data;
}

/** `fn` applied to every row of `data` ({rows}, Agenda, {days: Agenda[]}); other data is returned as is. */
export function mapRows<T>(data: T, fn: RowFn): T {
  if (typeof data !== "object" || data === null) return data;
  if ("rows" in data && Array.isArray((data as { rows: unknown }).rows)) {
    return { ...data, rows: mapList((data as unknown as { rows: Row[] }).rows, fn) };
  }
  if ("days" in data && Array.isArray((data as { days: unknown }).days)) {
    return { ...data, days: (data as unknown as { days: Agenda[] }).days.map((d) => mapAgenda(d, fn)) };
  }
  if (isAgenda(data)) return mapAgenda(data, fn) as T;
  return data;
}

/** The row as done / not done (its current occurrence, or the item without one). */
export function withDone(row: Row, done: boolean): Row {
  const status = done ? "done" : "open";
  if (row.occurrence_id !== null) {
    return { ...row, occ_status: status, status: row.rrule ? row.status : status };
  }
  return { ...row, status };
}
