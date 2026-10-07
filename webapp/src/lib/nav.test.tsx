/** Telegram's BackButton follows the stack, and is hidden when the screens give way (an expired session's gate). */
import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WebApp } from "../telegram";
import { NavProvider, useNav } from "./nav";

function fakeTelegram() {
  const back = { onClick: vi.fn(), offClick: vi.fn(), show: vi.fn(), hide: vi.fn() };
  for (const fn of Object.values(back)) fn.mockReturnValue(back);
  const haptics = { impactOccurred: vi.fn(), notificationOccurred: vi.fn(), selectionChanged: vi.fn() };
  window.Telegram = {
    WebApp: { initData: "query_id=1", isVersionAtLeast: () => true, BackButton: back, HapticFeedback: haptics } as unknown as WebApp,
  };
  return back;
}

afterEach(() => {
  delete window.Telegram;
});

describe("NavProvider", () => {
  it("hides Telegram's Back button when it unmounts", () => {
    const back = fakeTelegram();
    let push: ((id: number) => void) | null = null;
    function Probe() {
      const nav = useNav();
      push = (id) => nav.push({ name: "item", id });
      return null;
    }
    const view = render(
      <NavProvider>
        <Probe />
      </NavProvider>,
    );
    act(() => push!(3)); // a card on top: Back is shown
    expect(back.show).toHaveBeenCalled();
    back.hide.mockClear();

    view.unmount(); // «Сессия устарела» replaces every screen

    expect(back.hide).toHaveBeenCalled();
    expect(back.offClick).toHaveBeenCalled();
  });
});
