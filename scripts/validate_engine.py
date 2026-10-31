from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from kessler.analysis.planner import (
    aggregate_cell,
    default_params,
    infer,
    overloaded_cells,
    plan_next,
)
from kessler.engine import get_engine
from kessler.spec import build_engine_spec, check, compile_text
from kessler.spec.templates import get_template

VOLATILE = {"wall_ms", "events_per_s"}


def rss_mb() -> float:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (n, ctypes.c_size_t)
                for n in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                )
            ]

        c = Counters()
        c.cb = ctypes.sizeof(c)
        k = ctypes.WinDLL("kernel32")
        k.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.WorkingSetSize / 2**20
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class Validator:
    def __init__(self, yaml_text: str, seeds: int, threads: int, quick: bool) -> None:
        self.yaml = yaml_text
        res = compile_text(yaml_text)
        if not res.ok or res.system is None:
            sys.exit(f"spec does not compile: {[e.as_dict() for e in res.errors]}")
        self.system = res.system
        self.static = check(yaml_text)["analysis"]
        self.engine = get_engine()
        self.seeds = seeds
        self.threads = threads
        self.quick = quick
        self.results: dict[str, Any] = {}
        self.sims = 0
        self.events = 0
        self.sim_wall = 0.0

    def run(self, scenario: dict[str, Any]) -> dict[str, Any]:
        t0 = time.perf_counter()
        out = self.engine.run(build_engine_spec(self.system, scenario))
        self.sim_wall += time.perf_counter() - t0
        self.sims += 1
        self.events += int(out["events"])
        return out

    def run_many(self, scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with ThreadPoolExecutor(self.threads) as ex:
            return list(ex.map(self.run, scenarios))

    def report(self, name: str, ok: bool, summary: str, **data: Any) -> None:
        self.results[name] = {"pass": ok, "summary": summary, **data}
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:12} {summary}", flush=True)

    @staticmethod
    def baseline(load: float, seed: int, horizon: float = 120.0) -> dict[str, Any]:
        return {"seed": seed, "horizon_s": horizon, "warmup_s": 30.0, "load": load, "faults": []}

    def check_baseline_flow_accounting(self) -> None:
        runs = self.run_many([self.baseline(1.0, s + 1) for s in range(self.seeds)])
        verdicts = {r["verdict"] for r in runs}
        ratio = min(r["baseline"]["success_ratio"] for r in runs)
        p50 = statistics.median(r["latency_ms"]["p50"] for r in runs)
        p99 = max(r["latency_ms"]["p99"] for r in runs)
        self.report(
            "baseline",
            verdicts == {"stable"} and ratio >= 0.995,
            f"{len(runs)} seeds {sorted(verdicts)}; worst success ratio {ratio:.4f}; e2e p50 {p50:.0f} ms, p99 {p99:.0f} ms",
            success_ratio=ratio,
            p50_ms=p50,
            p99_ms=p99,
        )

        window = 120.0 - 30.0
        expected = {s["name"]: float(s["arrival_rps"]) for s in self.static["services"]}
        rows = []
        for name, want in expected.items():
            got = sum(next(x for x in r["services"] if x["name"] == name)["arrivals"] for r in runs)
            n_expected = want * window * len(runs)
            if n_expected < 300:
                continue
            rel = got / n_expected - 1
            noise = 4 / math.sqrt(n_expected)
            rows.append((name, want, got / window / len(runs), rel, max(noise, 0.02)))
        bad = [r for r in rows if abs(r[3]) > r[4]]
        worst = max(rows, key=lambda r: abs(r[3]))
        self.report(
            "flow",
            not bad,
            f"{len(rows)} services checked; worst {worst[0]} {worst[2]:.1f} rps vs {worst[1]:.1f} model "
            f"({worst[3]:+.2%})" + (f"; out of tolerance: {[r[0] for r in bad]}" if bad else ""),
            services=[
                {"name": r[0], "model_rps": round(r[1], 2), "sim_rps": round(r[2], 2), "rel_err": round(r[3], 5)}
                for r in rows
            ],
        )

        worst_gap = 0.0
        for r in runs:
            for s in r["services"]:
                done = s["ok"] + s["err"] + s["shed"] + s["dropped"]
                if s["arrivals"] >= 100:
                    worst_gap = max(worst_gap, abs(done - s["arrivals"]) / s["arrivals"])
        self.report(
            "accounting",
            worst_gap < 0.01,
            f"largest gap between arrivals and outcomes {worst_gap:.3%} (boundary effects only)",
            worst_gap=worst_gap,
        )

    def check_determinism(self) -> None:
        def strip(r: dict[str, Any]) -> str:
            return json.dumps({k: v for k, v in r.items() if k not in VOLATILE}, sort_keys=True)

        sc = [self.baseline(1.2, 7, 90.0), self.baseline(1.2, 8, 90.0)]
        sequential = [strip(self.run(s)) for s in sc]
        repeat = [strip(self.run(s)) for s in sc]
        parallel = [strip(r) for r in self.run_many(sc * 2)]
        ok = sequential == repeat and parallel == sequential * 2
        self.report(
            "determinism",
            ok,
            "same seed gives identical results, sequentially and on parallel threads"
            if ok
            else "results differ between runs of the same seed",
        )

    def check_saturation(self) -> None:
        sat = float(self.static["max_load_multiplier"])
        loads = [
            round(sat * f, 3)
            for f in ((0.7, 0.9, 1.0, 1.1, 1.3) if self.quick else (0.6, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4))
        ]
        runs = self.run_many([self.baseline(load, s + 1, 90.0) for load in loads for s in range(2)])
        by_load: dict[float, list[str]] = {}
        for load, r in zip([load for load in loads for _ in range(2)], runs, strict=True):
            by_load.setdefault(load, []).append(r["verdict"])
        onset = next((load for load in loads if by_load[load].count("overloaded") * 2 >= 2), None)
        ok = onset is not None and 0.75 * sat <= onset <= 1.3 * sat
        self.report(
            "saturation",
            ok,
            f"model saturates at {sat:.2f}x today's load; simulation overloaded from "
            f"{(f'{onset:.2f}x' if onset else 'beyond the range')} "
            f"({'within' if ok else 'outside'} 0.75-1.3x of the model)",
            model=sat,
            onset=onset,
            verdicts={str(k): v for k, v in by_load.items()},
        )

    def check_planner(self, trigger: dict[str, Any]) -> None:
        sat = float(self.static["max_load_multiplier"])
        rows, cols = (4, 6) if self.quick else (6, 10)
        p = default_params(trigger, sat, seeds=2, cols=cols, rows=rows)
        cells = list(range(p.n_cells))
        flat = self.run_many([p.scenario(c, s) for c in cells for s in range(p.seeds)])
        grid = {
            c: [{"verdict": r["verdict"], "recovery_s": r["recovery_s"]} for r in flat[i * p.seeds : (i + 1) * p.seeds]]
            for i, c in enumerate(cells)
        }
        grid_agg = {c: aggregate_cell(v) for c, v in grid.items()}
        truth = infer(p, {c: a["bad"] for c, a in grid_agg.items()}, overloaded_cells(grid_agg))
        evaluated: dict[int, bool | None] = {}
        aggs: dict[int, dict[str, Any]] = {}
        while nxt := plan_next(p, evaluated, overloaded_cells(aggs)):
            for c in nxt:
                aggs[c] = grid_agg[c]
                evaluated[c] = grid_agg[c]["bad"]
        adaptive = infer(p, {c: bool(v) for c, v in evaluated.items()}, overloaded_cells(aggs))
        wrong = [c for c in cells if adaptive[c]["state"] != truth[c]["state"]]
        inferred = [c for c in cells if adaptive[c]["inferred"]]
        self.report(
            "planner",
            len(wrong) <= max(1, len(cells) // 20),
            f"adaptive simulated {len(evaluated)}/{len(cells)} cells; {len(inferred)} inferred, "
            f"{len(wrong)} disagree with the full grid",
            cells=len(cells),
            simulated=len(evaluated),
            disagree=[
                {
                    "cell": c,
                    "load": p.loads[p.coords(c)[1]],
                    "value": p.values[p.coords(c)[0]],
                    "adaptive": adaptive[c]["state"],
                    "grid": truth[c]["state"],
                    "p_bad": round(1 - sum(1 for x in grid[c] if x["verdict"] in ("stable", "recovered")) / p.seeds, 2),
                }
                for c in wrong
            ],
        )

    def check_performance(self, rss_start: float) -> None:
        rate = self.events / self.sim_wall if self.sim_wall else 0.0
        grown = rss_mb() - rss_start
        self.report(
            "performance",
            grown < 200,
            f"{self.sims} simulations, {self.events / 1e6:.0f}M events; {rate / 1e6:.2f}M events/s per thread; "
            f"{self.sim_wall / self.sims:.2f} s per simulation; memory grew {grown:.0f} MB",
            sims=self.sims,
            events=self.events,
            events_per_s=rate,
            s_per_sim=self.sim_wall / self.sims,
            rss_growth_mb=grown,
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--spec", type=Path)
    src.add_argument("--template")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--threads", type=int, default=min(2, os.cpu_count() or 1))
    ap.add_argument("--quick", action="store_true", help="smaller saturation and planner grids")
    ap.add_argument("--report", type=Path, help="write the results as JSON")
    args = ap.parse_args()
    if args.template:
        t = get_template(args.template)
        yaml_text, trigger = t.yaml, t.trigger
    else:
        yaml_text, trigger = args.spec.read_text(encoding="utf-8"), None
    start = time.perf_counter()
    rss0 = rss_mb()
    v = Validator(yaml_text, args.seeds, args.threads, args.quick)
    n = len(v.static["services"])
    print(
        f"{v.system.get('name', 'system')}: {n} services, {len(v.static['edges'])} edges, "
        f"saturation {v.static['max_load_multiplier']:.2f}x, {args.threads} threads"
    )
    v.check_baseline_flow_accounting()
    v.check_determinism()
    v.check_saturation()
    bottleneck = v.static["bottleneck"]
    v.check_planner(trigger or {"kind": "latency", "target": bottleneck, "duration_s": 20})
    v.check_performance(rss0)
    passed = sum(1 for r in v.results.values() if r["pass"])
    print(f"{passed}/{len(v.results)} checks passed in {time.perf_counter() - start:.0f} s")
    if args.report:
        args.report.write_text(json.dumps(v.results, indent=2, default=float), encoding="utf-8")
    sys.exit(0 if passed == len(v.results) else 1)


if __name__ == "__main__":
    main()
