#!/usr/bin/env python3

from __future__ import annotations

import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from kessler.analysis.planner import SweepParams, linspace  # noqa: E402
from kessler.engine import get_engine  # noqa: E402
from kessler.spec import build_engine_spec, compile_text  # noqa: E402
from kessler.spec.templates import get_template  # noqa: E402

OUT = ROOT / "docs" / "img" / "metastability-maps.svg"
COLORS = {"stable": "#1a9a7c", "slow": "#c98a12", "metastable": "#cc2f55", "overloaded": "#7c8296"}


def sweep(template_id: str, params: SweepParams) -> list[str]:
    res = compile_text(get_template(template_id).yaml)
    assert res.ok and res.system
    engine = get_engine()
    jobs = [(c, s) for c in range(params.n_cells) for s in range(params.seeds)]

    def run(job: tuple[int, int]) -> tuple[int, dict]:
        cell, seed = job
        return cell, engine.run(build_engine_spec(res.system, params.scenario(cell, seed)))

    by_cell: dict[int, list[dict]] = {}
    with ThreadPoolExecutor() as pool:
        for cell, summary in pool.map(run, jobs):
            by_cell.setdefault(cell, []).append(summary)
    states = []
    for c in range(params.n_cells):
        sums = by_cell[c]
        bad = sum(s["verdict"] in ("metastable", "overloaded") for s in sums)
        over = sum(s["verdict"] == "overloaded" for s in sums)
        recs = [s["recovery_s"] for s in sums if s["recovery_s"] is not None]
        if over * 2 >= len(sums):
            states.append("overloaded")
        elif bad * 2 >= len(sums):
            states.append("metastable")
        elif recs and statistics.median(recs) > 10:
            states.append("slow")
        else:
            states.append("stable")
    return states


def draw(maps: list[tuple[str, list[str]]], params: SweepParams) -> str:
    cw, ch, gap, left, top = 30, 22, 2, 44, 34
    cols, rows = params.n_cols, params.n_rows
    mw = left + cols * (cw + gap) + 24
    W, H = mw * len(maps), top + rows * (ch + gap) + 64
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="system-ui, sans-serif" font-size="11">',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
    ]
    for m, (title, states) in enumerate(maps):
        ox = m * mw
        safe = sum(s in ("stable", "slow") for s in states)
        out.append(f'<text x="{ox + left}" y="18" font-size="13" font-weight="600" fill="#121826">{title}</text>')
        out.append(
            f'<text x="{ox + left}" y="{top - 6}" fill="#4a5468">{safe} of {len(states)} combinations recover</text>'
        )
        for r in range(rows):
            y = top + (rows - 1 - r) * (ch + gap)
            out.append(
                f'<text x="{ox + left - 6}" y="{y + 15}" text-anchor="end" fill="#4a5468">{params.values[r]:g}×</text>'
            )
            for c in range(cols):
                x = ox + left + c * (cw + gap)
                out.append(
                    f'<rect x="{x}" y="{y}" width="{cw}" height="{ch}" rx="3" fill="{COLORS[states[r * cols + c]]}"/>'
                )
        by = top + rows * (ch + gap)
        for c in range(0, cols, 3):
            out.append(
                f'<text x="{ox + left + c * (cw + gap) + cw / 2}" y="{by + 14}" text-anchor="middle" fill="#4a5468">{params.loads[c]:g}×</text>'
            )
        out.append(
            f'<text x="{ox + left + cols * (cw + gap) / 2}" y="{by + 32}" text-anchor="middle" fill="#121826">load (multiple of today) → ; rows: database slowdown</text>'
        )
    lx = 44
    for name, col in (
        ("recovers", "stable"),
        ("recovers slowly", "slow"),
        ("never recovers", "metastable"),
        ("overloaded", "overloaded"),
    ):
        out.append(f'<rect x="{lx}" y="{H - 16}" width="10" height="10" rx="2" fill="{COLORS[col]}"/>')
        out.append(f'<text x="{lx + 15}" y="{H - 7}" fill="#4a5468">{name}</text>')
        lx += 15 + 7 * len(name) + 18
    out.append("</svg>")
    return "\n".join(out)


def main() -> None:
    params = SweepParams(
        trigger={"kind": "latency", "target": "db", "duration_s": 20},
        loads=linspace(0.5, 3.2, 10),
        values=linspace(1.0, 8.0, 6),
        axis2="magnitude",
        seeds=2,
        mode="grid",
    )
    t0 = time.perf_counter()
    before = sweep("checkout-retry-storm", params)
    after = sweep("checkout-hardened", params)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        draw([("Retries at every layer", before), ("Hardened (Kessler's fixes)", after)], params), encoding="utf-8"
    )
    print(f"{OUT}: {2 * params.n_cells * params.seeds} simulations in {time.perf_counter() - t0:.1f}s")
    print(
        "safe before:",
        sum(s in ("stable", "slow") for s in before),
        "after:",
        sum(s in ("stable", "slow") for s in after),
    )


if __name__ == "__main__":
    main()
