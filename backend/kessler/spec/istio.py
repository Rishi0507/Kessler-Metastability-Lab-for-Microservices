from __future__ import annotations

import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .models import SystemSpec, _duration_ms
from .patch import apply_patch

_LB = {"ROUND_ROBIN": "round_robin", "RANDOM": "random", "LEAST_REQUEST": "p2c", "LEAST_CONN": "p2c"}
_TIMEOUT_RETRY_ON = re.compile(r"5xx|gateway-error|reset")
_ISTIO_BASE = "25ms"
_MAX_DOCS = 500


class MeshImportError(ValueError):
    pass


def _short(host: Any) -> str:
    return str(host or "").split(".")[0].lower()


def _name(doc: dict[str, Any]) -> str:
    return str((doc.get("metadata") or {}).get("name", "?"))


def _ms(v: Any) -> str | None:
    try:
        return f"{_duration_ms(v):g}ms"
    except ValueError:
        return None


def _docs(config: str) -> list[dict[str, Any]]:
    try:
        docs = [d for d in YAML(typ="safe").load_all(config) if isinstance(d, dict)]
    except YAMLError as exc:
        raise MeshImportError(f"config is not valid YAML: {exc}") from None
    out: list[dict[str, Any]] = []
    for d in docs:
        if d.get("kind") == "List":
            out.extend(i for i in d.get("items") or [] if isinstance(i, dict))
        else:
            out.append(d)
    return out[:_MAX_DOCS]


def _callers(spec: SystemSpec, target: str) -> list[tuple[str, int]]:
    return [(name, i) for name, s in spec.services.items() for i, c in enumerate(s.calls) if c.to == target]


def _virtual_service(spec: SystemSpec, doc: dict[str, Any], ops: list[dict[str, Any]], notes: list[str]) -> None:
    for route in (doc.get("spec") or {}).get("http") or []:
        dests = {_short((r.get("destination") or {}).get("host")) for r in route.get("route") or []}
        for target in dests:
            if target not in spec.services:
                notes.append(f"VirtualService {_name(doc)}: no service named '{target}' in the spec, skipped")
                continue
            retries = route.get("retries")
            per_try = _ms((retries or {}).get("perTryTimeout")) if retries else None
            timeout = per_try or _ms(route.get("timeout"))
            if retries is None:
                attempts, retry_on = 3, "connect-failure,refused-stream,unavailable,cancelled,retriable-status-codes"
                notes.append(f"{target}: no retries set, so Istio's default of 2 retries applies")
            else:
                attempts, retry_on = int(retries.get("attempts", 0)) + 1, str(retries.get("retryOn", ""))
            for caller, i in _callers(spec, target):
                base = ["services", caller, "calls", i]
                if timeout:
                    ops.append({"op": "set", "path": [*base, "timeout"], "value": timeout})
                if attempts > 1:
                    ops.append(
                        {
                            "op": "set",
                            "path": [*base, "retry"],
                            "value": {
                                "attempts": min(attempts, 10),
                                "base": _ISTIO_BASE,
                                "jitter": "full",
                                "on_timeout": bool(per_try) or bool(_TIMEOUT_RETRY_ON.search(retry_on)),
                            },
                        }
                    )
                else:
                    ops.append({"op": "set", "path": [*base, "retry"], "value": None})
            if per_try:
                notes.append(f"{target}: used perTryTimeout {per_try} as the call timeout")


def _destination_rule(spec: SystemSpec, doc: dict[str, Any], ops: list[dict[str, Any]], notes: list[str]) -> None:
    body = doc.get("spec") or {}
    target = _short(body.get("host"))
    if target not in spec.services:
        notes.append(f"DestinationRule {_name(doc)}: no service named '{target}' in the spec, skipped")
        return
    policy = body.get("trafficPolicy") or {}
    lb = _LB.get(str((policy.get("loadBalancer") or {}).get("simple", "")).upper())
    if lb:
        ops.append({"op": "set", "path": ["services", target, "lb"], "value": lb})
    outlier = policy.get("outlierDetection")
    if outlier:
        breaker = {
            "failure_rate": "50%",
            "window": _ms(outlier.get("interval", "10s")) or "10000ms",
            "open_for": _ms(outlier.get("baseEjectionTime", "30s")) or "30000ms",
        }
        for caller, i in _callers(spec, target):
            ops.append({"op": "set", "path": ["services", caller, "calls", i, "breaker"], "value": breaker})
        notes.append(f"{target}: outlier detection modelled as a circuit breaker on each call to it")
    pool = (policy.get("connectionPool") or {}).get("http") or {}
    if pool.get("http2MaxRequests") or pool.get("http1MaxPendingRequests"):
        notes.append(f"{target}: connection pool limits are not modelled")


def import_istio(spec_yaml: str, config: str) -> tuple[str, list[str]]:
    try:
        spec = SystemSpec.model_validate(YAML(typ="safe").load(spec_yaml))
    except Exception:
        raise MeshImportError("fix the errors in the spec before importing") from None
    ops: list[dict[str, Any]] = []
    notes: list[str] = []
    seen = 0
    for doc in _docs(config):
        kind = doc.get("kind")
        if kind == "VirtualService":
            _virtual_service(spec, doc, ops, notes)
        elif kind == "DestinationRule":
            _destination_rule(spec, doc, ops, notes)
        else:
            continue
        seen += 1
    if seen == 0:
        raise MeshImportError("no VirtualService or DestinationRule found")
    if not ops:
        return spec_yaml, notes or ["nothing in the config matched this system"]
    return apply_patch(spec_yaml, ops), notes
