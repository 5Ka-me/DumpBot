/** The card's sheets and polling: «🕒 Время» keeps what is typed across refetches, a step's time can lose its time of
 * day, a split started elsewhere is polled often for a minute and then rarely. */
import { fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ItemDetail, Step } from "../../api/types";
import { Steps, stepTimeChanged } from "./Steps";
import { TimeSheet } from "./TimeSheet";
import { SPLIT_POLL_FAST_FOR_MS, SPLIT_POLL_MS, SPLIT_POLL_SLOW_MS, useSplitPolling } from "./useJob";

const TZ = "Europe/Moscow";

function item(over: Partial<ItemDetail> = {}): ItemDetail {
  return {
    id: 4,
    kind: "task",
    title: "Заказать машину",
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
    occurrence: {
      id: 40,
      due_at: "2026-10-08T12:00:00Z",
      due_local: "2026-10-08T15:00",
      precision: "exact",
      status: "open",
      done_at: null,
    },
    reminders: [],
    when_text: "⏰ чт 8 окт 15:00",
    steps: [],
    steps_done: 0,
    steps_total: 0,
    related: [],
    source: { kind: "text", text: null, forward: null, raw_id: null, from_app: false, message_date: null },
    images: [],
    can_split: false,
    split_pending: false,
    warnings: [],
    ...over,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("TimeSheet", () => {
  const handlers = () => ({ onClose: vi.fn(), onText: vi.fn(), onPick: vi.fn(), onPeriod: vi.fn(), onSomeday: vi.fn() });

  it("keeps the typed words and the picked date when the card is refetched", () => {
    const h = handlers();
    const view = render(<TimeSheet open item={item()} tz={TZ} busy={false} {...h} />);
    const words = screen.getByRole("textbox", { name: "Словами" });
    const date = screen.getByLabelText("Дата");
    expect(date).toHaveValue("2026-10-08");
    fireEvent.change(words, { target: { value: "в пятницу после работы" } });
    fireEvent.change(date, { target: { value: "2026-10-09" } });

    // a refetch (back from the background, a split being waited for): an equal item, a new object
    view.rerender(<TimeSheet open item={JSON.parse(JSON.stringify(item())) as ItemDetail} tz={TZ} busy={false} {...h} />);

    expect(words).toHaveValue("в пятницу после работы");
    expect(date).toHaveValue("2026-10-09");
  });

  it("starts again from the item when it opens", () => {
    const h = handlers();
    const view = render(<TimeSheet open item={item()} tz={TZ} busy={false} {...h} />);
    fireEvent.change(screen.getByRole("textbox", { name: "Словами" }), { target: { value: "завтра" } });
    view.rerender(<TimeSheet open={false} item={item()} tz={TZ} busy={false} {...h} />);
    const moved = item({ occurrence: { ...item().occurrence!, due_local: "2026-10-10T09:00" } });
    view.rerender(<TimeSheet open item={moved} tz={TZ} busy={false} {...h} />);
    expect(screen.getByRole("textbox", { name: "Словами" })).toHaveValue("");
    expect(screen.getByLabelText("Дата")).toHaveValue("2026-10-10");
    expect(screen.getByLabelText("Время")).toHaveValue("09:00");
  });
});

describe("a step's own time", () => {
  const at15: Step = { id: 6, position: 1, title: "Заказать машину", done: false, due_at: "2026-10-08T12:00:00Z", precision: "exact", when_text: "чт 15:00" };
  const dateOnly: Step = { ...at15, due_at: "2026-10-08T07:00:00Z", precision: "date", when_text: "чт 8 окт" };

  it("changes when only the time of day is cleared", () => {
    expect(stepTimeChanged(at15, "2026-10-08", TZ)).toBe(true);
    expect(stepTimeChanged(at15, "2026-10-08T15:00", TZ)).toBe(false);
    expect(stepTimeChanged(dateOnly, "2026-10-08", TZ)).toBe(false);
    expect(stepTimeChanged(dateOnly, "2026-10-08T10:00", TZ)).toBe(true); // an explicit time
    expect(stepTimeChanged(at15, null, TZ)).toBe(true);
    expect(stepTimeChanged({ ...at15, due_at: null, precision: null }, null, TZ)).toBe(false);
  });

  it("is saved as a date without a time from the sheet", async () => {
    const onSave = vi.fn(async () => true);
    const parent = item({ occurrence: null, steps: [at15], steps_total: 1 });
    render(
      <Steps item={parent} tz={TZ} busy={false} splitStatus={null} onToggle={vi.fn()} onAdd={vi.fn(async () => true)} onSave={onSave} onDelete={vi.fn(async () => true)} onOpen={vi.fn()} onSplit={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Изменить шаг 1: Заказать машину" }));
    expect(screen.getByLabelText("Время шага")).toHaveValue("15:00");
    fireEvent.change(screen.getByLabelText("Время шага"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await vi.waitFor(() => expect(onSave).toHaveBeenCalledWith(at15, { due_local: "2026-10-08" }));
  });
});

describe("useSplitPolling", () => {
  it("polls often for a minute, then rarely, and stops when off", async () => {
    vi.useFakeTimers();
    const reload = vi.fn(async () => undefined);
    const hook = renderHook(({ on }) => useSplitPolling(on, reload), { initialProps: { on: true } });
    const fastTicks = SPLIT_POLL_FAST_FOR_MS / SPLIT_POLL_MS;
    for (let i = 0; i < fastTicks; i++) await vi.advanceTimersByTimeAsync(SPLIT_POLL_MS);
    expect(reload).toHaveBeenCalledTimes(fastTicks);
    await vi.advanceTimersByTimeAsync(SPLIT_POLL_MS * 4);
    expect(reload).toHaveBeenCalledTimes(fastTicks); // now every 30 s
    await vi.advanceTimersByTimeAsync(SPLIT_POLL_SLOW_MS);
    expect(reload).toHaveBeenCalledTimes(fastTicks + 1);
    hook.rerender({ on: false }); // the card went under another one, or the split ended
    await vi.advanceTimersByTimeAsync(SPLIT_POLL_SLOW_MS * 3);
    expect(reload).toHaveBeenCalledTimes(fastTicks + 1);
  });
});
