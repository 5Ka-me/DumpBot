/** Shared item actions of the lists: open a card, ☐ / ☑ with an optimistic update. */
import { useCallback } from "react";
import { ApiError, api } from "../api";
import type { ItemResponse, Row } from "../api/types";
import { haptic } from "../telegram";
import { isDone } from "./kinds";
import { useNav } from "./nav";
import { mapRows, withDone } from "./rows";
import { getCached, itemsChanged, setCached } from "./store";
import { toast, toastError } from "./toast";

const REFRESH_DELAY_MS = 450;

export function useOpenItem(): (row: { id: number }) => void {
  const nav = useNav();
  return useCallback((row) => nav.push({ name: "item", id: row.id }), [nav]);
}

/**
 * Done / not done of exactly the row's occurrence, like the bot's buttons under a card or a reminder: the evening's
 * row of a series may be this morning's missed instance (the next one already exists), a «7 дней» row Thursday's
 * while Wednesday's is still open. Without an occurrence of that state (an item without a time): the item's own.
 */
export function toggleRow(row: Pick<Row, "id" | "occurrence_id" | "occ_status" | "status">): Promise<ItemResponse> {
  const done = isDone(row);
  const occurrence = row.occurrence_id;
  const own = done ? row.occ_status === "done" : row.occ_status === "open" || row.occ_status === "missed";
  if (occurrence !== null && own) return api.occurrenceAction(row.id, done ? "undone" : "done", occurrence);
  return api.action(row.id, done ? "undone" : "done");
}

/** ☐ / ☑ of a row in the cached list `key`: checked at once, rolled back if the bot refuses. */
export function useRowToggle(key: string): (row: Row) => Promise<void> {
  return useCallback(
    async (row: Row) => {
      const done = isDone(row);
      haptic.tap();
      const before = getCached(key);
      setCached<unknown>(key, (prev: unknown) =>
        mapRows(prev, (r) => (r.id === row.id && r.occurrence_id === row.occurrence_id ? withDone(r, !done) : r)),
      );
      try {
        const res = await toggleRow(row);
        haptic.success();
        toast(res.message, "success");
        setCached(`item:${row.id}`, res.item);
        setTimeout(itemsChanged, REFRESH_DELAY_MS); // let the ☑ be seen before the row moves
      } catch (error) {
        setCached(key, before);
        toastError(error);
        if (error instanceof ApiError && (error.status === 404 || error.status === 409)) itemsChanged(); // the list is old
      }
    },
    [key],
  );
}
