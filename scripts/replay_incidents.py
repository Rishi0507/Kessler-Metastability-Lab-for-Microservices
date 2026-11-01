from __future__ import annotations

import argparse
import itertools
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from kessler.engine import get_engine
from kessler.spec import (
    apply_patch,
    build_engine_spec,
    check,
    compile_text,
    merge_patches,
)
from kessler.spec.lint import Finding

ENGINE = get_engine()
SEEDS = (1, 2)
RECOVERS = {"stable", "recovered"}


def dynamodb_yaml(
    rps: float, p50_ms: float, timeout_s: float, attempts: int, instances: int = 4, queue: str = "{ capacity: 2000 }"
) -> str:
    return f"""
name: DynamoDB metadata storm
traffic:
  rps: {rps}
  entry: metadata
  client:
    timeout: {timeout_s}s
    retry: {{ attempts: {attempts}, base: 50ms, cap: 400ms, jitter: none }}
services:
  metadata:
    instances: {instances}
    concurrency: 16
    latency: {{ p50: {p50_ms}ms, p99: {p50_ms * 4}ms }}
    queue: {queue}
    calls:
      - to: membership-store
        timeout: 500ms
  membership-store:
    instances: 3
    concurrency: 32
    latency: {{ p50: 3ms, p99: 15ms }}
"""


def facebook_yaml(rps: float, db_concurrency: int, key_space: int, cache_retry: int) -> str:
    return f"""
name: Facebook configuration storm
traffic:
  rps: {rps}
  entry: web
  client:
    timeout: 2s
    retry: {{ attempts: 2, base: 200ms, jitter: full }}
services:
  web:
    instances: 6
    concurrency: 64
    latency: {{ p50: 3ms, p99: 15ms }}
    calls:
      - to: config-cache
        timeout: 300ms
  config-cache:
    instances: 4
    concurrency: 256
    latency: {{ p50: 0.3ms, p99: 2ms }}
    cache: {{ hit_ratio: 99%, key_space: {key_space}, miss_target: config-db }}
    calls:
      - to: config-db
        timeout: 250ms
        retry: {{ attempts: {cache_retry}, base: 20ms, jitter: none }}
  config-db:
    instances: 1
    concurrency: {db_concurrency}
    latency: {{ p50: 4ms, p99: 20ms }}
    queue: {{ capacity: 2000 }}
"""


def system_of(yaml_text: str) -> dict[str, Any]:
    res = compile_text(yaml_text)
    if not res.ok or res.system is None:
        raise ValueError([e.as_dict() for e in res.errors])
    return res.system


def outcome(yaml_text: str, fault: dict[str, Any] | None, seed: int, load: float = 1.0) -> str:
    scenario = {
        "seed": seed,
        "horizon_s": 260.0,
        "warmup_s": 30.0,
        "load": load,
        "recovery_extend_s": 600.0,
        "faults": [fault | {"start_s": 60.0}] if fault else [],
    }
    return ENGINE.run(build_engine_spec(system_of(yaml_text), scenario))["verdict"]


def verdict(yaml_text: str, fault: dict[str, Any] | None) -> str:
    vs = [outcome(yaml_text, fault, s) for s in SEEDS]
    if all(v in RECOVERS for v in vs):
        return "recovers"
    if all(v == "metastable" for v in vs):
        return "stuck"
    if all(v == "overloaded" for v in vs):
        return "overloaded"
    return "mixed"


def replay(yaml_text: str, steps: list[tuple[float, dict[str, Any] | None]], seed: int = 1) -> list[float]:
    spec = build_engine_spec(
        system_of(yaml_text), {"seed": seed, "horizon_s": steps[-1][0] + 1, "warmup_s": 30.0, "load": 1.0, "faults": []}
    )
    sim = ENGINE.live(spec, emit_frames=False)
    try:
        for t, cmd in steps:
            sim.advance(t)
            if cmd is not None:
                sim.command(cmd)
        series = sim.summary(include_series=True)["series"]
    finally:
        sim.close()
    out = []
    for t, _ in steps:
        idx = [i for i, ts in enumerate(series["t"]) if t - 20 < ts <= t]
        offered = sum(series["offered"][i] for i in idx)
        good = sum(series["goodput"][i] for i in idx)
        out.append(round(good / offered, 3) if offered else 1.0)
    return out


def window_ratio(series: dict[str, list[float]], t: float, width: float) -> float:
    idx = [i for i, ts in enumerate(series["t"]) if t - width < ts <= t]
    offered = sum(series["offered"][i] for i in idx)
    return sum(series["goodput"][i] for i in idx) / offered if offered else 1.0


