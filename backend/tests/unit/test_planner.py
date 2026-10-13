from __future__ import annotations

import pytest

from kessler.analysis.planner import (
    SweepParams,
    aggregate_cell,
    boundary,
    cell_is_bad,
    default_params,
    infer,
    is_complete,
    plan_next,
    row_next,
    savings,
)


def _params(rows=6, cols=10, mode="adaptive") -> SweepParams:
    return SweepParams(
        trigger={"kind": "latency", "target": "db", "duration_s": 20},
        loads=tuple(0.3 + 0.1 * i for i in range(cols)),
        values=tuple(float(1 + i) for i in range(rows)),
        axis2="magnitude",
        seeds=3,
        mode=mode,
    )


def _run(params: SweepParams, oracle) -> dict[int, bool]:
    evaluated: dict[int, bool | None] = {}
    rounds = 0
    while True:
        nxt = plan_next(params, evaluated)
        if not nxt:
            break
        rounds += 1
        for c in nxt:
            assert c not in evaluated, "a cell must never be scheduled twice"
            evaluated[c] = None
        for c in nxt:
            row, col = params.coords(c)
            evaluated[c] = oracle(row, col)
        assert rounds < 50
    assert is_complete(params, evaluated)
    return {c: bool(v) for c, v in evaluated.items()}


def test_row_probes_highest_load_first_and_stops_when_safe():
    assert row_next(10, {}) == [9]
    assert row_next(10, {9: False}) == []


def test_row_waits_for_in_flight_results():
    assert row_next(10, {9: None}) == []


def test_row_that_is_bad_everywhere_needs_three_probes():
    ev = {9: True}
    assert row_next(10, ev) == [0, 4]
    assert row_next(10, {9: True, 0: True, 4: True}) == []


@pytest.mark.parametrize("tip", [1, 3, 6, 9])
def test_bisection_pins_the_boundary(tip):
    ev: dict[int, bool | None] = {}
    probes = 0
    while nxt := row_next(10, ev):
        for c in nxt:
            ev[c] = c >= tip
            probes += 1
    bad = sorted(c for c, v in ev.items() if v)
    good = sorted(c for c, v in ev.items() if v is False)
    assert bad[0] == tip and good[-1] == tip - 1
    assert probes <= 6 < 10


def test_adaptive_sweep_matches_full_grid_with_fewer_simulations():
    p = _params()

    def oracle(row, col):
        return col >= max(0, 9 - 2 * row)

    evaluated = _run(p, oracle)
    verdicts = infer(p, evaluated)
    for c in range(p.n_cells):
        row, col = p.coords(c)
        assert verdicts[c]["bad"] == oracle(row, col)
    assert savings(p, len(evaluated))["avoided_fraction"] > 0.4
    tips = [r["tipping_load"] for r in boundary(p, verdicts)]
    assert tips[0] == pytest.approx(p.loads[9]) and tips[-1] == pytest.approx(p.loads[0])


def test_grid_mode_schedules_everything_at_once():
    p = _params(rows=2, cols=3, mode="grid")
    assert plan_next(p, {}) == list(range(6))
    assert plan_next(p, {0: True}) == [1, 2, 3, 4, 5]


def test_scenarios_carry_load_fault_seed_and_horizon():
    p = _params()
    sc = p.scenario(p.cell(2, 4), seed_index=1)
    assert sc["load"] == pytest.approx(p.loads[4])
    assert sc["seed"] == 2
    assert sc["faults"] == [{"kind": "latency", "start_s": 60.0, "target": "db", "magnitude": 3.0, "duration_s": 20.0}]
    assert sc["horizon_s"] == pytest.approx(60 + 20 + 120)


def test_crash_sweeps_vary_duration():
    p = SweepParams(trigger={"kind": "crash", "target": "db"}, loads=(1.0,), values=(10.0, 30.0), axis2="duration")
    f = p.fault(1)
    assert f["duration_s"] == 30.0 and f["magnitude"] == 1.0


def test_default_axes_bracket_the_saturation_point():
    p = default_params({"kind": "latency", "target": "db", "duration_s": 20}, max_load_multiplier=3.44)
    assert p.loads[0] == pytest.approx(0.3 * 3.44, rel=1e-3)
    assert p.loads[-1] == pytest.approx(1.2 * 3.44, rel=1e-3)
    assert p.values[0] == 1.0 and p.values[-1] == 8.0
    flush = default_params({"kind": "cache_flush", "target": "cache"}, max_load_multiplier=10)
    assert flush.n_rows == 1


def test_params_round_trip_through_json():
    p = _params()
    assert SweepParams.from_dict(p.as_dict()) == p


def test_cell_aggregation_uses_majority_vote():
    meta = {"verdict": "metastable", "recovery_s": None, "peak_amplification": 4.2, "wasted_fraction": 0.8}
    ok = {"verdict": "recovered", "recovery_s": 3.0, "peak_amplification": 1.1, "wasted_fraction": 0.1}
    assert cell_is_bad([meta, meta, ok]) is True
    assert cell_is_bad([meta, ok, ok]) is False
    agg = aggregate_cell([meta, ok, ok])
    assert agg["p_metastable"] == pytest.approx(1 / 3, abs=1e-3)
    assert agg["recovery_s"] == 3.0
    assert agg["peak_amplification"] == 4.2
