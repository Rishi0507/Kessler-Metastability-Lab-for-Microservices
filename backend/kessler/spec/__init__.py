from __future__ import annotations

from typing import Any

from ..engine import SpecInvalid, get_engine
from .compiler import CompileResult, SpecIssue, build_engine_spec, canonical_json, compile_text, sha256_hex, task_hash
from .lint import Finding, lint, merge_patches, score
from .models import SystemSpec
from .patch import PatchError, apply_patch

__all__ = [
    "CompileResult",
    "Finding",
    "PatchError",
    "SpecIssue",
    "SystemSpec",
    "apply_patch",
    "build_engine_spec",
    "canonical_json",
    "check",
    "compile_text",
    "merge_patches",
    "sha256_hex",
    "task_hash",
    "topology",
]


def topology(spec: SystemSpec, analysis: dict[str, Any]) -> dict[str, Any]:
    info = {s["name"]: s for s in analysis.get("services", [])}
    nodes = [{"id": "client", "kind": "client", "label": "clients", "rps": spec.traffic.rps}]
    for name, s in spec.services.items():
        i = info.get(name, {})
        nodes.append(
            {
                "id": name,
                "kind": "cache" if s.cache else "service",
                "label": name,
                "description": s.description,
                "instances": s.instances,
                "concurrency": s.concurrency,
                "utilization": i.get("utilization", 0.0),
                "capacity_rps": i.get("capacity_rps", 0.0),
                "arrival_rps": i.get("arrival_rps", 0.0),
                "hold_ms": i.get("hold_ms", 0.0),
                "queue": {"capacity": s.queue.capacity, "discipline": s.queue.discipline},
                "autoscale": bool(s.autoscale),
            }
        )
    edges = []
    for e in analysis.get("edges", []):
        edges.append(
            {
                "from": e["from"],
                "to": e["to"],
                "probability": e["probability"],
                "timeout_ms": e["timeout_ms"],
                "attempts": e["max_attempts"],
                "jitter": e["jitter"],
                "budget": e["budget"],
                "breaker": e["breaker"],
            }
        )
    return {"nodes": nodes, "edges": edges, "entry": spec.traffic.entry, "bottleneck": analysis.get("bottleneck")}


def check(yaml_text: str) -> dict[str, Any]:
    res: CompileResult = compile_text(yaml_text)
    if not res.ok or res.spec is None or res.system is None:
        return {"ok": False, "errors": [e.as_dict() for e in res.errors]}
    probe = build_engine_spec(res.system, {"seed": 1, "horizon_s": 60, "warmup_s": 0, "load": 1.0, "faults": []})
    try:
        analysis = get_engine().analyze(probe)
    except SpecInvalid as exc:
        return {"ok": False, "errors": [{"path": "", "message": str(exc), "line": None}]}
    findings = lint(res.spec, analysis)
    return {
        "ok": True,
        "errors": [],
        "name": res.spec.name,
        "description": res.spec.description,
        "spec_hash": res.spec_hash,
        "system": res.system,
        "analysis": analysis,
        "findings": [f.as_dict() for f in findings],
        "score": score(findings),
        "topology": topology(res.spec, analysis),
    }
