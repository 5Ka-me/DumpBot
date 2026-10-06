/** The checklist of a card: ☐ / ☑ steps, add, edit (title, own time), delete, «🪄 Разбить на шаги». */
import { useEffect, useState } from "react";
import type { ItemDetail, Step, StepPatch } from "../../api/types";
import { Sheet } from "../../components/Sheet";
import { Section, Spinner } from "../../components/ui";
import { localDateTime } from "../../lib/format";
import { ACTIONABLE } from "../../lib/kinds";
import { dueLocal } from "./TimeSheet";

export const STEPS_MAX = 20;
const DATE_ONLY = new Set(["date", "fallback", "period", "soon"]);

export interface StepsProps {
  item: ItemDetail;
  tz: string;
  busy: boolean;
  /** The split job is running: its words ("Думаю…"). */
  splitStatus: string | null;
  onToggle: (step: Step) => void;
  onAdd: (title: string) => Promise<boolean>;
  onSave: (step: Step, patch: StepPatch) => Promise<boolean>;
  onDelete: (step: Step) => Promise<boolean>;
  onOpen: (step: Step) => void;
  onSplit: () => void;
}

function StepSheet({
  step,
  tz,
  busy,
  onClose,
  onSave,
  onDelete,
  onOpen,
}: {
  step: Step | null;
  tz: string;
  busy: boolean;
  onClose: () => void;
  onSave: (step: Step, patch: StepPatch) => Promise<boolean>;
  onDelete: (step: Step) => Promise<boolean>;
  onOpen: (step: Step) => void;
}) {
  const [title, setTitle] = useState("");
  const [date, setDate] = useState("");
  const [time, setTime] = useState("");
  useEffect(() => {
    if (!step) return;
    setTitle(step.title);
    const local = step.due_at ? localDateTime(step.due_at, tz) : "";
    setDate(local.slice(0, 10));
    setTime(local && !DATE_ONLY.has(step.precision ?? "") ? local.slice(11, 16) : "");
  }, [step, tz]);
  if (!step) return null;
  const save = async () => {
    const patch: StepPatch = {};
    if (title.trim() && title.trim() !== step.title) patch.title = title.trim();
    const wanted = date ? dueLocal(date, time) : null;
    const was = step.due_at ? localDateTime(step.due_at, tz) : null;
    if (wanted === null && was !== null) patch.due_local = null;
    else if (wanted !== null && (!was || !was.startsWith(wanted))) patch.due_local = wanted;
    if (!Object.keys(patch).length || (await onSave(step, patch))) onClose();
  };
  return (
    <Sheet
      open
      title={`Шаг ${step.position}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="button button--danger-text" disabled={busy} onClick={async () => (await onDelete(step)) && onClose()}>
            Удалить
          </button>
          <button type="button" className="button" disabled={busy || !title.trim()} onClick={save}>
            Сохранить
          </button>
        </>
      }
    >
      <label className="field-group">
        <span className="field-label">Название</span>
        <input className="input" value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <div className="field-group">
        <span className="field-label">Своё время (необязательно)</span>
        <div className="input-row">
          <input className="input" type="date" aria-label="Дата шага" value={date} onChange={(e) => setDate(e.target.value)} />
          <input
            className="input input--time"
            type="time"
            aria-label="Время шага"
            value={time}
            disabled={!date}
            onChange={(e) => setTime(e.target.value)}
          />
        </div>
        {date && (
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setDate("");
              setTime("");
            }}
          >
            Убрать время
          </button>
        )}
        <span className="field-hint">Шаг без времени не напоминает и виден только в этой карточке.</span>
      </div>
      <button type="button" className="text-button" onClick={() => onOpen(step)}>
        Открыть карточку шага →
      </button>
    </Sheet>
  );
}

export function Steps({ item, tz, busy, splitStatus, onToggle, onAdd, onSave, onDelete, onOpen, onSplit }: StepsProps) {
  const [title, setTitle] = useState("");
  const [editing, setEditing] = useState<Step | null>(null);
  const steps = item.steps;
  const full = steps.length >= STEPS_MAX;
  const deleted = item.status === "deleted";
  if (deleted && steps.length === 0) return null;
  const canAdd = !deleted && item.parent === null && !item.rec_active && (ACTIONABLE.has(item.kind) || steps.length > 0);
  if (!steps.length && !canAdd && !item.can_split) return null;
  const ratio = item.steps_total ? item.steps_done / item.steps_total : 0;
  return (
    <Section
      title={steps.length ? `Шаги · ${item.steps_done}/${item.steps_total}` : "Шаги"}
      aside={
        steps.length ? (
          <span className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={item.steps_total} aria-valuenow={item.steps_done} aria-label="Сделано шагов">
            <span style={{ width: `${Math.round(ratio * 100)}%` }} />
          </span>
        ) : undefined
      }
    >
      {steps.map((step) => (
        <div key={step.id} className={step.done ? "row row--done" : "row"}>
          <button
            type="button"
            className={step.done ? "check check--on" : "check"}
            role="checkbox"
            aria-checked={step.done}
            aria-label={`${step.done ? "Снять отметку" : "Отметить"}: ${step.title}`}
            disabled={deleted}
            onClick={() => onToggle(step)}
          >
            <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
              <path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
            </svg>
          </button>
          <button type="button" className="row-main" onClick={() => !deleted && setEditing(step)} aria-label={`Изменить шаг ${step.position}: ${step.title}`}>
            <span className="row-title">
              <span className="step-number">{step.position}.</span> {step.title}
            </span>
            {step.when_text && (
              <span className="row-meta">
                <span className="row-when">{step.when_text}</span>
              </span>
            )}
          </button>
        </div>
      ))}
      {canAdd && !full && (
        <form
          className="row row--form"
          onSubmit={async (e) => {
            e.preventDefault();
            const text = title.trim();
            if (text && (await onAdd(text))) setTitle("");
          }}
        >
          <span className="check check--ghost" aria-hidden="true">
            +
          </span>
          <input
            className="input input--bare"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Добавить шаг"
            aria-label="Новый шаг"
            maxLength={120}
            enterKeyHint="done"
          />
          {title.trim() && (
            <button type="submit" className="button button--small" disabled={busy}>
              Добавить
            </button>
          )}
        </form>
      )}
      {full && <p className="muted-row">В чек-листе уже {STEPS_MAX} шагов.</p>}
      {(item.can_split || splitStatus || item.split_pending) && !steps.length && (
        <div className="row row--action">
          {splitStatus || item.split_pending ? (
            <span className="inline-status">
              <Spinner small /> {splitStatus ?? "Разбиваю на шаги…"}
            </span>
          ) : (
            <button type="button" className="button button--secondary button--wide" disabled={busy} onClick={onSplit}>
              🪄 Разбить на шаги
            </button>
          )}
        </div>
      )}
      <StepSheet
        step={editing}
        tz={tz}
        busy={busy}
        onClose={() => setEditing(null)}
        onSave={onSave}
        onDelete={onDelete}
        onOpen={(s) => {
          setEditing(null);
          onOpen(s);
        }}
      />
    </Section>
  );
}
