/**
 * Types of the Mini App API (the bot's backend, src/dumpbot/web/).
 * Contract with every field explained: docs/features/miniapp-api.md — keep the two in sync.
 *
 * Conventions:
 * - `IsoDateTime`: a moment in UTC, ISO 8601 with "Z" ("2026-10-06T08:00:00Z");
 * - `LocalDate`: "YYYY-MM-DD" in the owner's timezone; `LocalDateTime`: "YYYY-MM-DDTHH:MM" (wall time);
 * - `HHMM`: a time of day "HH:MM";
 * - human texts (when_text, message, note, error messages) are plain Russian text, never HTML.
 * Every /api request sends `Authorization: tma <Telegram.WebApp.initData>`.
 */

export type IsoDateTime = string;
export type LocalDate = string;
export type LocalDateTime = string;
export type HHMM = string;

export type Kind = "task" | "event" | "idea" | "note" | "purchase" | "link" | "meme" | "quote" | "other";
export type ItemStatus = "open" | "done" | "deleted";
export type OccurrenceStatus = "open" | "done" | "missed" | "cancelled";
export type ReminderStatus = "pending" | "sending" | "sent" | "missed" | "cancelled" | "failed";
export type Precision =
  | "exact"
  | "slot"
  | "date"
  | "default"
  | "relative"
  | "fallback"
  | "period"
  | "soon"
  | "someday"
  | "none";
export type PeriodKind = "week" | "month" | "season" | "soon";
export type Role = "event" | "remind" | "deadline";
export type SourceKind = "text" | "voice" | "photo" | "forward" | "link" | "other";
export type RawStatus = "new" | "processing" | "done" | "deferred" | "failed" | "ignored";
export type JobState = "queued" | "running" | "done" | "dead";
export type JobReason = "budget" | "ai_unavailable" | "ai_config" | "error" | "failed";
export type FeedStatus = "any" | "open" | "done" | "deleted";
/** Values of PATCH /api/items/{id} `period` ("YYYY-MM" is a month: "2026-11"). */
export type PeriodValue =
  | "this_week"
  | "next_week"
  | "this_month"
  | "next_month"
  | "spring"
  | "summer"
  | "autumn"
  | "winter"
  | "soon"
  | "someday"
  | `${number}-${number}`;

// --- errors ------------------------------------------------------------------------------------------------------

export type ErrorCode =
  | "bad_request"
  | "unauthorized"
  | "expired"
  | "budget_exhausted"
  | "forbidden"
  | "not_found"
  | "not_a_step"
  | "too_big"
  | "method_not_allowed"
  | "too_large"
  | "deleted"
  | "has_steps"
  | "cant_split"
  | "cant_have_steps"
  | "checklist_full"
  | "nothing_to_snooze"
  | "already_answered"
  | "no_question"
  | "not_listed"
  | "internal"
  | "ai_config"
  | "ai_failed"
  | "telegram_unavailable"
  | "ai_unavailable";

/** Body of every non-2xx /api answer. */
export interface ApiErrorBody {
  error: {
    code: ErrorCode;
    message: string;
    /** The request field that is wrong (400). */
    field?: string;
  };
}

// --- shared objects ----------------------------------------------------------------------------------------------

export interface Period {
  kind: PeriodKind;
  start: LocalDate;
  end: LocalDate;
  /** "на этой неделе", "в ноябре", "этой осенью", "на днях" */
  label: string;
}

/** A line of a list (feed, today, tasks…). */
export interface Row {
  id: number;
  kind: Kind;
  title: string;
  tags: string[];
  status: ItemStatus;
  due_at: IsoDateTime | null;
  occurrence_id: number | null;
  occ_status: OccurrenceStatus | null;
  rrule: string | null;
  /** "каждый понедельник" */
  repeat_text: string | null;
  created_at: IsoDateTime;
  snippet: string | null;
  urls: string[];
  period: Period | null;
  someday: boolean;
  parent_id: number | null;
  /** A step of a checklist: its parent's title ("(из «Переезд»)"). */
  parent_title: string | null;
  /** A parent: checklist progress ("2/5"); 0 otherwise. */
  steps_done: number;
  steps_total: number;
  /** "сегодня 13:00", "пт 9 окт 10:00", "на этой неделе", "когда-нибудь" */
  when_text: string | null;
  /** Number of picture previews (GET /api/files/{id}/{n}). */
  images: number;
}

export interface Agenda {
  day: LocalDate;
  open: Row[];
  overdue: Row[];
  done: Row[];
  /** Items whose week / month / season is running today ("🗓 На этой неделе"). */
  period_items: Row[];
}

export interface Occurrence {
  id: number;
  due_at: IsoDateTime;
  due_local: LocalDateTime;
  precision: Precision;
  status: OccurrenceStatus;
  done_at: IsoDateTime | null;
}

export interface Reminder {
  id: number;
  fire_at: IsoDateTime;
  /** How long before the occurrence's time (0: at it, or a later notification of "за день и за час"). */
  lead_minutes: number;
  /** Time chosen automatically (moved out of quiet hours). */
  auto: boolean;
  status: ReminderStatus;
}

