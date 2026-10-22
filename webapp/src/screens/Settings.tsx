/**
 * «Настройки»: every preference of the bot (as /settings), checked and saved together by the MainButton. Unsaved
 * changes are a draft (lib/drafts): they come back after an expired session.
 */
import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../api";
import type { Prefs, SettingsPatch, SettingsResponse } from "../api/types";
import { ErrorBox, Loading, ScreenHeader, Section, Segmented, Switch } from "../components/ui";
import { clearDraft, getDraft, setDraft as keepDraft } from "../lib/drafts";
import { parseClock } from "../lib/format";
import { useMainButton } from "../lib/mainButton";
import { MeContext } from "../lib/me";
import { useBackHandler, useNav } from "../lib/nav";
import { markStale, setCached, useResource } from "../lib/store";
import { toast, toastError } from "../lib/toast";
import { confirmDialog, haptic, setClosingConfirmation } from "../telegram";

type NumberKeys = { [K in keyof Prefs]: Prefs[K] extends number ? K : never }[keyof Prefs];
/** The form: numbers as typed text. */
export type Draft = { [K in keyof Prefs]: Prefs[K] extends number ? string : Prefs[K] };

const INT_LIMITS: Partial<Record<NumberKeys, [number, number]>> = {
  soon_days: [1, 14],
  missed_grace_minutes: [1, 1_000_000],
};
const TIME_KEYS = [
  "slot_morning",
  "slot_lunch",
  "slot_afternoon",
  "slot_evening",
  "slot_after_work",
  "slot_night",
  "weekend_time",
  "bare_date_time",
  "task_default_time",
  "quiet_start",
  "quiet_end",
  "night_cutoff",
  "digest_morning_time",
  "digest_evening_time",
  "digest_weekly_time",
] as const satisfies readonly (keyof Prefs)[];
const SETTINGS_DRAFT = "settings";
const TZ_FALLBACK = ["Europe/Warsaw", "Europe/Berlin", "Europe/Minsk", "Europe/Kyiv", "Europe/Moscow", "Europe/London"];

export function toDraft(prefs: Prefs): Draft {
  return {
    ...prefs,
    soon_days: String(prefs.soon_days),
    missed_grace_minutes: String(prefs.missed_grace_minutes),
    budget_usd: String(prefs.budget_usd),
  };
}

/** The fields of `draft` that differ from `prefs` as typed (what an unsaved form keeps). */
export function changedFields(prefs: Prefs, draft: Draft): Partial<Draft> {
  const saved = toDraft(prefs);
  return Object.fromEntries(Object.entries(draft).filter(([key, value]) => saved[key as keyof Draft] !== value));
}

/** The changed preferences of `draft` against `prefs`, or the errors of fields that do not parse. */
export function diffPrefs(prefs: Prefs, draft: Draft): { patch: SettingsPatch; errors: Record<string, string> } {
  const patch: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const key of Object.keys(prefs) as (keyof Prefs)[]) {
    const value = draft[key];
    let parsed: unknown = value;
    if (key in INT_LIMITS) {
      const [min, max] = INT_LIMITS[key as NumberKeys]!;
      const text = String(value).trim();
      const n = Number(text);
      if (!/^\d+$/.test(text) || n < min || n > max) {
        errors[key] = `Целое число от ${min} до ${max}`;
        continue;
      }
      parsed = n;
    } else if (key === "budget_usd") {
      const n = Number(String(value).trim().replace(",", "."));
      if (!String(value).trim() || !Number.isFinite(n) || n < 0) {
        errors[key] = "Сумма в долларах: 0 или больше";
        continue;
      }
      parsed = Math.round(n * 100) / 100;
    } else if ((TIME_KEYS as readonly string[]).includes(key)) {
      const clock = parseClock(String(value));
      if (!clock) {
        errors[key] = "Время ЧЧ:ММ";
        continue;
      }
      parsed = clock;
    } else if (key === "tz") {
      parsed = String(value).trim();
      if (!parsed) {
        errors[key] = "Укажи часовой пояс";
        continue;
      }
    }
    if (parsed !== prefs[key]) patch[key] = parsed;
  }
  return { patch: patch as SettingsPatch, errors };
}

function timeZones(): string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
  try {
    return intl.supportedValuesOf?.("timeZone") ?? TZ_FALLBACK;
  } catch {
    return TZ_FALLBACK;
  }
}

function FieldError({ text }: { text?: string }) {
  return text ? (
    <span className="field-error" role="alert">
      {text}
    </span>
  ) : null;
}

