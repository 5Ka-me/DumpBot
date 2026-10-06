/** «Спросить»: a question to the saved items (the same path as «?…» in the chat) → an answer or the found rows. */
import { useState, useSyncExternalStore } from "react";
import { api, errorText, isAbort } from "../api";
import type { AskResponse } from "../api/types";
import { AutoTextarea } from "../components/AutoTextarea";
import { RowList } from "../components/RowItem";
import { Chip, ScreenHeader, Section, Spinner } from "../components/ui";
import { useOpenItem } from "../lib/actions";
import { useMainButton } from "../lib/mainButton";
import { haptic } from "../telegram";

export const QUESTION_MAX = 1000;
const EXAMPLES = ["что у меня завтра?", "что купить?", "какие идеи были на этой неделе?", "что я сохранял про отпуск?"];

interface Asked {
  id: number;
  question: string;
  answer: AskResponse | null;
  error: string | null;
}

// Questions of this visit survive switching tabs.
let asked: Asked[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function setAsked(next: Asked[]) {
  asked = next;
  listeners.forEach((l) => l());
}

function useAsked(): Asked[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => asked,
  );
}

function AnswerCard({ entry }: { entry: Asked }) {
  const open = useOpenItem();
  const a = entry.answer;
  return (
    <Section title={<span className="ask-question">❓ {entry.question}</span>}>
      {!a && !entry.error && (
        <div className="row row--static">
          <span className="inline-status">
            <Spinner small /> Ищу ответ…
          </span>
        </div>
      )}
      {entry.error && (
        <div className="row row--static">
          <span className="error-text">{entry.error}</span>
        </div>
      )}
      {a?.answer && (
        <div className="row row--static">
          <p className="answer-text">{a.answer}</p>
        </div>
      )}
      {a && !a.answer && <p className="muted-row">{a.rows.length ? a.title : `${a.title} — ничего не нашлось`}</p>}
      {a && a.rows.length > 0 && <RowList rows={a.rows} onOpen={open} />}
    </Section>
  );
}

export function AskScreen() {
  const entries = useAsked();
  const [question, setQuestion] = useState("");
  const running = entries.some((e) => !e.answer && !e.error);

  const ask = async (text = question) => {
    const value = text.trim().replace(/^\?+/, "").trim();
    if (!value || running) return;
    const entry: Asked = { id: nextId++, question: value, answer: null, error: null };
    setAsked([entry, ...asked].slice(0, 10));
    setQuestion("");
    haptic.tap();
    try {
      const answer = await api.ask(value);
      haptic.success();
      setAsked(asked.map((e) => (e.id === entry.id ? { ...e, answer } : e)));
    } catch (error) {
      if (isAbort(error)) return;
      haptic.error();
      setAsked(asked.map((e) => (e.id === entry.id ? { ...e, error: errorText(error) } : e)));
    }
  };

  useMainButton(question.trim() ? { text: "Спросить", onClick: () => void ask(), progress: running } : null);

  return (
    <>
      <ScreenHeader title="Спросить" />
      <Section footer="Ответ ищется по твоим записям: ИИ составит запрос и, если нужно, ответит словами.">
        <AutoTextarea
          className="compose-input"
          value={question}
          maxLength={QUESTION_MAX}
          placeholder="Что у меня на выходных?"
          aria-label="Вопрос"
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void ask();
            }
          }}
        />
      </Section>
      {entries.length === 0 && (
        <div className="chips chips--padded">
          {EXAMPLES.map((example) => (
            <Chip key={example} onClick={() => void ask(example)}>
              {example}
            </Chip>
          ))}
        </div>
      )}
      {entries.map((entry) => (
        <AnswerCard key={entry.id} entry={entry} />
      ))}
    </>
  );
}
