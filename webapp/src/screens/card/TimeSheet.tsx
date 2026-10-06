/** «🕒 Время»: by words (the AI, like the bot's button), from the date / time picker, or a span without a date. */
import { useEffect, useState } from "react";
import type { ItemDetail, PeriodValue } from "../../api/types";
import { Sheet } from "../../components/Sheet";
import { Chip } from "../../components/ui";
import { MONTHS_IN, todayIn } from "../../lib/format";

export const PERIOD_CHOICES: { value: PeriodValue; label: string }[] = [
  { value: "this_week", label: "на этой неделе" },
  { value: "next_week", label: "на следующей неделе" },
  { value: "this_month", label: "в этом месяце" },
  { value: "next_month", label: "в следующем месяце" },
  { value: "soon", label: "на днях" },
];

/** "YYYY-MM-DD" + optional "HH:MM" -> the API's due_local. */
export function dueLocal(date: string, time: string): string {
  return time ? `${date}T${time}` : date;
}

/** A month two months ahead as a period chip ("в декабре" -> "2026-12"). */
export function laterMonth(today: string): { value: PeriodValue; label: string } {
  const [y, m] = today.split("-").map(Number) as [number, number];
  const index = (m + 1) % 12; // two months ahead, 0-based
  const year = m + 2 > 12 ? y + 1 : y;
  const month = String(index + 1).padStart(2, "0");
  return { value: `${year}-${month}` as PeriodValue, label: `в ${MONTHS_IN[index]}` };
}

export function TimeSheet({
  open,
  item,
  tz,
  busy,
  onClose,
  onText,
  onPick,
  onPeriod,
  onSomeday,
}: {
  open: boolean;
  item: ItemDetail;
  tz: string;
  busy: boolean;
  onClose: () => void;
  onText: (text: string) => void;
  onPick: (dueLocal: string) => void;
  onPeriod: (period: PeriodValue) => void;
  onSomeday: (someday: boolean) => void;
}) {
  const [text, setText] = useState("");
  const [date, setDate] = useState("");
  const [time, setTime] = useState("");
  const today = todayIn(tz);
  useEffect(() => {
    if (!open) return;
    const local = item.occurrence?.due_local;
    const precise = item.occurrence && !["date", "fallback", "period", "soon", "someday", "none"].includes(item.occurrence.precision);
    setText("");
    setDate(local ? local.slice(0, 10) : today);
    setTime(local && precise ? local.slice(11, 16) : "");
  }, [open, item.occurrence, today]);
  const later = laterMonth(today);
  return (
    <Sheet open={open} title="🕒 Время" onClose={onClose}>
      <form
        className="field-group"
        onSubmit={(e) => {
          e.preventDefault();
          if (text.trim()) onText(text.trim());
        }}
      >
        <label className="field-label" htmlFor="when-text">
          Словами
        </label>
        <div className="input-row">
          <input
            id="when-text"
            className="input"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="завтра в 15:00, каждый пн, за час"
            maxLength={300}
            enterKeyHint="done"
          />
          <button type="submit" className="button button--small" disabled={busy || !text.trim()}>
            Готово
          </button>
        </div>
        <span className="field-hint">Разберёт ИИ — как «🕒 Время» в боте: время, повтор, «напомнить за».</span>
      </form>
      <form
        className="field-group"
        onSubmit={(e) => {
          e.preventDefault();
          if (date) onPick(dueLocal(date, time));
        }}
      >
        <span className="field-label">Дата и время</span>
        <div className="input-row">
          <input className="input" type="date" aria-label="Дата" value={date} onChange={(e) => setDate(e.target.value)} required />
          <input className="input input--time" type="time" aria-label="Время" value={time} onChange={(e) => setTime(e.target.value)} />
          <button type="submit" className="button button--small" disabled={busy || !date}>
            OK
          </button>
        </div>
        <span className="field-hint">Без времени — напомню в «дата без времени» из настроек.</span>
      </form>
      <div className="field-group">
        <span className="field-label">Без точной даты</span>
        <div className="chips">
          {[...PERIOD_CHOICES, later].map((p) => (
            <Chip key={p.value} onClick={() => onPeriod(p.value)} disabled={busy}>
              {p.label}
            </Chip>
          ))}
          {item.someday ? (
            <Chip onClick={() => onSomeday(false)} disabled={busy}>
              ✖ убрать «когда-нибудь»
            </Chip>
          ) : (
            <Chip onClick={() => onSomeday(true)} disabled={busy}>
              🌙 когда-нибудь
            </Chip>
          )}
        </div>
      </div>
    </Sheet>
  );
}
