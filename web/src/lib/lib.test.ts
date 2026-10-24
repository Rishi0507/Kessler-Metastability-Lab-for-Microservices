import { describe, expect, it } from "vitest";
import { aggregate, cellState } from "./cells";
import { axisValue, fmtMs, fmtMult, fmtPct, triggerPhrase } from "./format";
import { backoffMs, RetryBudget } from "./retry";
import type { SimSummary } from "./types";

const sim = (verdict: SimSummary["verdict"], recovery: number | null = null, amp = 1): SimSummary => ({
  verdict,
  recovery_s: recovery,
  peak_amplification: amp,
  wasted_fraction: verdict === "metastable" ? 0.8 : 0.05,
  min_goodput_ratio: verdict === "metastable" ? 0 : 0.7,
  lost_requests: verdict === "metastable" ? 9000 : 100,
});

describe("backoff with full jitter", () => {
  it("stays inside [0, min(cap, base * 2^attempt))", () => {
    for (let attempt = 0; attempt < 8; attempt++) {
      for (let i = 0; i < 200; i++) {
        const ms = backoffMs(attempt, 200, 4000);
        expect(ms).toBeGreaterThanOrEqual(0);
        expect(ms).toBeLessThan(Math.min(4000, 200 * 2 ** attempt));
      }
    }
  });

  it("spreads retries out instead of synchronising them", () => {
    const xs = Array.from({ length: 500 }, () => backoffMs(3, 200, 4000));
    expect(new Set(xs).size).toBeGreaterThan(300);
  });
});

describe("retry budget", () => {
  it("caps retries to a fraction of traffic once the burst allowance is spent", () => {
    const budget = new RetryBudget(0.1, 0, 10);
    let allowed = 0;
    for (let i = 0; i < 200; i++) {
      budget.deposit();
      if (budget.withdraw()) allowed++;
    }
    expect(allowed).toBeGreaterThanOrEqual(20);
    expect(allowed).toBeLessThanOrEqual(26);
  });
});

describe("cell aggregation mirrors the server", () => {
  it("uses a majority vote for the bad verdict", () => {
    expect(aggregate([sim("metastable"), sim("metastable"), sim("recovered", 3)])!.bad).toBe(true);
    expect(aggregate([sim("metastable"), sim("recovered", 3), sim("recovered", 4)])!.bad).toBe(false);
  });

  it("reports the median recovery and the peak amplification", () => {
    const a = aggregate([sim("recovered", 2, 1.2), sim("recovered", 30, 3.5), sim("recovered", 8, 1.1)])!;
    expect(a.recovery_s).toBe(8);
    expect(a.peak_amplification).toBe(3.5);
    expect(cellState(a)).toBe("stable");
  });

  it("classifies slow recoveries, metastability and overload", () => {
    expect(cellState(aggregate([sim("recovered", 45)])!)).toBe("slow");
    expect(cellState(aggregate([sim("metastable")])!)).toBe("metastable");
    expect(cellState(aggregate([sim("overloaded")])!)).toBe("overloaded");
  });

  it("ignores failed simulations in the vote", () => {
    const a = aggregate([sim("failed"), sim("recovered", 1)])!;
    expect(a.seeds).toBe(1);
    expect(a.bad).toBe(false);
  });
});

describe("formatting", () => {
  it("formats multipliers, percentages and durations for people", () => {
    expect(fmtMult(1)).toBe("1×");
    expect(fmtMult(2.5)).toBe("2.5×");
    expect(fmtMult(0.45)).toBe("0.45×");
    expect(fmtPct(0.756)).toBe("76%");
    expect(fmtMs(1500)).toBe("1.5 s");
    expect(fmtMs(12.3)).toBe("12 ms");
    expect(axisValue("errors", 0.5)).toBe("50%");
    expect(axisValue("crash", 30)).toBe("30 s");
  });

  it("describes triggers in plain language", () => {
    expect(triggerPhrase({ kind: "latency", target: "db", duration_s: 20 }, 3)).toBe("a 3× slowdown on db for 20 s");
    expect(triggerPhrase({ kind: "cache_flush", target: "cache" })).toBe("a cache flush on cache");
  });
});
