/**
 * «Карточка»: everything about one item and every edit of the bot — title, details, category, tags, the time (by
 * words through the AI or from the picker, a span, «когда-нибудь»), no reminders, snooze, done / not done, delete /
 * restore, stop repeating, the checklist with «🪄 Разбить на шаги», related items, the source. The item's time is its
 * notification time: the picker sets it, a snooze moves it, «за час до…» goes by words. The snooze chips show once
 * the item is due (its time has come): a snooze never moves an item earlier, so before that they would only add a
 * reminder while saying it moves.
 * An unsaved title / details is a draft (lib/drafts): it comes back after an expired session.
 */
import { useCallback, useContext, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../../api";
import type { ItemDetail, ItemPatch, ItemResponse, Kind, PeriodValue, Step, StepPatch, StepResponse } from "../../api/types";
import { AutoTextarea } from "../../components/AutoTextarea";
import { Chip, Empty, ErrorBox, Loading, ScreenHeader, Section, Spinner } from "../../components/ui";
import { clearDraft, useDraft } from "../../lib/drafts";
import { fmtStamp } from "../../lib/format";
import { ACTIONABLE, SOURCE_LABELS, kindInfo } from "../../lib/kinds";
import { useMainButton } from "../../lib/mainButton";
import { useKinds, useTz } from "../../lib/me";
import { useBackHandler, useNav } from "../../lib/nav";
import { ScreenActive, itemsChanged, setCached, useResource } from "../../lib/store";
import { toast, toastError } from "../../lib/toast";
import { confirmDialog, haptic, openExternal, setClosingConfirmation } from "../../telegram";
import { Gallery } from "./Gallery";
import { KindPicker } from "./KindPicker";
import { Steps } from "./Steps";
import { TagsEditor } from "./TagsEditor";
import { TimeSheet } from "./TimeSheet";
import { useJob, useSplitPolling } from "./useJob";

export const SNOOZE_CHOICES: { label: string; minutes: number | null }[] = [
  { label: "15 мин", minutes: 15 },
  { label: "1 час", minutes: 60 },
  { label: "3 часа", minutes: 180 },
  { label: "завтра", minutes: null },
];
/** The item can be done / not done in the card. */
export function canBeDone(item: ItemDetail): boolean {
  return item.status !== "deleted" && (ACTIONABLE.has(item.kind) || item.occurrence !== null);
}

export function itemDone(item: ItemDetail): boolean {
  return item.status === "done" || item.occurrence?.status === "done";
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

export function CardScreen({ id }: { id: number }) {
  const tz = useTz();
  const nav = useNav();
  const kinds = useKinds();
  const key = `item:${id}`;
  const res = useResource(key, (signal) => api.item(id, { signal }));
  const item = res.data;
  const [busy, setBusy] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [timeOpen, setTimeOpen] = useState(false);
  const draftKey = `card:${id}`;
  const [draft, setDraft] = useDraft<{ title: string; details: string }>(draftKey);
  const { job, start, waitText } = useJob((jobWarnings) => {
    setWarnings(jobWarnings);
    return res.reload();
  });

  const reload = res.reload;
  const apply = useCallback((answer: ItemResponse | StepResponse) => {
    itemsChanged();
    setCached(`item:${answer.item.id}`, answer.item);
    setWarnings(answer.item.warnings);
    haptic.success();
    toast(answer.message, "success");
  }, []);

  const run = useCallback(
    async (name: string, call: () => Promise<ItemResponse>): Promise<boolean> => {
      setBusy(name);
      try {
        apply(await call());
        return true;
      } catch (error) {
        toastError(error);
        if (error instanceof ApiError && error.status === 409) void reload();
        return false;
      } finally {
        setBusy(null);
      }
    },
    [apply, reload],
  );

  // «🪄 Разбить» started elsewhere (the chat): refresh until it ends, while this card is on top.
  const active = useContext(ScreenActive);
  useSplitPolling(Boolean(item?.split_pending) && !job && active, reload);

  const title = draft?.title ?? item?.title ?? "";
  const details = draft?.details ?? item?.details ?? "";
  const dirty = Boolean(item && draft && (draft.title.trim() !== item.title || draft.details.trim() !== (item.details ?? "")));
  const same = Boolean(item && draft && draft.title === item.title && draft.details === (item.details ?? ""));
  useEffect(() => {
    if (same) setDraft(undefined); // typed back to exactly what is saved: nothing to keep
  }, [same, setDraft]);
  const keep = useRef(dirty);
  useEffect(() => {
    keep.current = dirty || !item; // not loaded yet: a restored draft is not known to be clean
  });
  useEffect(() => () => void (keep.current || clearDraft(draftKey)), [draftKey]); // only unsaved changes outlive the card

  const save = async () => {
    if (!item) return;
    const patch: ItemPatch = {};
    if (title.trim() !== item.title) patch.title = title.trim();
    if (details.trim() !== (item.details ?? "")) patch.details = details.trim() || null;
    if (await run("save", () => api.patchItem(id, patch))) setDraft(undefined);
  };

  useMainButton(dirty ? { text: "Сохранить", onClick: () => void save(), progress: busy === "save", disabled: !title.trim() } : null);
  useBackHandler(dirty, () => {
    void confirmDialog("Изменения не сохранены. Выйти без сохранения?").then((leave) => {
      if (!leave) return;
      setDraft(undefined);
      nav.pop();
    });
  });
  useEffect(() => {
    setClosingConfirmation(dirty);
    return () => setClosingConfirmation(false);
  }, [dirty]);

  if (!item) {
    if (res.error instanceof ApiError && res.error.status === 404) {
      return (
        <>
          <ScreenHeader title="Запись" />
          <Empty emoji="🤷" title="Запись не найдена" text="Возможно, её удалили насовсем." />
        </>
      );
    }
    return (
      <>
        <ScreenHeader title="Запись" />
        {res.error ? <ErrorBox error={res.error} onRetry={res.reload} /> : <Loading rows={5} />}
      </>
    );
  }

  const info = kindInfo(item.kind);
  const deleted = item.status === "deleted";
  const done = itemDone(item);
  const locked = deleted || busy !== null;
  const occOpen = item.occurrence?.status === "open" && !item.someday;
  const occDue = occOpen && item.occurrence !== null && Date.parse(item.occurrence.due_at) <= Date.now();
  const pendingReminders = item.reminders.filter((r) => r.status === "pending");
  const sourceText = item.source.text && item.source.text.trim() !== item.title.trim() ? item.source.text : null;
  const allWarnings = [...(item.needs_confirm ? ["🤔 Проверь время — я не уверен, что понял правильно."] : []), ...warnings];

  const setKind = (kind: Kind) => run("kind", () => api.patchItem(id, { kind }));
  const setTags = (tags: string[]) => run("tags", () => api.patchItem(id, { tags }));
  const setPeriod = async (period: PeriodValue) => {
    if (await run("time", () => api.patchItem(id, { period }))) setTimeOpen(false);
  };
  const setSomeday = async (someday: boolean) => {
    if (await run("time", () => api.patchItem(id, { someday }))) setTimeOpen(false);
  };
  const pickTime = async (dueLocal: string) => {
    if (await run("time", () => api.whenAt(id, dueLocal))) setTimeOpen(false);
  };
  const timeByText = (text: string) => {
    setTimeOpen(false);
    void start("edit_time", () => api.whenText(id, text));
  };
  const toggleDone = () => run("done", () => api.action(id, done ? "undone" : "done"));
  const snooze = (minutes: number | null) =>
    run(`snooze:${minutes}`, () => api.snooze(id, minutes === null ? { tomorrow: true } : { minutes }));
  const remove = async () => {
    const withSteps = item.steps.length ? " Шаги удалятся вместе с ней." : "";
    if (await confirmDialog(`Удалить «${item.title}»?${withSteps}`)) await run("delete", () => api.action(id, "delete"));
  };
  const stopRepeat = async () => {
    if (await confirmDialog("Остановить повтор? Следующих напоминаний не будет, уже наступивший раз останется.")) await run("stoprec", () => api.action(id, "stoprec"));
  };

  const toggleStep = async (step: Step) => {
    haptic.tap();
    setCached<ItemDetail>(key, (prev) =>
      prev && {
        ...prev,
        steps: prev.steps.map((s) => (s.id === step.id ? { ...s, done: !step.done } : s)),
        steps_done: prev.steps_done + (step.done ? -1 : 1),
      },
    );
    try {
      apply(await api.toggleStep(step.id, !step.done));
    } catch (error) {
      toastError(error);
      void reload();
    }
  };
  const addStep = (stepTitle: string) => run("step", () => api.addStep(id, { title: stepTitle }));
  const saveStep = (step: Step, patch: StepPatch) => run("step", () => api.patchStep(step.id, patch));
  const deleteStep = async (step: Step) =>
    (await confirmDialog(`Удалить шаг «${step.title}»?`)) && run("step", () => api.deleteStep(step.id));

  return (
    <>
      <ScreenHeader title={`${info.emoji} ${info.label}`} />

      {deleted && (
        <div className="banner banner--danger">
          <span>🗑 Удалено{item.deleted_at ? ` ${fmtStamp(item.deleted_at, tz)}` : ""}</span>
          <button type="button" className="button button--small" disabled={busy !== null} onClick={() => run("restore", () => api.action(id, "restore"))}>
            ↩️ Вернуть
          </button>
        </div>
      )}

      {item.parent && (
        <button type="button" className="parent-link" onClick={() => nav.push({ name: "item", id: item.parent!.id })}>
          ↳ шаг из «{item.parent.title}»
        </button>
      )}

      <Section>
        <AutoTextarea
          className={done ? "title-input title-input--done" : "title-input"}
          value={title}
          maxLength={200}
          disabled={deleted}
          aria-label="Название"
          onChange={(e) => setDraft({ title: e.target.value.replace(/\n/g, " "), details })}
        />
        <AutoTextarea
          className="details-input"
          value={details}
          maxLength={4000}
          disabled={deleted}
          placeholder="Подробности…"
          aria-label="Подробности"
          onChange={(e) => setDraft({ title, details: e.target.value })}
        />
        {dirty && (
          <div className="row row--action draft-actions">
            <button type="button" className="text-button" onClick={() => setDraft(undefined)}>
              Отменить
            </button>
            <span className="field-hint">Сохранить — кнопкой внизу</span>
          </div>
        )}
      </Section>

      {allWarnings.length > 0 && (
        <div className="banner banner--warn" role="status">
          {allWarnings.map((w) => (
            <span key={w}>{w}</span>
          ))}
        </div>
      )}

      {!deleted && (
        <Section title="Время">
          <div className="row row--static">
            <span className="when-line">{item.when_text ?? (canBeDone(item) ? "Без срока" : "Без времени")}</span>
          </div>
          {item.reminders_off && item.occurrence !== null && !item.someday && (
            <div className="row row--static">
              <span className="field-hint">Напоминания выключены — новое время включит их снова.</span>
            </div>
          )}
          {item.repeat_text && (
            <div className="row row--static">
              <span>
                🔁 {item.repeat_text}
                {!item.rec_active && <span className="field-hint"> · повтор остановлен</span>}
              </span>
            </div>
          )}
          {job?.kind === "edit_time" && (
            <div className="row row--static">
              <span className="inline-status">
                <Spinner small /> {waitText ?? "Разбираю время…"}
              </span>
            </div>
          )}
          <div className="button-row">
            <button type="button" className="button button--secondary" disabled={locked || job !== null} onClick={() => setTimeOpen(true)}>
              🕒 Время
            </button>
            {canBeDone(item) && (
              <button type="button" className={done ? "button button--secondary" : "button"} disabled={locked} onClick={toggleDone}>
                {busy === "done" ? <Spinner small /> : done ? "↩️ Не сделано" : "✅ Готово"}
              </button>
            )}
          </div>
          {occDue && (
            <div className="field-group field-group--inset">
              <span className="field-label">⏰ Отложить — запись переедет на это время</span>
              <div className="chips">
                {SNOOZE_CHOICES.map((c) => (
                  <Chip key={c.label} onClick={() => snooze(c.minutes)} disabled={locked}>
                    {c.label}
                  </Chip>
                ))}
              </div>
            </div>
          )}
          {(pendingReminders.length > 0 || item.rec_active) && (
            <div className="button-row">
              {pendingReminders.length > 0 && !item.reminders_off && (
                <button type="button" className="button button--secondary" disabled={locked} onClick={() => run("norem", () => api.action(id, "norem"))}>
                  🔕 Без напоминаний
                </button>
              )}
              {item.rec_active && (
                <button type="button" className="button button--secondary" disabled={locked} onClick={stopRepeat}>
                  ⏹ Стоп повтор
                </button>
              )}
            </div>
          )}
        </Section>
      )}

      <Steps
        item={item}
        tz={tz}
        busy={busy !== null}
        splitStatus={job?.kind === "split" ? (waitText ?? "Разбиваю на шаги…") : null}
        onToggle={toggleStep}
        onAdd={addStep}
        onSave={saveStep}
        onDelete={deleteStep}
        onOpen={(step) => nav.push({ name: "item", id: step.id })}
        onSplit={() => void start("split", () => api.split(id))}
      />

      {!deleted && (
        <Section title="Категория">
          <KindPicker kinds={kinds} value={item.kind} disabled={locked} onChange={setKind} />
        </Section>
      )}

      <Section title="Теги">
        <div className="chips-padded">
          <TagsEditor tags={item.tags} disabled={locked} onChange={setTags} />
        </div>
      </Section>

      {item.images.length > 0 && (
        <Section title="Картинки">
          <Gallery images={item.images} title={item.title} />
        </Section>
      )}

      {item.urls.length > 0 && (
        <Section title="Ссылки">
          {item.urls.map((url) => (
            <button key={url} type="button" className="link-row" onClick={() => openExternal(url)}>
              <span className="row-emoji" aria-hidden="true">
                🔗
              </span>
              <span className="link-row-text">
                <span>{item.link_titles[url] ?? hostOf(url)}</span>
                <span className="field-hint link-url">{url}</span>
              </span>
            </button>
          ))}
        </Section>
      )}

      {item.related.length > 0 && (
        <Section title="🔗 Похоже на">
          {item.related.map((r) => (
            <button key={r.id} type="button" className="link-row" onClick={() => nav.push({ name: "item", id: r.id })}>
              <span className="row-emoji" aria-hidden="true">
                {kindInfo(r.kind).emoji}
              </span>
              <span className="link-row-text">
                <span>{r.title}</span>
                <span className="field-hint">{fmtStamp(r.created_at, tz)}</span>
              </span>
            </button>
          ))}
        </Section>
      )}

      <Section title="Откуда">
        <div className="row row--static">
          <span className="field-hint">
            {SOURCE_LABELS[item.source.kind] ?? "Сообщение"}
            {item.source.from_app ? " из приложения" : ""}
            {item.source.message_date ? ` · ${fmtStamp(item.source.message_date, tz)}` : ""}
          </span>
        </div>
        {item.source.forward && (
          <div className="row row--static">
            {item.source.forward.url ? (
              <button type="button" className="text-button" onClick={() => openExternal(item.source.forward!.url!)}>
                ↪️ {item.source.forward.name ?? "Источник"}
              </button>
            ) : (
              <span>↪️ {item.source.forward.name ?? "Переслано"}</span>
            )}
          </div>
        )}
        {sourceText && (
          <details className="source-text">
            <summary>Исходный текст</summary>
            <p>{sourceText}</p>
          </details>
        )}
      </Section>

      {!deleted && (
        <div className="danger-zone">
          <button type="button" className="button button--danger-text" disabled={locked} onClick={remove}>
            🗑 Удалить
          </button>
        </div>
      )}
      <p className="footnote">
        Создано {fmtStamp(item.created_at, tz)}
        {fmtStamp(item.updated_at, tz) !== fmtStamp(item.created_at, tz) ? ` · изменено ${fmtStamp(item.updated_at, tz)}` : ""}
        {item.done_at ? ` · сделано ${fmtStamp(item.done_at, tz)}` : ""}
      </p>

      <TimeSheet
        open={timeOpen}
        item={item}
        tz={tz}
        busy={busy !== null}
        onClose={() => setTimeOpen(false)}
        onText={timeByText}
        onPick={pickTime}
        onPeriod={setPeriod}
        onSomeday={setSomeday}
      />
    </>
  );
}
