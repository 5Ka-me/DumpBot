/** Words for background AI jobs: why one waits, how it ended. */
import type { JobStatus } from "../api/types";

/** What the bot is doing with a job right now (null: nothing to say). */
export function jobWaitText(job: Pick<JobStatus, "status" | "reason"> | null | undefined): string | null {
  if (!job) return null;
  if (job.status === "dead") return "😢 Не получилось — попробуй ещё раз.";
  if (job.status === "done") return null;
  switch (job.reason) {
    case "budget":
      return "⛔ Бюджет на ИИ исчерпан — сделаю, когда он появится (или подними лимит в настройках).";
    case "ai_unavailable":
      return "✍ ИИ сейчас недоступен — повторю позже.";
    case "ai_config":
      return "⚠️ chutes.ai отклоняет запросы — проверь ключ и баланс в .env.";
    case "error":
      return "Не вышло с первого раза — пробую ещё.";
    default:
      return job.status === "running" ? "Думаю…" : "В очереди…";
  }
}

/** The job waits for something that will not resolve in a minute (stop polling, offer a refresh). */
export function jobStuck(job: Pick<JobStatus, "status" | "reason"> | null | undefined): boolean {
  return Boolean(job && job.status === "queued" && (job.reason === "budget" || job.reason === "ai_unavailable" || job.reason === "ai_config"));
}
