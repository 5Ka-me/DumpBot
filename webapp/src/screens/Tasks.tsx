/** «Задачи»: open tasks (with a time first), «когда-нибудь», repeating ones. */
import { useState } from "react";
import { api } from "../api";
import type { Row, RowsResponse } from "../api/types";
import { RowList } from "../components/RowItem";
import { Empty, ErrorBox, Loading, RefreshButton, ScreenHeader, Section, Segmented } from "../components/ui";
import { useOpenItem, useRowToggle } from "../lib/actions";
import { markStale, useResource } from "../lib/store";

type Mode = "tasks" | "someday" | "recurring";

const LOADERS: Record<Mode, (signal: AbortSignal) => Promise<RowsResponse>> = {
  tasks: (signal) => api.tasks({ signal }),
  someday: (signal) => api.someday({ signal }),
  recurring: (signal) => api.recurring({ signal }),
};

const EMPTY: Record<Mode, { emoji: string; title: string; text: string }> = {
  tasks: { emoji: "🎉", title: "Открытых задач нет", text: "Всё сделано — или ещё не закинуто." },
  someday: {
    emoji: "🌙",
    title: "Пока пусто",
    text: "Сюда попадает то, что «когда-нибудь»: без даты и без напоминаний.",
  },
  recurring: { emoji: "🔁", title: "Повторов нет", text: "Например: «каждый понедельник выносить мусор»." },
};

/** Open tasks: with a time, then without one (the API sends them in this order). */
export function splitByDue(rows: Row[]): { dated: Row[]; undated: Row[] } {
  return { dated: rows.filter((r) => r.due_at || r.period), undated: rows.filter((r) => !r.due_at && !r.period) };
}

function ModeList({ mode }: { mode: Mode }) {
  const open = useOpenItem();
  const toggle = useRowToggle(mode);
  const res = useResource(mode, LOADERS[mode]);
  if (!res.data) return res.error ? <ErrorBox error={res.error} onRetry={res.reload} /> : <Loading />;
  const rows = res.data.rows;
  const errorBox = res.error ? <ErrorBox error={res.error} onRetry={res.reload} /> : null;
  if (rows.length === 0) {
    const e = EMPTY[mode];
    return (
      <>
        {errorBox}
        <Empty emoji={e.emoji} title={e.title} text={e.text} />
      </>
    );
  }
  if (mode === "tasks") {
    const { dated, undated } = splitByDue(rows);
    return (
      <>
        {errorBox}
        {dated.length > 0 && (
          <Section title={`Со сроком · ${dated.length}`}>
            <RowList rows={dated} onOpen={open} onToggle={toggle} />
          </Section>
        )}
        {undated.length > 0 && (
          <Section title={`Без срока · ${undated.length}`}>
            <RowList rows={undated} onOpen={open} onToggle={toggle} />
          </Section>
        )}
      </>
    );
  }
  return (
    <>
      {errorBox}
      <Section>
        <RowList rows={rows} onOpen={open} onToggle={mode === "recurring" ? toggle : undefined} snippet={mode === "someday"} />
      </Section>
    </>
  );
}

export function TasksScreen() {
  const [mode, setMode] = useState<Mode>("tasks");
  return (
    <>
      <ScreenHeader title="Задачи" actions={<RefreshButton onClick={() => markStale(mode)} />} />
      <Segmented
        label="Списки задач"
        value={mode}
        onChange={setMode}
        options={[
          { value: "tasks", label: "Открытые" },
          { value: "someday", label: "Когда-нибудь" },
          { value: "recurring", label: "Повторы" },
        ]}
      />
      <ModeList key={mode} mode={mode} />
    </>
  );
}
