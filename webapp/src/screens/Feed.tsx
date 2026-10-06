/** «Лента»: everything, newest first — search, filters (category, tags, status, dates), picture previews, paging. */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, isAbort } from "../api";
import type { FeedQuery, FeedStatus, Kind, Row } from "../api/types";
import { Icon } from "../components/Icon";
import { RowList } from "../components/RowItem";
import { Sheet } from "../components/Sheet";
import { Chip, Empty, ErrorBox, Loading, RefreshButton, ScreenHeader, Section, Segmented, Spinner } from "../components/ui";
import { useOpenItem } from "../lib/actions";
import { fmtDate, fmtStamp, normalizeTags, todayIn } from "../lib/format";
import { kindInfo } from "../lib/kinds";
import { useKinds, useTz } from "../lib/me";
import { useStaleSignal } from "../lib/store";

export interface Filters {
  kinds: Kind[];
  tags: string[];
  status: FeedStatus;
  from: string;
  to: string;
}

export const NO_FILTERS: Filters = { kinds: [], tags: [], status: "any", from: "", to: "" };
const PAGE = 30;
const MAX_PAGE = 100;

const STATUS_OPTIONS: { value: FeedStatus; label: string }[] = [
  { value: "any", label: "Все" },
  { value: "open", label: "Открытые" },
  { value: "done", label: "Сделанные" },
  { value: "deleted", label: "Корзина" },
];

/** How many filters are on (the badge of the filter button). */
export function activeFilters(f: Filters): number {
  return f.kinds.length + f.tags.length + (f.status !== "any" ? 1 : 0) + (f.from ? 1 : 0) + (f.to ? 1 : 0);
}

export function toQuery(q: string, f: Filters): FeedQuery {
  return {
    q: q.trim() || undefined,
    kinds: f.kinds.length ? f.kinds : undefined,
    tags: f.tags.length ? f.tags : undefined,
    status: f.status,
    from: f.from || undefined,
    to: f.to || undefined,
  };
}

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

interface FeedState {
  key: string;
  rows: Row[];
  next: string | null;
  loading: boolean;
  more: boolean;
  error?: unknown;
}

/** The last pages per query: instant when the tab is opened again. */
const pagesCache = new Map<string, { rows: Row[]; next: string | null }>();

function remember(key: string, rows: Row[], next: string | null) {
  pagesCache.delete(key);
  pagesCache.set(key, { rows, next });
  while (pagesCache.size > 8) pagesCache.delete(pagesCache.keys().next().value as string);
}

function useFeed(query: FeedQuery) {
  const key = JSON.stringify(query);
  const initial = pagesCache.get(key);
  const [state, setState] = useState<FeedState>({
    key,
    rows: initial?.rows ?? [],
    next: initial?.next ?? null,
    loading: true,
    more: false,
  });
  const controller = useRef<AbortController | null>(null);
  const queryRef = useRef(query);
  const rowsRef = useRef(state.rows);
  useEffect(() => {
    queryRef.current = query;
    rowsRef.current = state.rows;
  });

  const load = useCallback(
    async (limit = PAGE) => {
      controller.current?.abort();
      const ctrl = new AbortController();
      controller.current = ctrl;
      setState((s) => {
        const cached = s.key === key ? null : pagesCache.get(key);
        return s.key === key
          ? { ...s, loading: true, error: undefined }
          : { key, rows: cached?.rows ?? [], next: cached?.next ?? null, loading: true, more: false };
      });
      try {
        const page = await api.feed({ ...queryRef.current, limit }, { signal: ctrl.signal });
        remember(key, page.rows, page.next_cursor);
        setState({ key, rows: page.rows, next: page.next_cursor, loading: false, more: false });
      } catch (error) {
        if (isAbort(error)) return;
        setState((s) => ({ ...s, loading: false, error }));
      }
    },
    [key],
  );

  const loadMore = useCallback(async () => {
    if (!state.next || state.more || state.loading) return;
    const ctrl = new AbortController();
    controller.current = ctrl;
    setState((s) => ({ ...s, more: true, error: undefined }));
    try {
      const page = await api.feed({ ...queryRef.current, cursor: state.next, limit: PAGE }, { signal: ctrl.signal });
      setState((s) => {
        const seen = new Set(s.rows.map((r) => r.id));
        const rows = [...s.rows, ...page.rows.filter((r) => !seen.has(r.id))];
        remember(key, rows, page.next_cursor);
        return { ...s, rows, next: page.next_cursor, more: false };
      });
    } catch (error) {
      if (isAbort(error)) return;
      setState((s) => ({ ...s, more: false, error }));
    }
  }, [key, state.next, state.more, state.loading]);

  useEffect(() => {
    void load();
    return () => controller.current?.abort();
  }, [load]);

  // An item changed elsewhere: reload what is shown (up to 100 rows), keeping the scroll.
  useStaleSignal(["feed"], () => void load(Math.min(MAX_PAGE, Math.max(PAGE, rowsRef.current.length))));

  // A new query keeps the old rows on screen until its answer comes (no flash of the skeleton while typing).
  const current = state.key === key ? state : { ...state, next: null, loading: true };
  return { ...current, reload: () => load(), loadMore };
}

