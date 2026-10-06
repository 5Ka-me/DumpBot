import { describe, expect, it } from "vitest";
import {
  addDays,
  capitalize,
  clip,
  count,
  daysBetween,
  fmtDate,
  fmtDateTime,
  fmtDayHeader,
  fmtDuration,
  fmtLongDate,
  fmtMoney,
  fmtStamp,
  leadLabel,
  localDateTime,
  normalizeTags,
  parseClock,
  plural,
  todayIn,
  weekday,
  zoned,
  zonedToUtc,
} from "./format";

const TZ = "Europe/Warsaw";
const NOW = Date.parse("2026-10-06T10:00:00Z"); // Tuesday, 12:00 in Warsaw (CEST, UTC+2)

describe("calendar arithmetic", () => {
  it("adds days across months, years and DST", () => {
    expect(addDays("2026-10-06", 1)).toBe("2026-10-07");
    expect(addDays("2026-10-31", 1)).toBe("2026-11-01");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
    expect(addDays("2026-10-24", 2)).toBe("2026-10-26"); // over the switch back to winter time
    expect(daysBetween("2026-10-06", "2026-10-09")).toBe(3);
    expect(daysBetween("2026-10-06", "2026-10-05")).toBe(-1);
  });

  it("numbers weekdays from Monday", () => {
    expect(weekday("2026-10-05")).toBe(0); // Monday
    expect(weekday("2026-10-06")).toBe(1);
    expect(weekday("2026-10-11")).toBe(6); // Sunday
  });
});

describe("timezones", () => {
  it("shows a UTC moment on the owner's wall clock", () => {
    expect(zoned("2026-10-06T10:00:00Z", TZ)).toMatchObject({ date: "2026-10-06", time: "12:00" });
    expect(zoned("2026-12-31T23:30:00Z", TZ)).toMatchObject({ date: "2027-01-01", time: "00:30" }); // CET, UTC+1
    expect(localDateTime("2026-10-06T21:45:00Z", TZ)).toBe("2026-10-06T23:45");
    expect(todayIn(TZ, Date.parse("2026-10-06T22:30:00Z"))).toBe("2026-10-07");
    expect(todayIn("America/New_York", Date.parse("2026-10-06T02:00:00Z"))).toBe("2026-10-05");
  });

  it("turns a wall time back into the UTC moment, also around DST", () => {
    expect(zonedToUtc("2026-10-06T12:00", TZ).toISOString()).toBe("2026-10-06T10:00:00.000Z");
    expect(zonedToUtc("2026-01-15T09:30", TZ).toISOString()).toBe("2026-01-15T08:30:00.000Z");
    // 2026-03-29 02:30 does not exist in Warsaw (clocks jump 02:00 -> 03:00): the nearest valid moment.
    const gap = zonedToUtc("2026-03-29T02:30", TZ);
    expect(["2026-03-29T00:30:00.000Z", "2026-03-29T01:30:00.000Z"]).toContain(gap.toISOString());
    for (const local of ["2026-10-25T01:15", "2026-10-25T04:00", "2026-07-01T00:00"]) {
      expect(localDateTime(zonedToUtc(local, TZ), TZ)).toBe(local);
    }
  });
});

describe("dates as the bot writes them", () => {
  it("uses today / tomorrow / yesterday, the weekday, and the year only when it differs", () => {
    expect(fmtDate("2026-10-06", "2026-10-06")).toBe("сегодня");
    expect(fmtDate("2026-10-07", "2026-10-06")).toBe("завтра");
    expect(fmtDate("2026-10-05", "2026-10-06")).toBe("вчера");
    expect(fmtDate("2026-10-09", "2026-10-06")).toBe("пт 9 окт");
    expect(fmtDate("2026-10-07", "2026-10-06", false)).toBe("ср 7 окт");
    expect(fmtDate("2027-05-01", "2026-10-06")).toBe("1 мая 2027");
    expect(fmtDate("2026-05-01")).toBe("пт 1 мая");
  });

  it("formats moments, stamps and headers", () => {
    expect(fmtDateTime("2026-10-06T11:00:00Z", TZ, NOW)).toBe("сегодня 13:00");
    expect(fmtDateTime("2026-10-09T13:00:00Z", TZ, NOW)).toBe("пт 9 окт 15:00");
    expect(fmtStamp("2026-10-05T19:10:00Z", TZ, NOW)).toBe("вчера 21:10");
    expect(fmtStamp("2026-09-20T08:00:00Z", TZ, NOW)).toBe("20 сен");
    expect(fmtStamp("2025-09-20T08:00:00Z", TZ, NOW)).toBe("20 сен 2025");
    expect(fmtLongDate("2026-10-06")).toBe("6 октября");
    expect(fmtLongDate("2026-10-06", true)).toBe("вт, 6 октября");
    expect(fmtDayHeader("2026-10-06", "2026-10-06")).toBe("Сегодня, 6 октября");
    expect(fmtDayHeader("2026-10-07", "2026-10-06")).toBe("Завтра, 7 октября");
    expect(fmtDayHeader("2026-10-08", "2026-10-06")).toBe("Чт, 8 октября");
  });
});

describe("numbers and words", () => {
  it("writes durations like the bot", () => {
    expect(fmtDuration(0)).toBe("0 мин");
    expect(fmtDuration(5)).toBe("5 мин");
    expect(fmtDuration(60)).toBe("1 ч");
    expect(fmtDuration(90)).toBe("1 ч 30 мин");
    expect(fmtDuration(1440)).toBe("1 дн");
    expect(fmtDuration(3060)).toBe("2 дн 3 ч");
    expect(fmtDuration(-15)).toBe("-15 мин");
    expect(leadLabel(0)).toBe("в момент");
    expect(leadLabel(60)).toBe("за 1 ч");
  });

  it("writes money with cents (four digits for tiny sums)", () => {
    expect(fmtMoney(0)).toBe("$0.00");
    expect(fmtMoney(0.4213)).toBe("$0.42");
    expect(fmtMoney(0.0004)).toBe("$0.0004");
    expect(fmtMoney(5)).toBe("$5.00");
  });

  it("picks Russian plural forms", () => {
    const forms = (n: number) => plural(n, "запись", "записи", "записей");
    expect([1, 2, 5, 11, 12, 21, 22, 25, 101, 111].map(forms)).toEqual([
      "запись",
      "записи",
      "записей",
      "записей",
      "записей",
      "запись",
      "записи",
      "записей",
      "запись",
      "записей",
    ]);
    expect(count(3, "шаг", "шага", "шагов")).toBe("3 шага");
  });

  it("normalizes typed tags and clock times", () => {
    expect(normalizeTags("#Работа дом, ДОМ;  #идеи ")).toEqual(["работа", "дом", "идеи"]);
    expect(normalizeTags("  ")).toEqual([]);
    expect(parseClock("9:30")).toBe("09:30");
    expect(parseClock("09.05")).toBe("09:05");
    expect(parseClock("23:59")).toBe("23:59");
    expect(parseClock("24:00")).toBeNull();
    expect(parseClock("9")).toBeNull();
  });

  it("clips and capitalizes", () => {
    expect(clip("короткий", 20)).toBe("короткий");
    expect(clip("очень длинный текст", 10)).toBe("очень дли…");
    expect(capitalize("октябрь 2026")).toBe("Октябрь 2026");
    expect(capitalize("")).toBe("");
  });
});
