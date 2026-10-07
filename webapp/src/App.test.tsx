/** The whole app against the dev mock API (mock/api.ts) in place of the bot. */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App, DRAFTS_KEPT_TEXT } from "./App";
import { getDraft, resetDrafts } from "./lib/drafts";
import { resetInbox } from "./lib/inbox";
import { resetStore } from "./lib/store";
import { resetToasts } from "./lib/toast";
import { useMockApi } from "./test/mockFetch";

beforeEach(() => {
  resetStore();
  resetInbox();
  resetToasts();
  resetDrafts();
});

function expiredFetch() {
  vi.stubGlobal("fetch", async () =>
    new Response(JSON.stringify({ error: { code: "expired", message: "Сессия устарела — закрой приложение и открой его снова." } }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("outside Telegram", () => {
  it("explains how to open the app and asks the API nothing", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    render(<App outside />);
    expect(screen.getByText(/нажми кнопку «📱 Свалка»/)).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("signed out", () => {
  it("asks to reopen the app when the session expired", async () => {
    vi.stubGlobal("fetch", async () =>
      new Response(JSON.stringify({ error: { code: "expired", message: "Сессия устарела — закрой приложение и открой его снова." } }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<App outside={false} />);
    expect(await screen.findByRole("heading", { name: "Сессия устарела" })).toBeInTheDocument();
    expect(screen.getByText("Сессия устарела — закрой приложение и открой его снова.")).toBeInTheDocument();
  });

  it("offers a retry when the bot cannot be reached", async () => {
    vi.stubGlobal("fetch", async () => Promise.reject(new TypeError("Failed to fetch")));
    render(<App outside={false} />);
    expect(await screen.findByText(/Нет связи с ботом/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
  });
});

describe("with the mock API", () => {
  it("shows today, signs every request and ticks a task optimistically", async () => {
    const { calls } = useMockApi();
    render(<App outside={false} />);
    expect(await screen.findByText("Позвонить в банк")).toBeInTheDocument();
    expect(screen.getByText("Оплатить интернет")).toBeInTheDocument();
    expect(calls.every((c) => c.authorization === "tma dev")).toBe(true);

    const box = screen.getByRole("checkbox", { name: "Отметить сделанным: Позвонить в банк" });
    fireEvent.click(box);
    expect(box).toHaveAttribute("aria-checked", "true"); // before the answer
    expect(await screen.findByText("✅ Готово")).toBeInTheDocument();
    const done = calls.find((c) => c.method === "POST" && c.path === "/api/items/1/done");
    expect(done?.body).toEqual({ occurrence_id: expect.any(Number) }); // exactly this row's occurrence
  });

  it("moves what is left of today to tomorrow, like the evening digest's button", async () => {
    const { calls } = useMockApi();
    render(<App outside={false} />);
    expect(await screen.findByText("Оплатить интернет")).toBeInTheDocument(); // overdue since yesterday
    fireEvent.click(screen.getByRole("button", { name: "📅 Перенести невыполненное на завтра" }));
    expect(await screen.findByText(/^📅 Перенёс на завтра: \d+$/)).toBeInTheDocument();
    const move = calls.find((c) => c.path === "/api/today/move");
    expect(move?.method).toBe("POST");
    expect(move?.body).toEqual({ day: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/) });
    await waitFor(() => expect(screen.queryByText("Оплатить интернет")).toBeNull());
    expect(screen.queryByRole("button", { name: "📅 Перенести невыполненное на завтра" })).toBeNull();
  });

  it("keeps the text being added when another tab is opened", async () => {
    useMockApi();
    render(<App outside={false} />);
    await screen.findByText("Позвонить в банк");
    fireEvent.click(screen.getByRole("button", { name: "Добавить" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Что закинуть" }), { target: { value: "длинная заметка про отпуск" } });
    fireEvent.click(screen.getByRole("button", { name: "Задачи" }));
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Что закинуть" })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Добавить" }));
    expect(await screen.findByRole("textbox", { name: "Что закинуть" })).toHaveValue("длинная заметка про отпуск");
  });

  it("keeps unsaved text for the reopened app when the session expires", async () => {
    useMockApi();
    render(<App outside={false} />);
    await screen.findByText("Позвонить в банк");
    fireEvent.click(await screen.findByText("Купить наушники Sony"));
    fireEvent.change(await screen.findByRole("textbox", { name: "Подробности" }), { target: { value: "XM5, чёрные" } });
    expiredFetch();
    fireEvent.click(screen.getByRole("button", { name: "✅ Готово" })); // a request in the expired session
    expect(await screen.findByRole("heading", { name: "Сессия устарела" })).toBeInTheDocument();
    expect(screen.getByText(new RegExp(DRAFTS_KEPT_TEXT))).toBeInTheDocument();
    expect(getDraft("card:3")).toEqual({ title: "Купить наушники Sony", details: "XM5, чёрные" });
  });

  it("opens a card from a list and goes back", async () => {
    useMockApi();
    render(<App outside={false} />);
    fireEvent.click(await screen.findByText("Купить наушники Sony"));
    expect(await screen.findByRole("textbox", { name: "Название" })).toHaveValue("Купить наушники Sony");
    expect(screen.getByText("Наушники для бега")).toBeInTheDocument(); // «🔗 Похоже на»
    fireEvent.click(screen.getByRole("button", { name: "Назад" }));
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Название" })).toBeNull());
  });

  it("shows the item's notification time, no «напомнить за», and a snooze moves the time", async () => {
    const { api } = useMockApi();
    render(<App outside={false} />);
    await screen.findByText("Позвонить в банк");
    fireEvent.click(screen.getByRole("button", { name: "Задачи" }));
    // "стоматолог завтра в 10:30, напомни за час и за 15 минут": notified at 09:30 and 10:15
    fireEvent.click(await screen.findByText("Стоматолог (10:30)"));
    expect(await screen.findByText("🔔 завтра 09:30 · завтра 10:15")).toBeInTheDocument();
    expect(screen.queryByText("Напомнить")).toBeNull();
    expect(screen.queryByText(/как в настройках/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "завтра" })); // tomorrow at its time of day: one notification
    expect(await screen.findByText("⏰ Напомню завтра 09:30")).toBeInTheDocument();
    expect(await screen.findByText("🔔 завтра 09:30")).toBeInTheDocument();
    expect(api.db.get(2)!.later).toEqual([]);
  });

  it("renames an item with the main button", async () => {
    const { api } = useMockApi();
    render(<App outside={false} />);
    fireEvent.click(await screen.findByText("Купить наушники Sony"));
    const title = await screen.findByRole("textbox", { name: "Название" });
    fireEvent.change(title, { target: { value: "Купить наушники Sony XM5" } });
    fireEvent.click(await screen.findByRole("button", { name: "Сохранить" }));
    expect(await screen.findByText("✅ Сохранено")).toBeInTheDocument();
    expect(api.db.get(3)!.title).toBe("Купить наушники Sony XM5");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Сохранить" })).toBeNull());
  });

  it("closes the parent with the last step (🎉)", async () => {
    useMockApi();
    render(<App outside={false} />);
    fireEvent.click(await screen.findByText("Переезд"));
    fireEvent.click(await screen.findByRole("checkbox", { name: "Отметить: Упаковать вещи" }));
    await screen.findByText(/☑ Упаковать вещи/);
    fireEvent.click(screen.getByRole("checkbox", { name: "Отметить: Отдать ключи хозяйке" }));
    expect(await screen.findByText("🎉 Все шаги выполнены — «Переезд» закрыт")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "↩️ Не сделано" })).toBeInTheDocument();
  });

  it("throws a text into the dump and shows what came of it", async () => {
    const { calls } = useMockApi();
    render(<App outside={false} />);
    await screen.findByText("Позвонить в банк");
    fireEvent.click(screen.getByRole("button", { name: "Добавить" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Что закинуть" }), { target: { value: "завтра в 11 купить молоко" } });
    fireEvent.click(screen.getByRole("button", { name: "📥 Закинуть в свалку" }));
    expect(await screen.findByText("⏳ Разбираю…")).toBeInTheDocument();
    expect(await screen.findByText("✅ Сохранено", {}, { timeout: 6000 })).toBeInTheDocument();
    const created = screen.getAllByText("завтра в 11 купить молоко");
    expect(created.length).toBeGreaterThanOrEqual(2); // the sent text and the new item
    expect(within(created[1]!.closest(".row")!).getByText("завтра 11:00")).toBeInTheDocument();
    expect(calls.filter((c) => c.path.startsWith("/api/inbox/")).length).toBeGreaterThan(0);
  }, 10_000);

  it("asks a question and shows the found rows, or why it could not", async () => {
    const { calls } = useMockApi();
    render(<App outside={false} />);
    await screen.findByText("Позвонить в банк");
    fireEvent.click(screen.getByRole("button", { name: "Ещё" }));
    fireEvent.click(await screen.findByText("Спросить"));
    const field = await screen.findByRole("textbox", { name: "Вопрос" });
    fireEvent.change(field, { target: { value: "?что с наушниками" } });
    fireEvent.click(screen.getByRole("button", { name: "Спросить" }));
    expect(await screen.findByText("По запросу «что с наушниками»")).toBeInTheDocument();
    expect(screen.getAllByText("Купить наушники Sony").length).toBeGreaterThan(0);
    expect(calls.find((c) => c.path === "/api/ask")?.body).toEqual({ question: "что с наушниками" });
    fireEvent.change(field, { target: { value: "сколько осталось бюджета" } });
    fireEvent.click(screen.getByRole("button", { name: "Спросить" }));
    expect(await screen.findByText(/Бюджет на ИИ исчерпан/)).toBeInTheDocument();
  });

  it("filters the feed by status from the trash link", async () => {
    const { calls } = useMockApi();
    render(<App outside={false} />);
    await screen.findByText("Позвонить в банк");
    fireEvent.click(screen.getByRole("button", { name: "Ещё" }));
    fireEvent.click(await screen.findByText("Корзина"));
    expect(await screen.findByText("Старая заметка")).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/api/feed")).toBe(true);
  });

  it("saves settings and shows a server-side refusal on its field", async () => {
    const { api } = useMockApi();
    render(<App outside={false} />);
    await screen.findByText("Позвонить в банк");
    fireEvent.click(screen.getByRole("button", { name: "Ещё" }));
    fireEvent.click(await screen.findByText("Настройки"));
    const tz = await screen.findByDisplayValue("Europe/Warsaw");
    fireEvent.change(tz, { target: { value: "Mars/Olympus" } });
    fireEvent.click(await screen.findByRole("button", { name: "Сохранить" }));
    await waitFor(() =>
      expect(screen.getAllByRole("alert").some((a) => /Не знаю такой часовой пояс/.test(a.textContent ?? ""))).toBe(true),
    );
    fireEvent.change(tz, { target: { value: "Europe/Berlin" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    expect(await screen.findByText("✅ Сохранено")).toBeInTheDocument();
    expect(api.db.prefs.tz).toBe("Europe/Berlin");
  });
});
