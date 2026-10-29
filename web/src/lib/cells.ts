import type { CellAggregate, SimSummary } from "./types";

const BAD = new Set(["metastable", "overloaded"]);

export function aggregate(summaries: SimSummary[]): CellAggregate | undefined {
  const ok = summaries.filter((s) => s.verdict !== "failed");
  const n = ok.length;
  if (n === 0) return summaries.length ? { ...EMPTY, failed: true } : undefined;
  const bad = ok.filter((s) => BAD.has(s.verdict)).length;
  const recs = ok
    .map((s) => (BAD.has(s.verdict) || s.recovery_s === null || s.recovery_s === undefined ? Infinity : s.recovery_s))
    .sort((a, b) => a - b);
  const med = recs[Math.floor(recs.length / 2)] ?? Infinity;
  return {
    seeds: n,
    p_metastable: ok.filter((s) => s.verdict === "metastable").length / n,
    p_overloaded: ok.filter((s) => s.verdict === "overloaded").length / n,
    bad: bad * 2 >= n,
    recovery_s: Number.isFinite(med) ? med : null,
    peak_amplification: Math.max(...ok.map((s) => s.peak_amplification ?? 0)),
    wasted_fraction: ok.reduce((a, s) => a + (s.wasted_fraction ?? 0), 0) / n,
    min_goodput_ratio: Math.min(...ok.map((s) => s.min_goodput_ratio ?? 0)),
    lost_requests: Math.round(ok.reduce((a, s) => a + (s.lost_requests ?? 0), 0) / n),
  };
}

const EMPTY: CellAggregate = {
  seeds: 0,
  p_metastable: 0,
  p_overloaded: 0,
  bad: false,
  recovery_s: null,
  peak_amplification: 0,
  wasted_fraction: 0,
  min_goodput_ratio: 0,
  lost_requests: 0,
};

export type CellState = "stable" | "slow" | "metastable" | "overloaded" | "failed";

export function cellState(a: CellAggregate): CellState {
  if (a.failed) return "failed";
  if (a.p_overloaded >= 0.5) return "overloaded";
  if (a.bad) return "metastable";
  if ((a.recovery_s ?? 0) > 10) return "slow";
  return "stable";
}

export const STATE_LABEL: Record<CellState, string> = {
  stable: "Recovers within 10 s",
  slow: "Recovers slowly",
  metastable: "Metastable: does not recover",
  overloaded: "Overloaded before the trigger",
  failed: "Simulation failed",
};