function TimeField({
  label,
  hint,
  value,
  error,
  disabled,
  onChange,
}: {
  label: string;
  hint?: string;
  value: string;
  error?: string;
  disabled?: boolean;
  onChange: (v: string) => void;
}) {
  return (
    <label className="field field--row">
      <span className="field-text">
        <span className="field-label">{label}</span>
        {hint && <span className="field-hint">{hint}</span>}
        <FieldError text={error} />
      </span>
      <input
        className="input input--time"
        type="time"
        value={value}
        disabled={disabled}
        aria-invalid={Boolean(error)}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

function NumberField({
  label,
  hint,
  value,
  error,
  unit,
  step,
  onChange,
}: {
  label: string;
  hint?: string;
  value: string;
  error?: string;
  unit: string;
  step?: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="field field--row">
      <span className="field-text">
        <span className="field-label">{label}</span>
        {hint && <span className="field-hint">{hint}</span>}
        <FieldError text={error} />
      </span>
      <span className="number-input">
        <input
          className="input input--number"
          type="text"
          inputMode={step ? "decimal" : "numeric"}
          value={value}
          aria-invalid={Boolean(error)}
          onChange={(e) => onChange(e.target.value)}
        />
        <span className="field-hint">{unit}</span>
      </span>
    </label>
  );
}

function SettingsForm({ data, onSaved }: { data: SettingsResponse; onSaved: (next: SettingsResponse) => void }) {
  const nav = useNav();
  const [draft, setDraft] = useState<Draft>(() => ({ ...toDraft(data.prefs), ...getDraft<Partial<Draft>>(SETTINGS_DRAFT) }));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const zones = useMemo(timeZones, []);
  const diff = diffPrefs(data.prefs, draft);
  const dirty = Object.keys(diff.patch).length > 0 || Object.keys(diff.errors).length > 0;

  // Fresh prefs from the bot (changed in the chat meanwhile) replace an untouched form only.
  const base = useRef(data.prefs);
  useEffect(() => {
    if (data.prefs === base.current) return;
    const untouched = Object.keys(diffPrefs(base.current, draft).patch).length === 0;
    base.current = data.prefs;
    if (untouched) setDraft(toDraft(data.prefs));
    // the draft is read only at the moment the prefs change
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.prefs]);
  useEffect(() => {
    setClosingConfirmation(dirty);
    return () => setClosingConfirmation(false);
  }, [dirty]);
  useEffect(() => {
    const changed = changedFields(data.prefs, draft);
    keepDraft(SETTINGS_DRAFT, Object.keys(changed).length ? changed : undefined);
  }, [data.prefs, draft]);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((d) => ({ ...d, [key]: value }));
    setErrors((e) => {
      if (!(key in e)) return e;
      const rest = { ...e };
      delete rest[key];
      return rest;
    });
  };

  const save = async () => {
    const result = diffPrefs(data.prefs, draft);
    if (Object.keys(result.errors).length) {
      setErrors(result.errors);
      haptic.error();
      toast("Проверь отмеченные поля", "error");
      return;
    }
    if (!Object.keys(result.patch).length) return;
    setSaving(true);
    try {
      const next = await api.patchSettings(result.patch);
      setDraft(toDraft(next.prefs));
      haptic.success();
      toast("✅ Сохранено", "success");
      onSaved(next);
    } catch (error) {
      if (error instanceof ApiError && error.field) setErrors({ [error.field]: error.message });
      toastError(error);
    } finally {
      setSaving(false);
    }
  };

  useMainButton(dirty ? { text: "Сохранить", onClick: () => void save(), progress: saving } : null);
  useBackHandler(dirty, () => {
    void confirmDialog("Настройки не сохранены. Выйти без сохранения?").then((leave) => {
      if (!leave) return;
      clearDraft(SETTINGS_DRAFT);
      nav.pop();
    });
  });

  const time = (key: (typeof TIME_KEYS)[number], label: string, hint?: string, disabled?: boolean) => (
    <TimeField label={label} hint={hint} value={draft[key]} error={errors[key]} disabled={disabled} onChange={(v) => set(key, v)} />
  );

  return (
    <>
      <Section title="🌍 Основное">
        <label className="field">
          <span className="field-label">Часовой пояс</span>
          <input
            className="input"
            list="tz-list"
            value={draft.tz}
            aria-invalid={Boolean(errors.tz)}
            autoCapitalize="none"
            autoCorrect="off"
            onChange={(e) => set("tz", e.target.value)}
          />
          <datalist id="tz-list">
            {zones.map((z) => (
              <option key={z} value={z} />
            ))}
          </datalist>
          <FieldError text={errors.tz} />
        </label>
        {time("task_default_time", "Задача без времени", "когда напомнить, если время не сказано")}
        <NumberField
          label="«На днях» — это через"
          value={draft.soon_days}
          error={errors.soon_days}
          unit="дн"
          onChange={(v) => set("soon_days", v)}
        />
      </Section>

      <Section title="🕘 Время суток" footer="Как понимать «утром», «в обед», «вечером», «на выходных» и дату без времени.">
        {time("slot_morning", "Утро")}
        {time("slot_lunch", "Обед")}
        {time("slot_afternoon", "День")}
        {time("slot_after_work", "После работы")}
        {time("slot_evening", "Вечер")}
        {time("slot_night", "Ночь")}
        {time("weekend_time", "На выходных", "суббота")}
        {time("bare_date_time", "Дата без времени", "«завтра», «в пятницу»")}
      </Section>

      <Section title="🌙 Тихие часы" footer="В тихие часы приходят только напоминания с явно заданным временем.">
        <Switch label="Тихие часы" checked={draft.quiet_enabled} onChange={(v) => set("quiet_enabled", v)} />
        {time("quiet_start", "Начало", undefined, !draft.quiet_enabled)}
        {time("quiet_end", "Конец", undefined, !draft.quiet_enabled)}
        {time("night_cutoff", "Граница ночи", "до этого времени «завтра» — это наступивший день")}
      </Section>

      <Section title="📰 Дайджесты">
        <Switch label="Утренний план" checked={draft.digest_morning_enabled} onChange={(v) => set("digest_morning_enabled", v)} />
        {time("digest_morning_time", "Время", undefined, !draft.digest_morning_enabled)}
        <Switch label="Вечерние итоги" checked={draft.digest_evening_enabled} onChange={(v) => set("digest_evening_enabled", v)} />
        {time("digest_evening_time", "Время", undefined, !draft.digest_evening_enabled)}
        <Switch label="Обзор недели" checked={draft.digest_weekly_enabled} onChange={(v) => set("digest_weekly_enabled", v)} />
        {time("digest_weekly_time", "Время", "по воскресеньям", !draft.digest_weekly_enabled)}
      </Section>

      <Section title="💰 Бюджет на ИИ" footer="0 — ИИ выключен. При исчерпании сообщения ждут следующего месяца.">
        <NumberField
          label="В месяц"
          value={draft.budget_usd}
          error={errors.budget_usd}
          unit="$"
          step="0.01"
          onChange={(v) => set("budget_usd", v)}
        />
        <button type="button" className="link-row link-row--plain" onClick={() => nav.push({ name: "usage" })}>
          <span className="link-row-text">Расходы за месяц →</span>
        </button>
      </Section>

      <Section title="🎙 Голос">
        <div className="field">
          <span className="field-label">Язык голосовых</span>
          <Segmented
            label="Язык голосовых"
            value={draft.stt_language ?? "auto"}
            onChange={(v) => set("stt_language", v === "auto" ? null : v)}
            options={[
              { value: "auto", label: "Авто" },
              { value: "ru", label: "Русский" },
              { value: "en", label: "English" },
            ]}
          />
        </div>
        <fieldset className="field radio-group">
          <legend className="field-label">Распознавание</legend>
          {(
            [
              ["auto", "🔀 Авто", "chutes, при сбое — локальный"],
              ["local", `💻 Локальный${data.local_stt ? "" : " (не настроен)"}`, "при сбое — chutes"],
              ["chutes", "☁️ Только chutes", "локальный не используется"],
            ] as const
          ).map(([value, label, hint]) => (
            <label key={value} className="radio">
              <input type="radio" name="stt_mode" checked={draft.stt_mode === value} onChange={() => set("stt_mode", value)} />
              <span className="field-text">
                <span>{label}</span>
                <span className="field-hint">{hint}</span>
              </span>
            </label>
          ))}
          {!data.local_stt && (
            <span className="field-hint">Без LOCAL_STT_URL все режимы работают как «Только chutes» (README, «Локальный Whisper»).</span>
          )}
        </fieldset>
      </Section>

      <Section title="⚙️ Дополнительно">
        <NumberField
          label="Не присылать опоздавшие напоминания старше"
          hint="например, после выключения компьютера"
          value={draft.missed_grace_minutes}
          error={errors.missed_grace_minutes}
          unit="мин"
          onChange={(v) => set("missed_grace_minutes", v)}
        />
      </Section>
      <p className="footnote">Изменения действуют для новых напоминаний — уже созданные не перепланируются.</p>
    </>
  );
}

export function SettingsScreen() {
  const { setTz } = useContext(MeContext);
  const res = useResource("settings", (signal) => api.settings({ signal }));
  return (
    <>
      <ScreenHeader title="Настройки" />
      {res.data ? (
        <SettingsForm
          data={res.data}
          onSaved={(next) => {
            if (next.prefs.tz !== res.data?.prefs.tz) {
              setTz(next.prefs.tz);
              markStale("");
            }
            markStale("usage");
            setCached("settings", next);
          }}
        />
      ) : res.error ? (
        <ErrorBox error={res.error} onRetry={res.reload} />
      ) : (
        <Loading rows={8} />
      )}
    </>
  );
}
