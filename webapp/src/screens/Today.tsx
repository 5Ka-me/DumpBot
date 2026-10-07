/**
 * «Сегодня»: today's agenda (overdue, today, the running week / month / season, done) with the evening digest's
 * «📅 Перенести невыполненное на завтра», or the next seven days.
 */
import { useState, type ReactNode } from "react";
import { api } from "../api";
import type { Agenda, Row } from "../api/types";
import { RowList } from "../components/RowItem";
import { Empty, ErrorBox, Loading, RefreshButton, ScreenHeader, Section, Segmented, Spinner } from "../components/ui";
import { useOpenItem, useRowToggle } from "../lib/actions";
import { count, fmtDate, fmtDayHeader, fmtLongDate, todayIn } from "../lib/format";
import { isDone } from "../lib/kinds";
import { useTz } from "../lib/me";
import { useNav } from "../lib/nav";
import { itemsChanged, markStale, useResource } from "../lib/store";
import { toast, toastError } from "../lib/toast";
import { haptic } from "../telegram";

type Mode = "today" | "week";

/** Rows of the running spans grouped by their label and end ("на этой неделе", до вс 11 окт). */
export function periodGroups(rows: Row[]): { title: string; end: string; rows: Row[] }[] {
  const groups = new Map<string, { title: string; end: string; rows: Row[] }>();
  for (const row of rows) {
    const label = row.period?.label ?? "без срока";
    const end = row.period?.end ?? "";
    const key = `${label}|${end}`;
    let group = groups.get(key);
    if (!group) groups.set(key, (group = { title: label, end, rows: [] }));
    group.rows.push(row);
  }
  return [...groups.values()].sort((a, b) => a.end.localeCompare(b.end));
}

function agendaSize(a: Agenda): number {
  return a.open.length + a.overdue.length + a.done.length + a.period_items.length;
}

/** Rows «📅 Перенести невыполненное на завтра» would move: overdue and today's not done whose time has come. */
export function movableRows(a: Agenda, now: number = Date.now()): Row[] {
  return [...a.overdue, ...a.open].filter(
    (r) => r.occurrence_id !== null && r.due_at !== null && Date.parse(r.due_at) <= now && !isDone(r),
  );
}

/** The evening digest's button: what is due and not done today (and the overdue) goes to tomorrow, same time. */
function MoveToTomorrow({ day }: { day: string }) {
  const [busy, setBusy] = useState(false);
  const move = async () => {
    setBusy(true);
    try {
      const res = await api.moveToTomorrow(day);
      haptic.success();
      toast(res.message, res.moved ? "success" : "info");
      itemsChanged();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Section footer="Просроченное и то, что сегодня уже должно было быть сделано, — на завтра, на то же время.">
      <div className="row row--action">
        <button type="button" className="button button--secondary button--wide" disabled={busy} onClick={() => void move()}>
          {busy ? <Spinner small /> : "📅 Перенести невыполненное на завтра"}
        </button>
      </div>
    </Section>
  );
}

function DayAgenda({ agenda, listKey, today, after }: { agenda: Agenda; listKey: string; today: string; after?: ReactNode }) {
  const open = useOpenItem();
  const toggle = useRowToggle(listKey);
  const [showDone, setShowDone] = useState(false);
  return (
    <>
      {agenda.overdue.length > 0 && (
        <Section title={`⚠️ Просрочено · ${agenda.overdue.length}`} tone="danger">
          <RowList rows={agenda.overdue} onOpen={open} onToggle={toggle} overdue />
        </Section>
      )}
      {agenda.open.length > 0 && (
        <Section title="План">
          <RowList rows={agenda.open} onOpen={open} onToggle={toggle} />
        </Section>
      )}
      {after}
      {periodGroups(agenda.period_items).map((g) => (
        <Section
          key={`${g.title}|${g.end}`}
          title={`🗓 ${g.title.charAt(0).toUpperCase()}${g.title.slice(1)}`}
          aside={g.end ? <span className="section-aside">до {fmtDate(g.end, today)}</span> : undefined}
        >
          <RowList rows={g.rows} onOpen={open} onToggle={toggle} />
        </Section>
      ))}
      {agenda.done.length > 0 && (
        <Section
          title={`✅ Сделано · ${agenda.done.length}`}
          aside={
            <button type="button" className="text-button" onClick={() => setShowDone((v) => !v)} aria-expanded={showDone}>
              {showDone ? "Скрыть" : "Показать"}
            </button>
          }
        >
          {showDone && <RowList rows={agenda.done} onOpen={open} onToggle={toggle} />}
        </Section>
      )}
    </>
  );
}

function TodayView({ today }: { today: string }) {
  const nav = useNav();
  const res = useResource("today", (signal) => api.today({ signal }));
  if (!res.data) return res.error ? <ErrorBox error={res.error} onRetry={res.reload} /> : <Loading />;
  const agenda = res.data;
  return (
    <>
      {res.error ? <ErrorBox error={res.error} onRetry={res.reload} /> : null}
      {agendaSize(agenda) === 0 ? (
        <Empty
          emoji="☕"
          title="На сегодня ничего"
          text="Задачи и события со временем на сегодня появятся здесь."
          action={
            <button type="button" className="button" onClick={() => nav.switchTab("add")}>
              Закинуть в свалку
            </button>
          }
        />
      ) : (
        <DayAgenda
          agenda={agenda}
          listKey="today"
          today={today}
          after={movableRows(agenda).length > 0 ? <MoveToTomorrow day={agenda.day} /> : undefined}
        />
      )}
    </>
  );
}

function WeekView({ today }: { today: string }) {
  const open = useOpenItem();
  const toggle = useRowToggle("week");
  const res = useResource("week", (signal) => api.week({ signal }));
  if (!res.data) return res.error ? <ErrorBox error={res.error} onRetry={res.reload} /> : <Loading rows={6} />;
  return (
    <>
      {res.error ? <ErrorBox error={res.error} onRetry={res.reload} /> : null}
      {res.data.days.map((day) => {
        const rows = [...day.overdue, ...day.open, ...day.done];
        return (
          <Section
            key={day.day}
            title={fmtDayHeader(day.day, today)}
            aside={rows.length ? <span className="section-aside">{count(rows.length, "запись", "записи", "записей")}</span> : undefined}
          >
            {rows.length ? (
              <RowList rows={rows} onOpen={open} onToggle={toggle} />
            ) : (
              <p className="muted-row">Свободно</p>
            )}
          </Section>
        );
      })}
    </>
  );
}

export function TodayScreen() {
  const tz = useTz();
  const [mode, setMode] = useState<Mode>("today");
  const today = todayIn(tz);
  return (
    <>
      <ScreenHeader
        title={mode === "today" ? `Сегодня, ${fmtLongDate(today)}` : "Неделя"}
        actions={<RefreshButton onClick={() => markStale(mode)} />}
      />
      <Segmented
        label="Период"
        value={mode}
        onChange={setMode}
        options={[
          { value: "today", label: "Сегодня" },
          { value: "week", label: "7 дней" },
        ]}
      />
      {mode === "today" ? <TodayView today={today} /> : <WeekView today={today} />}
    </>
  );
}
