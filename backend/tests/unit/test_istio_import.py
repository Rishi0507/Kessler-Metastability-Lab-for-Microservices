from __future__ import annotations

from pathlib import Path

import pytest

from kessler.spec import compile_text
from kessler.spec.istio import MeshImportError, import_istio

SPEC = (Path(__file__).parents[2] / "kessler/spec/templates/checkout-retry-storm.yaml").read_text()

MESH = """
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata: {name: db}
spec:
  hosts: [db.shop.svc.cluster.local]
  http:
    - route:
        - destination: {host: db.shop.svc.cluster.local}
      timeout: 2s
      retries: {attempts: 1, perTryTimeout: 300ms, retryOn: 5xx}
---
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata: {name: inventory}
spec:
  hosts: [inventory]
  http:
    - route:
        - destination: {host: inventory}
      timeout: 150ms
---
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata: {name: db}
spec:
  host: db.shop.svc.cluster.local
  trafficPolicy:
    loadBalancer: {simple: LEAST_REQUEST}
    outlierDetection: {consecutive5xxErrors: 5, interval: 5s, baseEjectionTime: 30s}
"""


def _spec(text: str):
    res = compile_text(text)
    assert res.ok, res.errors
    return res.spec


def test_maps_routes_and_rules_onto_calls() -> None:
    out, notes = import_istio(SPEC, MESH)
    spec = _spec(out)
    checkout = {c.to: c for c in spec.services["checkout"].calls}
    assert checkout["db"].timeout == 300
    assert checkout["db"].retry.attempts == 2
    assert checkout["db"].retry.on_timeout
    assert checkout["db"].breaker.open_for == 30_000
    assert spec.services["db"].lb == "p2c"
    assert checkout["inventory"].timeout == 150
    assert checkout["inventory"].retry.attempts == 3
    assert any("default of 2 retries" in n for n in notes)
    assert "Checkout with retries everywhere" in out


def test_keeps_untouched_lines() -> None:
    out, _ = import_istio(SPEC, MESH)
    assert out.splitlines()[:12] == SPEC.splitlines()[:12]


def test_reports_unknown_hosts() -> None:
    cfg = "kind: DestinationRule\nmetadata: {name: x}\nspec: {host: payments}\n"
    out, notes = import_istio(SPEC, cfg)
    assert out == SPEC
    assert "payments" in notes[0]


def test_rejects_config_without_istio_objects() -> None:
    with pytest.raises(MeshImportError):
        import_istio(SPEC, "kind: Service\nmetadata: {name: x}\n")


def test_rejects_bad_yaml() -> None:
    with pytest.raises(MeshImportError):
        import_istio(SPEC, "kind: [")
