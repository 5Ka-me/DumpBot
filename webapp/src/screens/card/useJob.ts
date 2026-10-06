/** Runs an AI job of a card ("🕒 Время" by words, "🪄 Разбить на шаги") and follows it to the end. */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, isAbort, pollJob } from "../../api";
import type { EditTimeResult, JobAccepted, JobStatus, SplitResult } from "../../api/types";
import { count } from "../../lib/format";
import { jobWaitText } from "../../lib/jobs";
import { itemsChanged } from "../../lib/store";
import { toast, toastError } from "../../lib/toast";
import { haptic } from "../../telegram";

export type JobKind = "edit_time" | "split";

export interface RunningJob {
  kind: JobKind;
  status: JobStatus | null;
}

/** What to say when a job ended; `ok` false: a refusal ("не понял время"). */
export function jobOutcome(kind: JobKind, status: JobStatus): { ok: boolean; text: string; warnings: string[] } {
  if (status.status === "dead") return { ok: false, text: "😢 Не получилось — попробуй ещё раз.", warnings: [] };
  if (status.status !== "done") {
    return { ok: false, text: jobWaitText(status) ?? "Ещё в очереди — бот закончит позже.", warnings: [] };
  }
  const result = (status.result ?? {}) as Partial<EditTimeResult & SplitResult>;
  if (result.ok === false) {
    return { ok: false, text: result.message ?? "😢 Не получилось — попробуй ещё раз.", warnings: [] };
  }
  if (kind === "split") {
    const steps = typeof result.steps === "number" ? result.steps : 0;
    return { ok: true, text: steps ? `🪄 Готово: ${count(steps, "шаг", "шага", "шагов")}` : "🪄 Готово", warnings: [] };
  }
  return { ok: true, text: "🕒 Время изменено", warnings: Array.isArray(result.warnings) ? result.warnings : [] };
}

export function useJob(onFinish: (warnings: string[]) => Promise<void> | void) {
  const [job, setJob] = useState<RunningJob | null>(null);
  const controller = useRef<AbortController | null>(null);
  const finish = useRef(onFinish);
  useEffect(() => {
    finish.current = onFinish;
  });
  useEffect(() => () => controller.current?.abort(), []);

  const start = useCallback(async (kind: JobKind, accept: () => Promise<JobAccepted>) => {
    controller.current?.abort();
    const ctrl = new AbortController();
    controller.current = ctrl;
    setJob({ kind, status: null });
    try {
      const accepted = await accept();
      const final = await pollJob(api, accepted.job_id, {
        signal: ctrl.signal,
        timeoutMs: 90_000,
        onUpdate: (status) => setJob({ kind, status }),
      });
      const outcome = jobOutcome(kind, final);
      itemsChanged();
      await finish.current(outcome.warnings);
      if (outcome.ok) haptic.success();
      else haptic.warning();
      toast(outcome.text, outcome.ok ? "success" : final.status === "done" || final.status === "dead" ? "error" : "info");
    } catch (error) {
      if (!isAbort(error)) toastError(error);
    } finally {
      if (controller.current === ctrl) setJob(null);
    }
  }, []);

  return { job, start, waitText: job ? (jobWaitText(job.status ?? { status: "queued", reason: null }) ?? null) : null };
}