function FilterSheet({
  open,
  value,
  onClose,
  onApply,
}: {
  open: boolean;
  value: Filters;
  onClose: () => void;
  onApply: (f: Filters) => void;
}) {
  const kinds = useKinds();
  const [draft, setDraft] = useState(value);
  const [tagsText, setTagsText] = useState(value.tags.join(" "));
  useEffect(() => {
    if (open) {
      setDraft(value);
      setTagsText(value.tags.join(" "));
    }
  }, [open, value]);
  const toggleKind = (kind: Kind) =>
    setDraft((d) => ({ ...d, kinds: d.kinds.includes(kind) ? d.kinds.filter((k) => k !== kind) : [...d.kinds, kind] }));
  const apply = () => onApply({ ...draft, tags: normalizeTags(tagsText).slice(0, 10) });
  return (
    <Sheet
      open={open}
      title="Фильтры"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="button button--secondary" onClick={() => onApply(NO_FILTERS)}>
            Сбросить
          </button>
          <button type="button" className="button" onClick={apply}>
            Показать
          </button>
        </>
      }
    >
      <div className="field-group">
        <span className="field-label">Статус</span>
        <Segmented
          label="Статус"
          value={draft.status}
          onChange={(status) => setDraft((d) => ({ ...d, status }))}
          options={STATUS_OPTIONS}
        />
      </div>
      <div className="field-group">
        <span className="field-label">Категории</span>
        <div className="chips">
          {kinds.map((k) => (
            <Chip key={k.kind} pressed={draft.kinds.includes(k.kind)} onClick={() => toggleKind(k.kind)}>
              {k.emoji} {k.label}
            </Chip>
          ))}
        </div>
      </div>
      <label className="field-group">
        <span className="field-label">Теги</span>
        <input
          className="input"
          value={tagsText}
          onChange={(e) => setTagsText(e.target.value)}
          placeholder="через пробел: работа дом"
          autoCapitalize="none"
          autoCorrect="off"
        />
      </label>
      <div className="field-group">
        <span className="field-label">Сохранено</span>
        <div className="date-range">
          <label>
            <span className="field-hint">с</span>
            <input
              className="input"
              type="date"
              value={draft.from}
              max={draft.to || undefined}
              onChange={(e) => setDraft((d) => ({ ...d, from: e.target.value }))}
            />
          </label>
          <label>
            <span className="field-hint">по</span>
            <input
              className="input"
              type="date"
              value={draft.to}
              min={draft.from || undefined}
              onChange={(e) => setDraft((d) => ({ ...d, to: e.target.value }))}
            />
          </label>
        </div>
      </div>
    </Sheet>
  );
}

function ActiveFilters({ value, onChange, today }: { value: Filters; onChange: (f: Filters) => void; today: string }) {
  const chips: { key: string; label: string; next: Filters }[] = [];
  if (value.status !== "any") {
    const label = STATUS_OPTIONS.find((o) => o.value === value.status)?.label ?? value.status;
    chips.push({ key: "status", label, next: { ...value, status: "any" } });
  }
  for (const kind of value.kinds) {
    const info = kindInfo(kind);
    chips.push({ key: `k:${kind}`, label: `${info.emoji} ${info.label}`, next: { ...value, kinds: value.kinds.filter((k) => k !== kind) } });
  }
  for (const tag of value.tags) {
    chips.push({ key: `t:${tag}`, label: `#${tag}`, next: { ...value, tags: value.tags.filter((t) => t !== tag) } });
  }
  if (value.from) chips.push({ key: "from", label: `с ${fmtDate(value.from, today, false)}`, next: { ...value, from: "" } });
  if (value.to) chips.push({ key: "to", label: `по ${fmtDate(value.to, today, false)}`, next: { ...value, to: "" } });
  if (!chips.length) return null;
  return (
    <div className="chips chips--scroll" aria-label="Включённые фильтры">
      {chips.map((c) => (
        <button key={c.key} type="button" className="chip chip--on" onClick={() => onChange(c.next)} aria-label={`Убрать фильтр ${c.label}`}>
          {c.label} <span aria-hidden="true">✕</span>
        </button>
      ))}
    </div>
  );
}

