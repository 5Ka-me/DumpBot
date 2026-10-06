/** Test data. */
import type { Row } from "../api/types";

export function row(over: Partial<Row> = {}): Row {
  return {
    id: 1,
    kind: "task",
    title: "Позвонить в банк",
    tags: [],
    status: "open",
    due_at: "2026-10-06T13:00:00Z",
    occurrence_id: 10,
    occ_status: "open",
    rrule: null,
    repeat_text: null,
    created_at: "2026-10-05T10:00:00Z",
    snippet: null,
    urls: [],
    period: null,
    someday: false,
    parent_id: null,
    parent_title: null,
    steps_done: 0,
    steps_total: 0,
    when_text: "сегодня 15:00",
    images: 0,
    ...over,
  };
}
