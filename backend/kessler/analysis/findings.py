from __future__ import annotations

from typing import Any

from .planner import TRIGGER_DEFAULTS, SweepParams


def _trigger_phrase(params: SweepParams, value: float) -> str:
    kind = params.trigger["kind"]
    target = params.trigger.get("target")
    on = f" on {target}" if target else ""
    if kind == "latency":
        d = params.trigger.get("duration_s", 20)
        return f"a {value:g}× slowdown{on} for {d:g} s"
    if kind == "errors":
        return f"a {value * 100:.0f}% error burst{on}"
    if kind == "capacity_loss":
        return f"losing {value * 100:.0f}% of capacity{on}"
    if kind == "spike":
        d = params.trigger.get("duration_s", 30)
        return f"a {value:g}× traffic spike for {d:g} s"
    if kind == "crash":
        return f"a {value:g} s outage{on}"
    if kind == "cache_flush":
        return f"a cache flush{on}"
    return f"{kind}{on} ({value:g}{TRIGGER_DEFAULTS.get(kind, {}).get('unit', '')})"


def summarize(
    params: SweepParams,
    verdicts: dict[int, dict[str, Any]],
    cells: dict[int, dict[str, Any]],
    boundary_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    total = params.n_cells
    safe = sum(1 for v in verdicts.values() if not v["bad"])
    findings: list[dict[str, Any]] = []

    tipping = [r for r in boundary_rows if r["tipping_load"] is not None]
    at_baseline = [r for r in tipping if r["tipping_load"] <= 1.0]
    if at_baseline:
        r = min(at_baseline, key=lambda r: r["value"])
        findings.append(
            {
                "rule": "tips-at-todays-load",
                "severity": "critical",
                "title": f"Today's traffic already tips over under {_trigger_phrase(params, r['value'])}",
                "detail": (
                    f"At {r['tipping_load']:g}× today's traffic the system does not recover after "
                    f"{_trigger_phrase(params, r['value'])}: it is still failing {params.observe_s:g} s after the "
                    "trigger ended. Production is one incident away from a self-sustaining outage."
                ),
                "evidence": r,
            }
        )
    elif tipping:
        r = min(tipping, key=lambda r: r["value"])
        findings.append(
            {
                "rule": "tipping-point",
                "severity": "high" if r["tipping_load"] < 1.5 else "medium",
                "title": f"Tips over at {r['tipping_load']:g}× today's traffic",
                "detail": (
                    f"Below {r['tipping_load']:g}× load the system absorbs {_trigger_phrase(params, r['value'])}. "
                    "Above it, the same trigger leaves the system in a failure state it cannot leave on its own."
                ),
                "evidence": r,
            }
        )
    else:
        findings.append(
            {
                "rule": "no-tipping-point",
                "severity": "info",
                "title": "No tipping point inside the swept range",
                "detail": (
                    f"Every combination up to {max(params.loads):g}× load recovered after the trigger ended. "
                    "Widen the range to find where the boundary is."
                ),
                "evidence": {},
            }
        )

    bad = [c for c in cells.values() if c.get("bad")]
    if bad:
        amp = max(c.get("peak_amplification", 0) for c in bad)
        wasted = sum(c.get("wasted_fraction", 0) for c in bad) / len(bad)
        mechanisms = []
        if amp >= 2:
            mechanisms.append(f"retries amplified load up to {amp:.1f}×")
        if wasted >= 0.4:
            mechanisms.append(f"{wasted * 100:.0f}% of all work served requests nobody was waiting for")
        if params.trigger["kind"] == "cache_flush":
            mechanisms.append("the cache could not re-warm because its refills depend on the overloaded database")
        if mechanisms:
            findings.append(
                {
                    "rule": "sustaining-mechanism",
                    "severity": "high",
                    "title": "What keeps the failure alive",
                    "detail": "In the failing cells, " + "; ".join(mechanisms) + ".",
                    "evidence": {"peak_amplification": amp, "wasted_fraction": round(wasted, 4)},
                }
            )

    return {
        "safe_cells": safe,
        "total_cells": total,
        "safe_fraction": round(safe / total, 4) if total else 0.0,
        "boundary": boundary_rows,
        "findings": findings,
    }
