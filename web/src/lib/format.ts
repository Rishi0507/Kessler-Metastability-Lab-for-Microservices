export function fmtMult(x: number): string {
  return `${x >= 10 ? x.toFixed(0) : x >= 1 ? x.toFixed(1).replace(/\.0$/, "") : x.toFixed(2).replace(/0$/, "")}×`;
}

export function fmtPct(x: number | null | undefined, digits = 0): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return " - ";
  return `${(x * 100).toFixed(digits)}%`;
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return " - ";
  if (ms >= 10_000) return `${(ms / 1000).toFixed(0)} s`;
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)} s`;
  if (ms >= 10) return `${ms.toFixed(0)} ms`;
  return `${ms.toFixed(1)} ms`;
}

export function fmtSeconds(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return " - ";
  if (s < 1) return `${(s * 1000).toFixed(0)} ms`;
  if (s < 90) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  const total = Math.round(s);
  return `${Math.floor(total / 60)} min ${total % 60} s`;
}

export function fmtCount(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return " - ";
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (Math.abs(n) >= 10_000) return `${(n / 1000).toFixed(0)}k`;
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${Math.round(n)}`;
}

export function fmtClock(simSeconds: number): string {
  const m = Math.floor(simSeconds / 60);
  const s = Math.floor(simSeconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

export function fmtAgo(iso: string | null | undefined): string {
  if (!iso) return "";
  const diff = (new Date(iso).getTime() - Date.now()) / 1000;
  const abs = Math.abs(diff);
  if (abs < 60) return rtf.format(Math.round(diff), "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86_400) return rtf.format(Math.round(diff / 3600), "hour");
  return rtf.format(Math.round(diff / 86_400), "day");
}

export function triggerPhrase(t: { kind: string; target?: string; duration_s?: number }, value?: number): string {
  const on = t.target ? ` on ${t.target}` : "";
  const v = value ?? 0;
  switch (t.kind) {
    case "latency":
      return `${value !== undefined ? `a ${fmtMult(v)} ` : "a "}slowdown${on}${t.duration_s ? ` for ${t.duration_s} s` : ""}`;
    case "errors":
      return `${value !== undefined ? `${fmtPct(v)} ` : ""}errors${on}`;
    case "capacity_loss":
      return `losing ${value !== undefined ? fmtPct(v) : "capacity"}${on}`;
    case "spike":
      return `${value !== undefined ? `a ${fmtMult(v)} ` : "a "}traffic spike${t.duration_s ? ` for ${t.duration_s} s` : ""}`;
    case "crash":
      return `${value !== undefined ? `a ${v} s ` : "an "}outage${on}`;
    case "cache_flush":
      return `a cache flush${on}`;
    default:
      return t.kind;
  }
}

export const AXIS_LABEL: Record<string, string> = {
  latency: "Slowdown",
  errors: "Error rate",
  capacity_loss: "Capacity lost",
  spike: "Traffic spike",
  crash: "Outage length",
  cache_flush: "Cache flush",
};

export function axisValue(kind: string, v: number): string {
  if (kind === "errors" || kind === "capacity_loss") return fmtPct(v);
  if (kind === "crash") return `${v} s`;
  if (kind === "cache_flush") return "flush";
  return fmtMult(v);
}
