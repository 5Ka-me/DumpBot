/** The category of a card: a scrolling row of chips, the current one scrolled into view. */
import { useLayoutEffect, useRef } from "react";
import type { Kind } from "../../api/types";
import { Chip } from "../../components/ui";
import type { KindInfo } from "../../lib/kinds";

export function KindPicker({
  kinds,
  value,
  disabled,
  onChange,
}: {
  kinds: readonly KindInfo[];
  value: Kind;
  disabled: boolean;
  onChange: (kind: Kind) => void;
}) {
  const row = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = row.current;
    const chip = el?.querySelector<HTMLElement>(".chip--on");
    if (el && chip) el.scrollLeft = chip.offsetLeft - (el.clientWidth - chip.offsetWidth) / 2;
  }, [value]);
  return (
    <div className="chips chips--scroll chips--in-card" ref={row} role="group" aria-label="Категория">
      {kinds.map((k) => (
        <Chip key={k.kind} pressed={k.kind === value} onClick={() => k.kind !== value && onChange(k.kind)} disabled={disabled}>
          {k.emoji} {k.label}
        </Chip>
      ))}
    </div>
  );
}