function MoreSentinel({ onVisible, disabled }: { onVisible: () => void; disabled: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const callback = useRef(onVisible);
  useEffect(() => {
    callback.current = onVisible;
  });
  useEffect(() => {
    if (disabled || !ref.current || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => entries.some((e) => e.isIntersecting) && callback.current(), {
      rootMargin: "400px",
    });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [disabled]);
  return <div ref={ref} className="sentinel" />;
}

export function FeedScreen({ initialStatus }: { initialStatus?: FeedStatus }) {
  const tz = useTz();
  const open = useOpenItem();
  const [text, setText] = useState("");
  const [filters, setFilters] = useState<Filters>({ ...NO_FILTERS, status: initialStatus ?? "any" });
  const [sheet, setSheet] = useState(false);
  const q = useDebounced(text, 350);
  const feed = useFeed(toQuery(q, filters));
  const today = todayIn(tz);
  const active = activeFilters(filters);
  const now = Date.now();

  return (
    <>
      <ScreenHeader
        title={filters.status === "deleted" ? "Корзина" : "Лента"}
        actions={<RefreshButton onClick={() => void feed.reload()} busy={feed.loading} />}
      />
      <form className="search-bar" role="search" onSubmit={(e) => e.preventDefault()}>
        <label className="search">
          <Icon name="search" size={18} />
          <input
            type="search"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Поиск: слова, #тег"
            aria-label="Поиск по свалке"
            enterKeyHint="search"
            maxLength={200}
          />
        </label>
        <button
          type="button"
          className={active ? "icon-button icon-button--badge" : "icon-button"}
          onClick={() => setSheet(true)}
          aria-label={active ? `Фильтры, включено: ${active}` : "Фильтры"}
        >
          <Icon name="filter" />
          {active > 0 && <span className="badge">{active}</span>}
        </button>
      </form>
      <ActiveFilters value={filters} onChange={setFilters} today={today} />
      {feed.error && feed.rows.length > 0 ? <ErrorBox error={feed.error} onRetry={feed.reload} /> : null}
      {feed.rows.length === 0 ? (
        feed.loading ? (
          <Loading rows={6} />
        ) : feed.error ? (
          <ErrorBox error={feed.error} onRetry={feed.reload} />
        ) : (
          <Empty
            emoji={q || active ? "🔍" : "🗑"}
            title={q || active ? "Ничего не нашлось" : "Здесь пока пусто"}
            text={q || active ? "Попробуй другие слова или сбрось фильтры." : "Всё, что закинешь в бота, появится здесь."}
          />
        )
      ) : (
        <div className={feed.loading ? "dim" : undefined} aria-busy={feed.loading}>
          <Section>
            <RowList rows={feed.rows} onOpen={open} thumbnail snippet aside={(row) => fmtStamp(row.created_at, tz, now)} />
          </Section>
        </div>
      )}
      {feed.rows.length > 0 && !feed.loading && (
        <div className="list-end">
          {feed.next ? (
            <>
              <MoreSentinel onVisible={feed.loadMore} disabled={feed.more || feed.loading} />
              {feed.more ? (
                <Spinner label="Загружаю ещё" />
              ) : (
                <button type="button" className="text-button" onClick={feed.loadMore}>
                  Показать ещё
                </button>
              )}
            </>
          ) : (
            <span className="field-hint">Это всё · {fmtStamp(feed.rows[feed.rows.length - 1]!.created_at, tz, now)}</span>
          )}
        </div>
      )}
      <FilterSheet
        open={sheet}
        value={filters}
        onClose={() => setSheet(false)}
        onApply={(f) => {
          setFilters(f);
          setSheet(false);
        }}
      />
    </>
  );
}
