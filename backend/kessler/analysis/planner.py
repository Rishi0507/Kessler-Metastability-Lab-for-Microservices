from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

AXIS2 = Literal["magnitude", "duration"]

TRIGGER_DEFAULTS: dict[str, dict[str, Any]] = {
    "latency": {"axis2": "magnitude", "range": (1.0, 8.0), "duration_s": 20.0, "unit": "× slower"},
    "errors": {"axis2": "magnitude", "range": (0.1, 1.0), "duration_s": 20.0, "unit": "error rate"},
    "capacity_loss": {"axis2": "magnitude", "range": (0.1, 0.8), "duration_s": 20.0, "unit": "capacity lost"},
    "spike": {"axis2": "magnitude", "range": (1.2, 4.0), "duration_s": 30.0, "unit": "× traffic"},
    "crash": {"axis2": "duration", "range": (5.0, 60.0), "magnitude": 1.0, "unit": "s outage"},
    "cache_flush": {"axis2": "duration", "range": (0.0, 0.0), "magnitude": 1.0, "unit": ""},
}


@dataclass(frozen=True)
class SweepParams:
    trigger: dict[str, Any]
    loads: tuple[float, ...]
    values: tuple[float, ...]
    axis2: AXIS2
    seeds: int = 3
    mode: Literal["adaptive", "grid"] = "adaptive"
    warmup_s: float = 30.0
    trigger_start_s: float = 60.0
    observe_s: float = 120.0
    base_seed: int = 1

    @property
    def n_cols(self) -> int:
        return len(self.loads)

    @property
    def n_rows(self) -> int:
        return len(self.values)

    @property
    def n_cells(self) -> int:
        return self.n_cols * self.n_rows

    def cell(self, row: int, col: int) -> int:
        return row * self.n_cols + col

    def coords(self, cell: int) -> tuple[int, int]:
        return divmod(cell, self.n_cols)

    def fault(self, row: int) -> dict[str, Any]:
        t = dict(self.trigger)
        kind = t["kind"]
        f: dict[str, Any] = {"kind": kind, "start_s": self.trigger_start_s}
        if t.get("target"):
            f["target"] = t["target"]
        if self.axis2 == "magnitude":
            f["magnitude"] = self.values[row]
            f["duration_s"] = float(t.get("duration_s", TRIGGER_DEFAULTS.get(kind, {}).get("duration_s", 20.0)))
        else:
            f["duration_s"] = self.values[row]
            f["magnitude"] = float(t.get("magnitude", TRIGGER_DEFAULTS.get(kind, {}).get("magnitude", 1.0)))
        if kind == "crash" and t.get("count"):
            f["count"] = int(t["count"])
        return f

    def horizon_s(self, row: int) -> float:
        f = self.fault(row)
        return self.trigger_start_s + float(f.get("duration_s", 0.0)) + self.observe_s

    def scenario(self, cell: int, seed_index: int) -> dict[str, Any]:
        row, col = self.coords(cell)
        return {
            "seed": self.base_seed + seed_index,
            "horizon_s": self.horizon_s(row),
            "warmup_s": self.warmup_s,
            "load": self.loads[col],
            "faults": [self.fault(row)],
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "loads": list(self.loads),
            "values": list(self.values),
            "axis2": self.axis2,
            "seeds": self.seeds,
            "mode": self.mode,
            "warmup_s": self.warmup_s,
            "trigger_start_s": self.trigger_start_s,
            "observe_s": self.observe_s,
            "base_seed": self.base_seed,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> SweepParams:
        return SweepParams(
            trigger=dict(d["trigger"]),
            loads=tuple(float(x) for x in d["loads"]),
            values=tuple(float(x) for x in d["values"]),
            axis2=d["axis2"],
            seeds=int(d.get("seeds", 3)),
            mode=d.get("mode", "adaptive"),
            warmup_s=float(d.get("warmup_s", 30.0)),
            trigger_start_s=float(d.get("trigger_start_s", 60.0)),
            observe_s=float(d.get("observe_s", 120.0)),
            base_seed=int(d.get("base_seed", 1)),
        )


def linspace(lo: float, hi: float, n: int) -> tuple[float, ...]:
    if n <= 1:
        return (round(lo, 4),)
    return tuple(round(lo + (hi - lo) * i / (n - 1), 4) for i in range(n))


BAD_VERDICTS = frozenset({"metastable", "overloaded"})


def cell_is_bad(summaries: list[dict[str, Any]]) -> bool:
    bad = sum(1 for s in summaries if s.get("verdict") in BAD_VERDICTS)
    return bad * 2 >= len(summaries) > 0


def aggregate_cell(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(summaries)
    if n == 0:
        return {}
    meta = sum(1 for s in summaries if s.get("verdict") == "metastable")
    over = sum(1 for s in summaries if s.get("verdict") == "overloaded")
    recs = sorted(float(s["recovery_s"]) for s in summaries if s.get("recovery_s") is not None)
    med = recs[len(recs) // 2] if recs else None
    return {
        "seeds": n,
        "p_metastable": round(meta / n, 4),
        "p_overloaded": round(over / n, 4),
        "bad": cell_is_bad(summaries),
        "recovery_s": med,
        "peak_amplification": round(max(float(s.get("peak_amplification") or 0) for s in summaries), 3),
        "wasted_fraction": round(sum(float(s.get("wasted_fraction") or 0) for s in summaries) / n, 4),
        "min_goodput_ratio": round(min(float(s.get("min_goodput_ratio") or 0) for s in summaries), 4),
        "lost_requests": round(sum(float(s.get("lost_requests") or 0) for s in summaries) / n),
    }


def row_next(n: int, ev: dict[int, bool | None]) -> list[int]:
    if any(v is None for v in ev.values()):
        return []
    if n - 1 not in ev:
        return [n - 1]
    if ev[n - 1] is False:
        return []
    bad_cols = sorted(c for c, v in ev.items() if v)
    b = bad_cols[0]
    goods = [c for c, v in ev.items() if v is False and c < b]
    g = max(goods) if goods else -1
    if b - g <= 1:
        return []
    if g == -1 and 0 not in ev:
        mid = b // 2
        return sorted({0, mid})
    return [(g + b) // 2]


def plan_next(params: SweepParams, evaluated: dict[int, bool | None]) -> list[int]:
    if params.mode == "grid":
        return [c for c in range(params.n_cells) if c not in evaluated]
    out: list[int] = []
    for row in range(params.n_rows):
        ev = {}
        for col in range(params.n_cols):
            c = params.cell(row, col)
            if c in evaluated:
                ev[col] = evaluated[c]
        out.extend(params.cell(row, col) for col in row_next(params.n_cols, ev))
    return out


def is_complete(params: SweepParams, evaluated: dict[int, bool | None]) -> bool:
    return not any(v is None for v in evaluated.values()) and not plan_next(params, evaluated)


def infer(params: SweepParams, evaluated: dict[int, bool]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in range(params.n_rows):
        cols = {
            col: evaluated[params.cell(row, col)] for col in range(params.n_cols) if params.cell(row, col) in evaluated
        }
        bad_cols = sorted(c for c, v in cols.items() if v)
        first_bad = bad_cols[0] if bad_cols else params.n_cols
        for col in range(params.n_cols):
            c = params.cell(row, col)
            if col in cols:
                out[c] = {"bad": cols[col], "inferred": False}
            else:
                out[c] = {"bad": col >= first_bad, "inferred": True}
    return out


def boundary(params: SweepParams, verdicts: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in range(params.n_rows):
        tip = None
        for col in range(params.n_cols):
            if verdicts.get(params.cell(row, col), {}).get("bad"):
                tip = params.loads[col]
                break
        rows.append({"value": params.values[row], "tipping_load": tip})
    return rows


def savings(params: SweepParams, evaluated_cells: int) -> dict[str, Any]:
    full = params.n_cells * params.seeds
    done = evaluated_cells * params.seeds
    return {
        "full_grid_sims": full,
        "simulated_sims": done,
        "avoided_fraction": round(1 - done / full, 4) if full else 0.0,
    }


def default_params(
    trigger: dict[str, Any],
    max_load_multiplier: float,
    *,
    seeds: int = 3,
    mode: str = "adaptive",
    cols: int = 10,
    rows: int = 6,
) -> SweepParams:
    kind = trigger["kind"]
    d = TRIGGER_DEFAULTS.get(kind, TRIGGER_DEFAULTS["latency"])
    sat = max_load_multiplier if math.isfinite(max_load_multiplier) and max_load_multiplier > 0 else 1.0
    lo, hi = round(0.3 * sat, 3), round(1.2 * sat, 3)
    loads = linspace(lo, hi, cols)
    axis2: AXIS2 = d["axis2"]
    if kind == "cache_flush":
        values: tuple[float, ...] = (0.0,)
    else:
        a, b = trigger.get("range", d["range"])
        values = linspace(float(a), float(b), rows)
    return SweepParams(
        trigger={k: v for k, v in trigger.items() if k != "range"},
        loads=loads,
        values=values,
        axis2=axis2,
        seeds=seeds,
        mode=mode,  # type: ignore[arg-type]
    )


@dataclass
class PlanState:
    params: SweepParams
    evaluated: dict[int, bool | None] = field(default_factory=dict)

    def next_cells(self) -> list[int]:
        return plan_next(self.params, self.evaluated)

    def complete(self) -> bool:
        return is_complete(self.params, self.evaluated)
