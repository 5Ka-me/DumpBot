/** «Ещё»: questions, the trash, settings, AI spend. */
import { LinkRow, ScreenHeader, Section } from "../components/ui";
import { useMe } from "../lib/me";
import { useNav } from "../lib/nav";

export function MoreScreen() {
  const nav = useNav();
  const me = useMe();
  return (
    <>
      <ScreenHeader title="Ещё" />
      <Section>
        <LinkRow emoji="❓" label="Спросить" hint="вопрос к сохранённому" onClick={() => nav.push({ name: "ask" })} />
        <LinkRow emoji="🗑" label="Корзина" hint="удалённые записи" onClick={() => nav.push({ name: "feed", status: "deleted" })} />
      </Section>
      <Section>
        <LinkRow emoji="⚙️" label="Настройки" hint="время, тихие часы, дайджесты, бюджет, голос" onClick={() => nav.push({ name: "settings" })} />
        <LinkRow emoji="💰" label="Расходы" hint="траты на ИИ за месяц" onClick={() => nav.push({ name: "usage" })} />
      </Section>
      <p className="footnote">
        Записи из приложения проходят тот же разбор ИИ, что и сообщения боту, но в чат не дублируются.
        {me ? ` Часовой пояс: ${me.tz}.` : ""}
      </p>
    </>
  );
}
