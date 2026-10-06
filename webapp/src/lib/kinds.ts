/** Categories and row states, as in the bot (src/dumpbot/domain/kinds.py). */
import type { Kind, Row } from "../api/types";

export interface KindInfo {
  kind: Kind;
  label: string;
  emoji: string;
}

/** The categories in the bot's menu order (GET /api/me sends the same list). */
export const KINDS: readonly KindInfo[] = [
  { kind: "task", label: "Задача", emoji: "📌" },
  { kind: "event", label: "Событие", emoji: "📅" },
  { kind: "idea", label: "Идея", emoji: "💡" },
  { kind: "note", label: "Заметка", emoji: "📝" },
  { kind: "purchase", label: "Покупка", emoji: "🛒" },
  { kind: "link", label: "Ссылка", emoji: "🔗" },
  { kind: "meme", label: "Мем", emoji: "😂" },
  { kind: "quote", label: "Цитата", emoji: "💬" },
  { kind: "other", label: "Другое", emoji: "📦" },
];

const BY_KIND = new Map(KINDS.map((k) => [k.kind, k]));

export function kindInfo(kind: Kind | string): KindInfo {
  return BY_KIND.get(kind as Kind) ?? { kind: "other", label: "Другое", emoji: "📦" };
}

/** Kinds that can be done / not done (and get a default reminder). */
export const ACTIONABLE: ReadonlySet<Kind> = new Set<Kind>(["task", "event", "purchase"]);

/** The row's current occurrence (or the item itself) is done. */
export function isDone(row: Pick<Row, "status" | "occ_status">): boolean {
  return row.status === "done" || row.occ_status === "done";
}

/** A list row gets a ☐ / ☑ toggle. */
export function canCheck(row: Pick<Row, "status" | "kind" | "occurrence_id">): boolean {
  return row.status !== "deleted" && (ACTIONABLE.has(row.kind) || row.occurrence_id !== null);
}

export const SOURCE_LABELS: Record<string, string> = {
  text: "Текст",
  voice: "Голосовое",
  photo: "Фото",
  forward: "Пересланное",
  link: "Ссылка",
  other: "Сообщение",
};
