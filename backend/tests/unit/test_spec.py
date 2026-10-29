from __future__ import annotations

import pytest

from kessler.spec import apply_patch, check, compile_text, merge_patches
from kessler.spec.compiler import validate
from kessler.spec.lint import Finding, effective_amplification
from kessler.spec.models import SystemSpec, _duration_ms, _fraction
from kessler.spec.templates import all_templates, get_template

pytestmark = pytest.mark.engine

MINI = """
name: mini
traffic: {rps: 100}
services:
  api:
    latency: {p50: 2ms, p99: 10ms}
    calls:
      - to: db
        timeout: 250ms
        retry: {attempts: 3, base: 20ms, jitter: none}
  db:
    latency: {mean: 5ms}
"""


@pytest.mark.parametrize(
    ("raw", "ms"),
    [(250, 250.0), ("250ms", 250.0), ("1.5s", 1500.0), ("2m", 120_000.0), ("500us", 0.5), (" 3 s ", 3000.0)],
)
def test_durations_accept_units(raw, ms):
    assert _duration_ms(raw) == pytest.approx(ms)


@pytest.mark.parametrize("raw", ["fast", "10 parsecs", True, None])
def test_durations_reject_garbage(raw):
    with pytest.raises((ValueError, TypeError)):
        _duration_ms(raw)


def test_fractions_accept_percentages():
    assert _fraction("10%") == pytest.approx(0.1)
    assert _fraction(0.25) == pytest.approx(0.25)
    assert _fraction("0.5") == pytest.approx(0.5)


def test_latency_p50_only_defaults_tail_to_4x():
    spec, issues = validate("name: x\ntraffic: {rps: 1}\nservices:\n  a: {latency: {p50: 3ms}}\n")
    assert not issues and spec is not None
    assert spec.services["a"].latency.p99 == pytest.approx(12.0)


def test_compile_is_canonical_and_hash_ignores_formatting():
    a = compile_text(MINI)
    b = compile_text(MINI.replace("    latency: {mean: 5ms}", "    # the database\n    latency:\n      mean: 5ms"))
    assert a.ok and b.ok
    assert a.spec_hash == b.spec_hash
    assert a.system["services"][0]["calls"][0] == {
        "to": "db",
        "probability": 1.0,
        "timeout_ms": 250.0,
        "retry": {
            "max_attempts": 3,
            "base_ms": 20.0,
            "cap_ms": 1000.0,
            "jitter": "none",
            "on_timeout": True,
            "on_error": True,
        },
    }
    c = compile_text(MINI.replace("timeout: 250ms", "timeout: 300ms"))
    assert c.spec_hash != a.spec_hash


def test_errors_carry_paths_lines_and_suggestions():
    res = check(MINI.replace("timeout: 250ms", "timout: 250ms"))
    assert not res["ok"]
    err = res["errors"][0]
    assert err["path"] == "services.api.calls.0.timout"
    assert "did you mean 'timeout'" in err["message"]
    assert err["line"] == 9


@pytest.mark.parametrize(
    ("yaml_text", "needle"),
    [
        ("name: x\ntraffic: {rps: 1, entry: nope}\nservices:\n  a: {latency: {p50: 1ms}}\n", "unknown service 'nope'"),
        ("name: x\ntraffic: {rps: 1}\nservices:\n  a: {latency: {p50: 1ms}, calls: [{to: a}]}\n", "cannot call itself"),
        ("name: x\ntraffic: {rps: 1}\nservices:\n  Bad Name: {latency: {p50: 1ms}}\n", "service name"),
        ("name: x\ntraffic: {rps: 1}\nservices:\n  a: {latency: {p50: 5ms, p99: 1ms}}\n", "p99 must be >= p50"),
        ("- just\n- a list\n", "YAML mapping"),
        ("name: [unclosed\n", "YAML syntax error"),
    ],
)
def test_invalid_specs_explain_themselves(yaml_text, needle):
    res = check(yaml_text)
    assert not res["ok"]
    assert any(needle in e["message"] for e in res["errors"]), res["errors"]


def test_every_template_compiles_and_has_topology():
    for t in all_templates():
        res = check(t.yaml)
        assert res["ok"], (t.id, res.get("errors"))
        ids = {n["id"] for n in res["topology"]["nodes"]}
        assert "client" in ids and res["topology"]["entry"] in ids