def operator_ramp(
    yaml_text: str,
    before: list[tuple[float, dict[str, Any]]],
    seed: int = 1,
    step_s: float = 20.0,
    horizon_s: float = 1800.0,
) -> dict[str, Any]:
    spec = build_engine_spec(
        system_of(yaml_text), {"seed": seed, "horizon_s": horizon_s, "warmup_s": 30.0, "load": 1.0, "faults": []}
    )
    sim = ENGINE.live(spec, emit_frames=False)
    try:
        for t, cmd in before:
            sim.advance(t)
            sim.command(cmd)
        t = before[-1][0] + 60.0
        sim.advance(t)
        on_at, level, healthy_full = t, 0.05, 0
        sim.command({"op": "load", "multiplier": level})
        while t + step_s < horizon_s:
            t += step_s
            sim.advance(t)
            ratio = window_ratio(sim.summary(include_series=True)["series"], t, step_s)
            if ratio >= 0.99:
                if level >= 1.0:
                    healthy_full += 1
                    if healthy_full * step_s >= 60:
                        return {"recovered": True, "seconds_to_full_traffic": round(t - 60 - on_at)}
                level = min(1.0, level * 1.5)
            else:
                healthy_full = 0
                level = max(0.05, level / 1.5)
            sim.command({"op": "load", "multiplier": level})
        return {"recovered": False, "seconds_to_full_traffic": None, "level_reached": round(level, 3)}
    finally:
        sim.close()


def kessler_fixed(yaml_text: str) -> str:
    findings = [Finding(**{k: v for k, v in f.items() if k != "id"}) for f in check(yaml_text)["findings"]]
    return apply_patch(yaml_text, merge_patches(findings))


def static_flags(yaml_text: str) -> list[str]:
    return [f["title"] for f in check(yaml_text)["findings"] if f["severity"] in ("critical", "high", "medium")]


def dynamodb(threads: int) -> dict[str, Any]:
    disruption = {"kind": "latency", "target": "metadata", "duration_s": 10.0, "magnitude": 10.0}
    grid = list(itertools.product((100, 150, 200, 300), (2.0, 2.5, 3.0, 3.5), (0.4, 0.6), (3, 6)))
    base_p50 = 30.0

    def facts(params: tuple[float, float, float, int]) -> dict[str, Any]:
        rps, growth, timeout, attempts = params
        before = dynamodb_yaml(rps, base_p50 * timeout / 0.5, timeout, attempts)
        after = dynamodb_yaml(rps, base_p50 * growth * timeout / 0.5, timeout, attempts)
        return {
            "params": params,
            "before": verdict(before, disruption),
            "after": verdict(after, disruption),
            "after_baseline": verdict(after, None),
        }

    with ThreadPoolExecutor(threads) as ex:
        rows = list(ex.map(facts, grid))
    matching = [
        r for r in rows if r["before"] == "recovers" and r["after"] == "stuck" and r["after_baseline"] == "recovers"
    ]

    def predictions(r: dict[str, Any]) -> dict[str, Any]:
        rps, growth, timeout, attempts = r["params"]
        p50 = base_p50 * growth * timeout / 0.5
        after = dynamodb_yaml(rps, p50, timeout, attempts)
        ratios = replay(
            after,
            [
                (60, {"op": "fault", "fault": disruption}),
                (150, {"op": "load", "multiplier": 0.0}),
                (210, {"op": "load", "multiplier": 1.0}),
                (330, None),
            ],
        )
        fixes = {
            "more metadata capacity (2x)": dynamodb_yaml(rps, p50, timeout, attempts, instances=8),
            "lower renewal rate (half)": dynamodb_yaml(rps / 2, p50, timeout, attempts),
            "longer timeout (3x)": dynamodb_yaml(rps, p50, timeout * 3, attempts),
            "longer timeout (3x), queue drops stale requests": dynamodb_yaml(
                rps,
                p50,
                timeout * 3,
                attempts,
                queue=f"{{ capacity: 2000, timeout: {timeout * 3 * 1000:g}ms, discipline: adaptive_lifo }}",
            ),
            "Kessler's suggested fixes": kessler_fixed(after),
        }
        return {
            "params": r["params"],
            "flagged": static_flags(after),
            "stuck_before_pause": ratios[1] < 0.9,
            "healthy_after_resume": ratios[3] >= 0.95,
            "ratios": ratios,
            "fixes": {name: verdict(y, disruption) for name, y in fixes.items()},
        }

    with ThreadPoolExecutor(threads) as ex:
        preds = list(ex.map(predictions, matching))
    return {"settings": len(rows), "reproduce_facts": len(matching), "facts": rows, "predictions": preds}