export interface Step {
  id: number;
  /** 1..n in order. */
  position: number;
  title: string;
  done: boolean;
  due_at: IsoDateTime | null;
  precision: Precision | null;
  /** "чт 8 окт 15:00" */
  when_text: string | null;
}

export interface RelatedRef {
  id: number;
  kind: Kind;
  title: string;
  created_at: IsoDateTime;
}

export interface Forward {
  type: "user" | "hidden_user" | "chat" | "channel" | string;
  name: string | null;
  url: string | null;
  date: IsoDateTime | null;
}

export interface Source {
  kind: SourceKind;
  /** Full text / transcript / OCR the item came from. */
  text: string | null;
  forward: Forward | null;
  raw_id: number | null;
  /** Saved with POST /api/inbox. */
  from_app: boolean;
  message_date: IsoDateTime | null;
}

export interface ImageRef {
  n: number;
  /** "/api/files/{id}/{n}": fetch with the Authorization header, show as a blob URL. */
  url: string;
  mime_type: string;
  width: number | null;
  height: number | null;
}

/** GET /api/items/{id}: the full card. */
export interface ItemDetail {
  id: number;
  kind: Kind;
  title: string;
  details: string | null;
  tags: string[];
  status: ItemStatus;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  done_at: IsoDateTime | null;
  deleted_at: IsoDateTime | null;
  urls: string[];
  /** url -> page title */
  link_titles: Record<string, string>;
  /** 🔕: no notifications. (An item's time is its notification time: there is no "напомнить за" any more.) */
  reminders_off: boolean;
  rrule: string | null;
  rec_active: boolean;
  repeat_text: string | null;
  period: Period | null;
  someday: boolean;
  role: Role;
  needs_confirm: boolean;
  parent: { id: number; title: string } | null;
  position: number | null;
  occurrence: Occurrence | null;
  reminders: Reminder[];
  /**
   * When it notifies: "🔔 завтра 13:00", "🔔 вс 11 окт 10:00 · пн 12 окт 09:00", "⏳ до пт 9 окт · 🔔 чт 19:00, пт 10:00",
   * "🗓 на этой неделе (до вс 11 окт) · 🔔 пт 19:00", "🔕 завтра 13:00", "🌙 когда-нибудь".
   */
  when_text: string | null;
  steps: Step[];
  steps_done: number;
  steps_total: number;
  related: RelatedRef[];
  source: Source;
  images: ImageRef[];
  /** Show "🪄 Разбить на шаги". */
  can_split: boolean;
  split_pending: boolean;
  /** Only in answers to changes: "дата в прошлом"… */
  warnings: string[];
}

/** Answer to a change of an item (PATCH, actions, time from the picker). */
export interface ItemResponse {
  item: ItemDetail;
  /** The bot's toast: "✅ Готово", "⏰ Напомню завтра 13:00". */
  message: string | null;
}

/** Answer to a change of a checklist (the parent's detail). */
export interface StepResponse extends ItemResponse {
  /** This step closed the parent (🎉). */
  closed: boolean;
}

/** 202: an AI job was queued; poll GET /api/jobs/{job_id}. */
export interface JobAccepted {
  job_id: number;
  /** true: the same job was already running. */
  pending: boolean;
}

export interface JobOutcome {
  ok: boolean;
  reason: string | null;
  /** Russian text to show when ok is false. */
  message: string | null;
}

/** Result of "edit_time" (POST /api/items/{id}/when {text}). */
export interface EditTimeResult extends JobOutcome {
  reason: null | "not_understood" | "gone" | "failed" | "empty";
  item_id?: number;
  warnings?: string[];
}

/** Result of "split" (POST /api/items/{id}/split). */
export interface SplitResult extends JobOutcome {
  reason: null | "no_steps" | "has_steps" | "cant_split" | "gone" | "failed";
  steps?: number;
}

export interface JobStatus {
  id: number;
  kind: string;
  status: JobState;
  retry_at: IsoDateTime | null;
  reason: JobReason | null;
  result: EditTimeResult | SplitResult | Record<string, unknown> | null;
}

// --- endpoints ---------------------------------------------------------------------------------------------------

/** GET /api/me */
export interface Me {
  user: { id: number; first_name: string; last_name: string | null; username: string | null };
  tz: string;
  now: IsoDateTime;
  today: LocalDate;
  kinds: { kind: Kind; label: string; emoji: string }[];
  local_stt: boolean;
}

/** GET /api/feed query parameters. */
export interface FeedQuery {
  q?: string;
  /** Sent comma-separated. */
  kinds?: Kind[];
  /** Sent comma-separated. */
  tags?: string[];
  status?: FeedStatus;
  from?: LocalDate;
  to?: LocalDate;
  cursor?: string;
  limit?: number;
}

/** GET /api/feed */
export interface FeedPage {
  rows: Row[];
  /** null: no more pages. */
  next_cursor: string | null;
}

