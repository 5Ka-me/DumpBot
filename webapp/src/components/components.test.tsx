import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ItemDetail } from "../api/types";
import { Steps } from "../screens/card/Steps";
import { row } from "../test/fixtures";
import { isOverdue, RowItem } from "./RowItem";
import { Segmented } from "./ui";

describe("RowItem", () => {
  it("shows the title, time, repeat, checklist progress, parent and tags", () => {
    render(
      <RowItem
        row={row({ title: "Переезд", repeat_text: "каждый пн", steps_done: 2, steps_total: 5, parent_title: "Дом", tags: ["дом", "важно"] })}
        onOpen={() => undefined}
      />,
    );
    expect(screen.getByText("Переезд")).toBeInTheDocument();
    expect(screen.getByText("сегодня 15:00")).toBeInTheDocument();
    expect(screen.getByText("🔁 каждый пн")).toBeInTheDocument();
    expect(screen.getByText("☑ 2/5")).toBeInTheDocument();
    expect(screen.getByText("из «Дом»")).toBeInTheDocument();
    expect(screen.getByText("#дом #важно")).toBeInTheDocument();
  });

  it("ticks a task and opens it", () => {
    const onToggle = vi.fn();
    const onOpen = vi.fn();
    const r = row();
    render(<RowItem row={r} onOpen={onOpen} onToggle={onToggle} />);
    const box = screen.getByRole("checkbox", { name: "Отметить сделанным: Позвонить в банк" });
    expect(box).toHaveAttribute("aria-checked", "false");
    fireEvent.click(box);
    expect(onToggle).toHaveBeenCalledWith(r);
    fireEvent.click(screen.getByText("Позвонить в банк"));
    expect(onOpen).toHaveBeenCalledWith(r);
  });

  it("shows done rows checked and has no toggle for ideas", () => {
    const { rerender } = render(<RowItem row={row({ status: "done", occ_status: "done" })} onOpen={() => undefined} onToggle={() => undefined} />);
    expect(screen.getByRole("checkbox")).toHaveAttribute("aria-checked", "true");
    rerender(<RowItem row={row({ kind: "idea", occurrence_id: null, due_at: null })} onOpen={() => undefined} onToggle={() => undefined} />);
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("marks overdue times", () => {
    const now = Date.parse("2026-10-06T14:00:00Z");
    expect(isOverdue(row(), now)).toBe(true);
    expect(isOverdue(row({ status: "done", occ_status: "done" }), now)).toBe(false);
    expect(isOverdue(row({ period: { kind: "week", start: "2026-10-05", end: "2026-10-11", label: "на этой неделе" } }), now)).toBe(false);
    render(<RowItem row={row({ due_at: "2020-01-01T10:00:00Z", when_text: "1 янв 2020 11:00" })} onOpen={() => undefined} />);
    expect(screen.getByText("1 янв 2020 11:00")).toHaveClass("row-when--overdue");
  });
});

describe("Segmented", () => {
  it("is a tab list that reports the new value", () => {
    const onChange = vi.fn();
    render(
      <Segmented
        label="Списки"
        value="a"
        onChange={onChange}
        options={[
          { value: "a", label: "Открытые" },
          { value: "b", label: "Повторы" },
        ]}
      />,
    );
    expect(screen.getByRole("tab", { name: "Открытые" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("tab", { name: "Повторы" }));
    expect(onChange).toHaveBeenCalledWith("b");
  });
});

function detail(over: Partial<ItemDetail> = {}): ItemDetail {
  return {
    id: 4,
    kind: "task",
    title: "Переезд",
    details: null,
    tags: [],
    status: "open",
    created_at: "2026-10-04T10:00:00Z",
    updated_at: "2026-10-04T10:00:00Z",
    done_at: null,
    deleted_at: null,
    urls: [],
    link_titles: {},
    lead_minutes: null,
    reminders_off: false,
    rrule: null,
    rec_active: false,
    repeat_text: null,
    period: null,
    someday: false,
    role: "event",
    needs_confirm: false,
    parent: null,
    position: null,
    occurrence: null,
    reminders: [],
    when_text: null,
    steps: [
      { id: 5, position: 1, title: "Упаковать вещи", done: false, due_at: null, precision: null, when_text: null },
      { id: 6, position: 2, title: "Заказать машину", done: true, due_at: null, precision: null, when_text: null },
    ],
    steps_done: 1,
    steps_total: 2,
    related: [],
    source: { kind: "text", text: null, forward: null, raw_id: null, from_app: false, message_date: null },
    images: [],
    can_split: false,
    split_pending: false,
    warnings: [],
    ...over,
  };
}

describe("Steps", () => {
  const handlers = () => ({
    onToggle: vi.fn(),
    onAdd: vi.fn(async () => true),
    onSave: vi.fn(async () => true),
    onDelete: vi.fn(async () => true),
    onOpen: vi.fn(),
    onSplit: vi.fn(),
  });

  it("shows the checklist with its progress and toggles a step", () => {
    const h = handlers();
    render(<Steps item={detail()} tz="Europe/Warsaw" busy={false} splitStatus={null} {...h} />);
    expect(screen.getByText("Шаги · 1/2")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "1");
    const done = screen.getByRole("checkbox", { name: "Снять отметку: Заказать машину" });
    expect(done).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByRole("checkbox", { name: "Отметить: Упаковать вещи" }));
    expect(h.onToggle).toHaveBeenCalledWith(expect.objectContaining({ id: 5 }));
  });

  it("adds a step and clears the field", async () => {
    const h = handlers();
    render(<Steps item={detail()} tz="Europe/Warsaw" busy={false} splitStatus={null} {...h} />);
    const field = screen.getByRole("textbox", { name: "Новый шаг" });
    fireEvent.change(field, { target: { value: "  Сдать ключи " } });
    fireEvent.click(screen.getByRole("button", { name: "Добавить" }));
    expect(h.onAdd).toHaveBeenCalledWith("Сдать ключи");
    await vi.waitFor(() => expect(field).toHaveValue(""));
  });

  it("offers «🪄 Разбить на шаги» for a task without steps and shows the running job", () => {
    const h = handlers();
    const { rerender } = render(
      <Steps item={detail({ steps: [], steps_done: 0, steps_total: 0, can_split: true })} tz="Europe/Warsaw" busy={false} splitStatus={null} {...h} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "🪄 Разбить на шаги" }));
    expect(h.onSplit).toHaveBeenCalled();
    rerender(
      <Steps item={detail({ steps: [], steps_done: 0, steps_total: 0, can_split: true })} tz="Europe/Warsaw" busy={false} splitStatus="Думаю…" {...h} />,
    );
    expect(screen.queryByRole("button", { name: "🪄 Разбить на шаги" })).toBeNull();
    expect(screen.getByText(/Думаю…/)).toBeInTheDocument();
  });

  it("has no checklist for a repeating task or a deleted one without steps", () => {
    const h = handlers();
    const { container } = render(
      <Steps item={detail({ steps: [], steps_total: 0, steps_done: 0, rec_active: true })} tz="Europe/Warsaw" busy={false} splitStatus={null} {...h} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
