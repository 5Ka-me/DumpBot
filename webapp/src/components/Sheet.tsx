/** A bottom sheet (dialog): closes by Back, the backdrop, Esc or ✕. */
import { useEffect, useId, useRef, type ReactNode } from "react";
import { useBackHandler } from "../lib/nav";
import { Icon } from "./Icon";

export function Sheet({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const id = useId();
  const panel = useRef<HTMLDivElement>(null);
  useBackHandler(open, onClose);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    return () => previous?.focus?.();
  }, [open]);
  if (!open) return null;
  return (
    <div className="sheet-layer">
      <div className="sheet-backdrop" onClick={onClose} aria-hidden="true" />
      <div className="sheet" role="dialog" aria-modal="true" aria-labelledby={id} ref={panel} tabIndex={-1}>
        <div className="sheet-head">
          <h2 id={id} className="sheet-title">
            {title}
          </h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Закрыть">
            <Icon name="close" size={20} />
          </button>
        </div>
        <div className="sheet-body">{children}</div>
        {footer && <div className="sheet-footer">{footer}</div>}
      </div>
    </div>
  );
}