def test_lint_flags_the_retry_storm_and_clears_the_hardened_config():
    storm = check(get_template("checkout-retry-storm").yaml)
    rules = {f["rule"] for f in storm["findings"]}
    assert {"retry-amplification", "no-jitter", "timeout-inversion", "queue-outlives-timeout"} <= rules
    assert storm["score"]["grade"] in {"D", "F"}
    hardened = check(get_template("checkout-hardened").yaml)
    assert not [f for f in hardened["findings"] if f["severity"] in {"critical", "high", "medium"}]
    assert hardened["score"]["grade"] == "A"


def test_budgets_cap_effective_amplification():
    spec = SystemSpec.model_validate(
        {
            "name": "x",
            "traffic": {"rps": 10, "client": {"retry": {"attempts": 3}, "budget": "10%"}},
            "services": {
                "a": {"latency": {"p50": 1}, "calls": [{"to": "b", "retry": {"attempts": 3}}]},
                "b": {"latency": {"p50": 1}},
            },
        }
    )
    amp, path = effective_amplification(spec)
    assert amp == pytest.approx(1.1 * 3)
    assert path == ["client", "a", "b"]


def test_applying_all_fixes_improves_the_grade():
    storm = get_template("checkout-retry-storm").yaml
    before = check(storm)
    ops = merge_patches([Finding(**{k: v for k, v in f.items() if k != "id"}) for f in before["findings"]])
    fixed_yaml = apply_patch(storm, ops)
    after = check(fixed_yaml)
    assert after["ok"], after.get("errors")
    assert after["score"]["score"] > before["score"]["score"]
    assert after["system"]["resilience"]["deadline_propagation"] is True


def _changed_lines(before: str, after: str) -> list[str]:
    import difflib

    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


@pytest.mark.parametrize("template", [t.id for t in all_templates()])
def test_patching_nothing_returns_the_file_unchanged(template):
    text = get_template(template).yaml
    assert apply_patch(text, []) == text


def test_a_fix_changes_only_the_line_it_is_about():
    storm = get_template("checkout-retry-storm").yaml
    fixed = apply_patch(storm, [{"op": "set", "path": ["traffic", "client", "retry", "jitter"], "value": "full"}])
    assert _changed_lines(storm, fixed) == [
        "-    retry: { attempts: 3, base: 100ms, cap: 400ms, jitter: none }",
        "+    retry: { attempts: 3, base: 100ms, cap: 400ms, jitter: full }",
    ]


def test_new_small_mappings_are_written_inline():
    storm = get_template("checkout-retry-storm").yaml
    breaker = {"failure_rate": "50%", "window": "10s", "open_for": "5s"}
    fixed = apply_patch(
        storm, [{"op": "set", "path": ["services", "checkout", "calls", 1, "breaker"], "value": breaker}]
    )
    assert _changed_lines(storm, fixed) == ["+        breaker: { failure_rate: 50%, window: 10s, open_for: 5s }"]
    spec = compile_text(fixed).spec
    assert spec is not None and spec.services["checkout"].calls[1].breaker is not None


def test_patches_keep_comments_and_other_indentation_styles():
    text = (
        "# my checkout\n"
        "name: mini\n"
        "traffic: {rps: 100}  # peak\n"
        "services:\n"
        "    api:\n"
        "        concurrency: 8\n"
        "        calls:\n"
        "        - to: db\n"
        "          retry: {attempts: 3}\n"
        "    db:\n"
        "        concurrency: 4\n"
    )
    assert apply_patch(text, []) == text
    fixed = apply_patch(
        text, [{"op": "set", "path": ["services", "api", "calls", 0, "retry", "jitter"], "value": "full"}]
    )
    assert _changed_lines(text, fixed) == [
        "-          retry: {attempts: 3}",
        "+          retry: {attempts: 3, jitter: full}",
    ]


def test_patching_reports_bad_input_instead_of_crashing():
    from kessler.spec import PatchError

    with pytest.raises(PatchError, match="not valid YAML"):
        apply_patch("name: [unclosed\n", [])
    with pytest.raises(PatchError, match="mapping"):
        apply_patch("- just a list\n", [])
    with pytest.raises(PatchError, match="inside a scalar"):
        apply_patch("name: x\n", [{"op": "set", "path": ["name", "deeper"], "value": 1}])
