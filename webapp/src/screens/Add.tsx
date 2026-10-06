/** «Добавить»: a text into the dump (the same AI pipeline as a message to the bot) and what came of it. */
import { useEffect, useRef, useState } from "react";
import type { InboxStatus, Row } from "../api/types";
import { AutoTextarea } from "../components/AutoTextarea";
import { RowList } from "../components/RowItem";
import { ScreenHeader, Section, Spinner } from "../components/ui";
import { useOpenItem } from "../lib/actions";
import { fmtStamp } from "../lib/format";
import {
  answerQuestion,
  forgetEntry,
  pickCandidate,
  refreshEntry,
  restoreInbox,
  sendToInbox,
  useInbox,
  type InboxEntry,
} from "../lib/inbox";
import { jobWaitText } from "../lib/jobs";
import { useMainButton } from "../lib/mainButton";
import { useTz } from "../lib/me";
import { toastError } from "../lib/toast";
import { setClosingConfirmation } from "../telegram";

export const INBOX_MAX = 4096;

/** The one-line state of a sent text. */
export function inboxStateText(status: InboxStatus): string {
  if (status.ask) return "🤔 Нужно уточнить";
  if (status.candidates) return "🤔 Какую запись изменить?";
  if (status.pending) {
    if (status.status === "deferred") return jobWaitText(status.job) ?? "✍ Ждёт ИИ — разберу позже";
    return "⏳ Разбираю…";
  }
  switch (status.status) {
    case "failed":
      return "😢 Не получилось";
    case "ignored":
      return "Пропущено";
    default:
      if (status.answer) return "❓ Ответ";
      if (status.edited.length) return "✏️ Изменено";
      if (status.items.length) return status.items.length === 1 ? "✅ Сохранено" : `✅ Сохранено: ${status.items.length}`;
      return "✅ Готово";
  }
}

function EntryCard({ entry }: { entry: InboxEntry }) {
  const tz = useTz();
  const open = useOpenItem();
  const [busy, setBusy] = useState(false);
  const s = entry.status;
  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  const pick = (row: Row) => void act(() => pickCandidate(s.raw_id, row.id));
  return (
    <Section
      title={
        <span className="inbox-state">
          {s.pending && entry.polling && <Spinner small />} {inboxStateText(s)}
        </span>
      }
      aside={
        <span className="section-aside">
          {fmtStamp(s.created_at, tz)}
          {!s.pending && (
            <button type="button" className="mini-close" onClick={() => forgetEntry(s.raw_id)} aria-label="Скрыть">
              ✕
            </button>
          )}
        </span>
      }
    >
      <div className="row row--static">
        <p className="inbox-text">{s.text}</p>
      </div>
      {s.note && (
        <div className="row row--static">
          <span>{s.note}</span>
        </div>
      )}
      {s.ask && (
        <div className="field-group field-group--inset">
          <span>{s.ask.question}</span>
          <div className="chips">
            {s.ask.options.map((option, i) => (
              <button key={option} type="button" className="chip" disabled={busy} onClick={() => void act(() => answerQuestion(s.raw_id, i))}>
                {option}
              </button>
            ))}
          </div>
        </div>
      )}
      {s.candidates && (
        <>
          <p className="muted-row">Выбери запись — изменю её:</p>
          <RowList rows={s.candidates} onOpen={pick} />
        </>
      )}
      {s.answer && (
        <>
          {s.answer.text ? (
            <div className="row row--static">
              <p className="answer-text">{s.answer.text}</p>
            </div>
          ) : (
            <p className="muted-row">{s.answer.title}</p>
          )}
          {!s.answer.text && <RowList rows={s.answer.rows} onOpen={open} />}
        </>
      )}
      {s.items.length > 0 && <RowList rows={s.items} onOpen={open} />}
      {s.edited.length > 0 && <RowList rows={s.edited} onOpen={open} />}
      {(entry.paused || entry.error) && (
        <div className="row row--action">
          <span className="field-hint">{entry.error ?? "Ещё обрабатывается."}</span>
          <button type="button" className="text-button" onClick={() => void refreshEntry(s.raw_id)}>
            Обновить
          </button>
        </div>
      )}
    </Section>
  );
}

export function AddScreen() {
  const entries = useInbox();
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const restored = useRef(false);

  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    void restoreInbox();
  }, []);

  useEffect(() => {
    setClosingConfirmation(text.trim().length > 0);
    return () => setClosingConfirmation(false);
  }, [text]);

  const send = async () => {
    const value = text.trim();
    if (!value || sending) return;
    setSending(true);
    try {
      await sendToInbox(value);
      setText("");
    } catch (error) {
      toastError(error);
    } finally {
      setSending(false);
    }
  };

  useMainButton(text.trim() ? { text: "📥 Закинуть в свалку", onClick: () => void send(), progress: sending } : null);

  return (
    <>
      <ScreenHeader title="Закинуть в свалку" />
      <Section footer="Как сообщение боту: ИИ разложит по записям и поставит напоминания. Начни с «?» — это вопрос.">
        <AutoTextarea
          className="compose-input"
          value={text}
          maxLength={INBOX_MAX}
          placeholder="Например: завтра в 10 позвонить в банк"
          aria-label="Что закинуть"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void send();
          }}
        />
        {text.length > INBOX_MAX - 200 && (
          <p className="field-hint counter">
            {text.length}/{INBOX_MAX}
          </p>
        )}
      </Section>
      {entries.map((entry) => (
        <EntryCard key={entry.rawId} entry={entry} />
      ))}
    </>
  );
}
