import { Info, X } from "lucide-react";
import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { Severity } from "../lib/types";

export function InfoTip({ children, label = "More information" }: { children: ReactNode; label?: string }) {
  const id = useId();
  const btn = useRef<HTMLButtonElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number; up: boolean } | null>(null);
  const show = () => {
    const r = btn.current?.getBoundingClientRect();
    if (!r) return;
    const x = Math.min(Math.max(r.left + r.width / 2, 160), window.innerWidth - 160);
    const up = r.bottom > window.innerHeight - 180;
    setPos({ x, y: up ? r.top - 8 : r.bottom + 8, up });
  };
  const hide = () => setPos(null);
  return (
    <>
      <button
        ref={btn}
        type="button"
        className="info-btn"
        aria-label={label}
        aria-describedby={pos ? id : undefined}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        onClick={(e) => {
          e.stopPropagation();
          if (pos) hide();
          else show();
        }}
        onKeyDown={(e) => e.key === "Escape" && hide()}
      >
        <Info size={15} strokeWidth={1.9} />
      </button>
      {pos &&
        createPortal(
          <div id={id} role="tooltip" className={`tip${pos.up ? " up" : ""}`} style={{ left: pos.x, top: pos.y }}>
            {children}
          </div>,
          document.body,
        )}
    </>
  );
}

export function Wordmark() {
  return (
    <span className="wordmark display" aria-label="Kessler">
      <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
        <ellipse cx="11" cy="11" rx="9.5" ry="4.6" fill="none" stroke="currentColor" strokeWidth="1.6" transform="rotate(-24 11 11)" />
        <circle cx="11" cy="11" r="2.6" fill="currentColor" />
        <circle cx="18.6" cy="6.2" r="1.7" fill="var(--meta)" />
      </svg>
      Kessler
    </span>
  );
}

export function useTitle(title: string | undefined): void {
  useEffect(() => {
    document.title = title ? `${title} | Kessler` : "Kessler";
  }, [title]);
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  label: string;
}) {
  const i = Math.max(0, options.findIndex((o) => o.value === value));
  return (
    <div className="seg" role="group" aria-label={label} style={{ "--n": options.length, "--i": i } as CSSProperties}>
      <span className="seg-thumb" aria-hidden="true" />
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      className={`dialog${wide ? " wide" : ""}`}
      onClose={onClose}
      onClick={(e) => {
        if (e.target === ref.current) onClose();
      }}
      aria-labelledby={titleId}
    >
      {open && (
        <>
          <div className="dialog-head">
            <h2 id={titleId} className="display">
              {title}
            </h2>
            <button className="btn btn-ghost icon-btn" onClick={onClose} aria-label="Close">
              <X size={18} />
            </button>
          </div>
          <div className="dialog-body">{children}</div>
          {footer && <div className="dialog-foot">{footer}</div>}
        </>
      )}
    </dialog>
  );
}

export function Grade({ score, grade }: { score: number; grade: string }) {
  const r = 27;
  const c = 2 * Math.PI * r;
  const color = score >= 75 ? "var(--stable)" : score >= 60 ? "var(--slow)" : "var(--meta)";
  return (
    <div className="grade" role="img" aria-label={`Resilience grade ${grade}, ${score} out of 100`}>
      <svg viewBox="0 0 64 64">
        <circle cx="32" cy="32" r={r} fill="none" stroke="var(--rule)" strokeWidth="6" />
        <circle
          className="arc"
          cx="32"
          cy="32"
          r={r}
          fill="none"
          stroke={color}
          strokeWidth="6"
          strokeLinecap="round"
          strokeDasharray={`${(score / 100) * c} ${c}`}
        />
      </svg>
      <b style={{ color }}>{grade}</b>
    </div>
  );
}

const SEV_TEXT: Record<Severity, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
  info: "Note",
};

export function SeverityDot({ severity }: { severity: Severity }) {
  return <span className={`sev-dot sev-${severity}`} role="img" aria-label={SEV_TEXT[severity]} title={SEV_TEXT[severity]} />;
}

export function StatusDot({ status }: { status: string }) {
  return <span className={`status-dot ${status}`} aria-hidden="true" />;
}

export function Skeleton({ h = 18, w = "100%" }: { h?: number; w?: number | string }) {
  return <div className="skeleton" style={{ height: h, width: w }} aria-hidden="true" />;
}
