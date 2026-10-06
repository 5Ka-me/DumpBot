/** Shared item actions of the lists: open a card, ☐ / ☑ with an optimistic update. */
import { useCallback } from "react";
import { api } from "../api";
import type { Row } from "../api/types";
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
        const res = await api.action(row.id, done ? "undone" : "done");
        haptic.success();
        toast(res.message, "success");
        setCached(`item:${row.id}`, res.item);
        setTimeout(itemsChanged, REFRESH_DELAY_MS); // let the ☑ be seen before the row moves
      } catch (error) {
        setCached(key, before);
        toastError(error);
      }
    },
    [key],
  );
}