def facebook(threads: int) -> dict[str, Any]:
    flush = {"kind": "cache_flush", "target": "config-cache", "duration_s": 0.0, "magnitude": 1.0}
    grid = list(itertools.product((1500, 3000), (2, 4, 8), (20_000, 200_000), (1, 3)))

    def facts(params: tuple[float, int, int, int]) -> dict[str, Any]:
        y = facebook_yaml(*params)
        return {"params": params, "baseline": verdict(y, None), "flush": verdict(y, flush)}

    with ThreadPoolExecutor(threads) as ex:
        rows = list(ex.map(facts, grid))
    matching = [r for r in rows if r["baseline"] == "recovers" and r["flush"] == "stuck"]

    def predictions(r: dict[str, Any]) -> dict[str, Any]:
        y = facebook_yaml(*r["params"])
        site_off = [(60, {"op": "fault", "fault": flush}), (120, {"op": "load", "multiplier": 0.0})]
        all_at_once = replay(y, [*site_off, (180, {"op": "load", "multiplier": 1.0}), (300, None)])
        gradually = operator_ramp(y, site_off)
        rps, db_conc, key_space, cache_retry = r["params"]
        big_db = facebook_yaml(rps, db_conc * 4, key_space, cache_retry)
        return {
            "params": r["params"],
            "flagged": static_flags(y),
            "all_at_once_recovers": all_at_once[-1] >= 0.95,
            "gradual_recovers": gradually["recovered"],
            "all_at_once": all_at_once,
            "gradually": gradually,
            "fix_database_sized_for_cold_traffic": verdict(big_db, flush),
            "fix_kessler_suggested": verdict(kessler_fixed(y), flush),
        }

    with ThreadPoolExecutor(threads) as ex:
        preds = list(ex.map(predictions, matching))
    return {"settings": len(rows), "reproduce_facts": len(matching), "facts": rows, "predictions": preds}


def summarise(name: str, r: dict[str, Any], checks: dict[str, int]) -> dict[str, Any]:
    n = len(r["predictions"])
    print(f"\n{name}: {r['reproduce_facts']} of {r['settings']} plausible settings reproduce the postmortem's facts")
    for label, count in checks.items():
        print(f"  {label:58} {count}/{n}")
    return {"settings": r["settings"], "reproduce_facts": r["reproduce_facts"], "checks": checks}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()

    d = dynamodb(args.threads)
    dp = d["predictions"]
    d_sum = summarise(
        "AWS DynamoDB 2015",
        d,
        {
            "static check flags the risk before simulating": sum(1 for p in dp if p["flagged"]),
            "stuck until requests are paused": sum(1 for p in dp if p["stuck_before_pause"]),
            "pause then resume ends the failure (the engineers' fix)": sum(1 for p in dp if p["healthy_after_resume"]),
            **{
                f"prevents it: {k}": sum(1 for p in dp if p["fixes"][k] == "recovers")
                for k in (dp[0]["fixes"] if dp else {})
            },
        },
    )
    f = facebook(args.threads)
    fp = f["predictions"]
    f_sum = summarise(
        "Facebook 2010",
        f,
        {
            "static check flags the risk before simulating": sum(1 for p in fp if p["flagged"]),
            "restoring all traffic at once collapses it again": sum(1 for p in fp if not p["all_at_once_recovers"]),
            "letting users back while the database copes recovers": sum(1 for p in fp if p["gradual_recovers"]),
            "a database sized for cold traffic prevents it": sum(
                1 for p in fp if p["fix_database_sized_for_cold_traffic"] == "recovers"
            ),
            "Kessler's suggested fixes prevent it": sum(1 for p in fp if p["fix_kessler_suggested"] == "recovers"),
        },
    )
    ramps = [p["gradually"]["seconds_to_full_traffic"] for p in fp if p["gradual_recovers"]]
    if ramps:
        print(
            f"  operator ramp back to full traffic: median {sorted(ramps)[len(ramps) // 2]} s, "
            f"range {min(ramps)}-{max(ramps)} s of simulated time"
        )
    if args.report:
        args.report.write_text(
            json.dumps(
                {"dynamodb_2015": d | {"summary": d_sum}, "facebook_2010": f | {"summary": f_sum}},
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