/** GET /api/tasks, /api/someday, /api/recurring */
export interface RowsResponse {
  rows: Row[];
}

/** GET /api/today: Agenda; GET /api/week: */
export interface Week {
  days: Agenda[];
}

/** PATCH /api/items/{id} */
export interface ItemPatch {
  title?: string;
  details?: string | null;
  tags?: string[];
  kind?: Kind;
  period?: PeriodValue;
  someday?: boolean;
}

/** POST /api/items/{id}/when */
export type WhenRequest = { text: string } | { due_local: LocalDate | LocalDateTime };

/** POST /api/items/{id}/snooze */
export type SnoozeRequest = { minutes: number } | { tomorrow: true };

export type ItemAction = "done" | "undone" | "norem" | "delete" | "restore" | "stoprec" | "snooze";

/** POST /api/items/{id}/done|undone: that occurrence of a list row (without it: the current one). */
export interface OccurrenceRequest {
  occurrence_id?: number;
}

/** POST /api/today/move: «📅 Перенести невыполненное на завтра» of a day (default: today). */
export interface MoveRequest {
  day?: LocalDate;
}

export interface MoveResult {
  moved: number;
  /** "📅 Перенёс на завтра: 3" or "Нечего переносить". */
  message: string;
}

/** POST /api/items/{id}/steps */
export interface NewStep {
  title: string;
  due_local?: LocalDate | LocalDateTime;
}

/** PATCH /api/steps/{id} */
export interface StepPatch {
  title?: string;
  /** null: no time of its own. */
  due_local?: LocalDate | LocalDateTime | null;
}

/** POST /api/steps/{id}/toggle */
export interface StepToggle {
  /** Omitted: flip. */
  done?: boolean;
}

/** POST /api/inbox */
export interface InboxRequest {
  text: string;
}

/** GET /api/inbox/{raw_id} (and the 202 answers of POST /api/inbox, …/answer, …/pick). */
export interface InboxStatus {
  raw_id: number;
  text: string;
  status: RawStatus;
  /** Still being processed: poll again. */
  pending: boolean;
  created_at: IsoDateTime;
  job: JobStatus | null;
  /** Items the message created (steps are inside their parent). */
  items: Row[];
  /** What the bot would have said in the chat. */
  note: string | null;
  /** A clarifying question until answered (POST …/answer {option}). */
  ask: { question: string; options: string[] } | null;
  /** "Какую запись изменить?" until one is picked (POST …/pick {item_id}). */
  candidates: Row[] | null;
  /** Answer to a "?" question: text, else the rows under the title. */
  answer: { text: string | null; title: string; rows: Row[] } | null;
  /** Items this message edited. */
  edited: Row[];
}

/** POST /api/inbox/{raw_id}/answer */
export interface InboxAnswer {
  /** Index into ask.options, from 0. */
  option: number;
}

/** POST /api/inbox/{raw_id}/pick */
export interface InboxPick {
  item_id: number;
}

/** POST /api/ask */
export interface AskRequest {
  question: string;
}

export interface QuerySpec {
  text: string | null;
  kinds: Kind[];
  status: "open" | "done" | "any";
  date_field: "due" | "created";
  range: string | null;
  tags: string[];
  answer: "list" | "summary";
  limit: number;
}

/** POST /api/ask answer: text, else the rows under the title. */
export interface AskResponse {
  answer: string | null;
  title: string;
  rows: Row[];
  query: QuerySpec;
}

export type SttMode = "auto" | "local" | "chutes";

/** Every preference (GET /api/settings); PATCH takes any subset. */
export interface Prefs {
  tz: string;
  slot_morning: HHMM;
  slot_lunch: HHMM;
  slot_afternoon: HHMM;
  slot_evening: HHMM;
  slot_after_work: HHMM;
  slot_night: HHMM;
  weekend_time: HHMM;
  bare_date_time: HHMM;
  task_default_time: HHMM;
  /** 1…14 */
  soon_days: number;
  quiet_enabled: boolean;
  quiet_start: HHMM;
  quiet_end: HHMM;
  night_cutoff: HHMM;
  digest_morning_enabled: boolean;
  digest_morning_time: HHMM;
  digest_evening_enabled: boolean;
  digest_evening_time: HHMM;
  /** ≥ 0, cents; 0 = AI off. */
  budget_usd: number;
  stt_language: "ru" | "en" | null;
  stt_mode: SttMode;
  /** ≥ 1 */
  missed_grace_minutes: number;
}

/** GET / PATCH /api/settings */
export interface SettingsResponse {
  prefs: Prefs;
  /** The local Whisper is configured (LOCAL_STT_URL): "local" mode works. */
  local_stt: boolean;
}

export type SettingsPatch = Partial<Prefs>;

/** GET /api/usage */
export interface Usage {
  /** "октябрь 2026" */
  month: string;
  spent_usd: number;
  limit_usd: number;
  /** spent / limit; null when the limit is 0 and something was spent. */
  ratio: number | null;
  exhausted: boolean;
  calls: number;
  by_purpose: { purpose: string; label: string; calls: number; usd: number }[];
}
