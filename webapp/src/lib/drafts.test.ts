/** Drafts: kept in memory and localStorage until saved, so a reopened app (an expired session) gets them back. */
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { clearDraft, getDraft, hasDrafts, resetDrafts, setDraft, useDraft } from "./drafts";

beforeEach(() => {
  resetDrafts();
});

describe("drafts", () => {
  it("are stored on the device and forgotten once saved", () => {
    setDraft("add", "завтра в 10 позвонить в банк");
    expect(JSON.parse(localStorage.getItem("dump:draft:add")!).value).toBe("завтра в 10 позвонить в банк");
    expect(hasDrafts()).toBe(true);
    clearDraft("add");
    expect(localStorage.getItem("dump:draft:add")).toBeNull();
    expect(getDraft("add")).toBeUndefined();
    expect(hasDrafts()).toBe(false);
  });

  it("come back in a new launch of the app, unless older than a week", () => {
    // what an earlier launch left (this one has not read these keys yet)
    localStorage.setItem("dump:draft:card:3", JSON.stringify({ value: { title: "Новое", details: "" }, at: Date.now() }));
    localStorage.setItem("dump:draft:card:4", JSON.stringify({ value: { title: "Старое", details: "" }, at: Date.now() - 8 * 86_400_000 }));
    expect(hasDrafts()).toBe(true);
    expect(getDraft("card:3")).toEqual({ title: "Новое", details: "" });
    expect(getDraft("card:4")).toBeUndefined();
    expect(localStorage.getItem("dump:draft:card:4")).toBeNull();
  });

  it("work in memory when storage is blocked", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    setDraft("add", "текст");
    expect(getDraft("add")).toBe("текст");
  });

  it("are shared state for the screens that use them", () => {
    const first = renderHook(() => useDraft<string>("add"));
    const second = renderHook(() => useDraft<string>("add"));
    act(() => first.result.current[1]("купить хлеб"));
    expect(second.result.current[0]).toBe("купить хлеб");
  });
});
