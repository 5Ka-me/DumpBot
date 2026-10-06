/** «Расходы»: this month's AI spend against the budget, by purpose (as /usage). */
import { api } from "../api";
import type { Usage } from "../api/types";
import { ErrorBox, Loading, ScreenHeader, Section } from "../components/ui";
import { capitalize, count, fmtMoney } from "../lib/format";
import { useNav } from "../lib/nav";
import { useResource } from "../lib/store";

/** Share of the budget for the meter, 0…1 (a zero budget with spending is full). */
export function usageRatio(u: Pick<Usage, "ratio" | "exhausted">): number {
  if (u.ratio === null) return 1;
  return Math.max(0, Math.min(1, u.ratio));
}

export function UsageScreen() {
  const nav = useNav();
  const res = useResource("usage", (signal) => api.usage({ signal }));
  const u = res.data;
  return (
    <>
      <ScreenHeader title="Расходы на ИИ" />
      {!u ? (
        res.error ? (
          <ErrorBox error={res.error} onRetry={res.reload} />
        ) : (
          <Loading rows={3} />
        )
      ) : (
        <>
          <Section title={capitalize(u.month)}>
            <div className="usage-total">
              <span className="usage-spent">{fmtMoney(u.spent_usd)}</span>
              <span className="field-hint">из {fmtMoney(u.limit_usd)}</span>
            </div>
            <div
              className={u.exhausted ? "meter meter--full" : usageRatio(u) >= 0.8 ? "meter meter--warn" : "meter"}
              role="meter"
              aria-label="Израсходовано бюджета"
              aria-valuemin={0}
              aria-valuemax={1}
              aria-valuenow={usageRatio(u)}
            >
              <span style={{ width: `${Math.round(usageRatio(u) * 100)}%` }} />
            </div>
            <p className="muted-row">
              {count(u.calls, "вызов", "вызова", "вызовов")} ИИ
              {u.exhausted && " · ⛔ бюджет исчерпан: сообщения ждут следующего месяца или нового лимита"}
            </p>
          </Section>
          {u.by_purpose.length > 0 && (
            <Section title="На что">
              {u.by_purpose.map((p) => (
                <div key={p.purpose} className="row row--static usage-row">
                  <span>{capitalize(p.label)}</span>
                  <span className="field-hint">{count(p.calls, "вызов", "вызова", "вызовов")}</span>
                  <span className="usage-usd">{fmtMoney(p.usd)}</span>
                </div>
              ))}
            </Section>
          )}
          <div className="button-row button-row--padded">
            <button type="button" className="button button--secondary" onClick={() => nav.push({ name: "settings" })}>
              Изменить бюджет
            </button>
          </div>
        </>
      )}
    </>
  );
}
