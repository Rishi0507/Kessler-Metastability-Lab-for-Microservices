import { X } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";
import type { Severity } from "../lib/types";

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
      aria-labelledby="dialog-title"
    >
      {open && (
        <>
          <div className="dialog-head">
            <h2 id="dialog-title" className="display">
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

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <span className={`sev sev-${severity}`}>{SEV_TEXT[severity]}</span>;
}

export function StatusDot({ status }: { status: string }) {
  return <span className={`status-dot ${status}`} aria-hidden="true" />;
}

export function Skeleton({ h = 18, w = "100%" }: { h?: number; w?: number | string }) {
  return <div className="skeleton" style={{ height: h, width: w }} aria-hidden="true" />;
}
