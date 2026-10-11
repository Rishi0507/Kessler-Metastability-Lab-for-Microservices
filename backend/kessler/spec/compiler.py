from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import ValidationError

from .models import SystemSpec

MAX_SPEC_BYTES = 64 * 1024


@dataclass
class SpecIssue:
    path: str
    message: str
    line: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "message": self.message, "line": self.line}


@dataclass
class CompileResult:
    spec: SystemSpec | None = None
    system: dict[str, Any] | None = None
    spec_hash: str | None = None
    errors: list[SpecIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and self.system is not None


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _line_index(text: str) -> dict[tuple[str, ...], int]:
    index: dict[tuple[str, ...], int] = {}
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:
        return index

    def walk(n: Any, path: tuple[str, ...]) -> None:
        if isinstance(n, yaml.MappingNode):
            for k, v in n.value:
                p = path + (str(k.value),)
                index[p] = k.start_mark.line + 1
                walk(v, p)
        elif isinstance(n, yaml.SequenceNode):
            for i, v in enumerate(n.value):
                p = path + (str(i),)
                index[p] = v.start_mark.line + 1
                walk(v, p)

    walk(node, ())
    return index


def _suggest(bad: str, model_fields: list[str]) -> str:
    close = difflib.get_close_matches(bad, model_fields, n=1, cutoff=0.6)
    return f" (did you mean '{close[0]}'?)" if close else ""


def _known_fields(loc: tuple[Any, ...]) -> list[str]:
    from typing import get_args, get_origin

    from .models import SystemSpec

    current: Any = SystemSpec
    parts = list(loc[:-1])
    i = 0
    while i < len(parts):
        fields = getattr(current, "model_fields", None)
        part = parts[i]
        if not fields or part not in fields:
            return []
        ann = fields[part].annotation
        origin = get_origin(ann)
        if origin is dict:
            current = _model_from_annotation(get_args(ann)[1])
            i += 2
            continue
        if origin is list:
            current = _model_from_annotation(get_args(ann)[0])
            i += 2 if i + 1 < len(parts) and isinstance(parts[i + 1], int) else 1
            continue
        current = _model_from_annotation(ann)
        i += 1
    fields = getattr(current, "model_fields", None)
    return list(fields) if fields else []


def _model_from_annotation(ann: Any) -> Any:
    from typing import get_args

    from pydantic import BaseModel

    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann
    for arg in get_args(ann):
        found = _model_from_annotation(arg)
        if found is not None:
            return found
    return None


def parse_yaml(text: str) -> tuple[Any, list[SpecIssue]]:
    if len(text.encode()) > MAX_SPEC_BYTES:
        return None, [SpecIssue("", f"spec is larger than {MAX_SPEC_BYTES // 1024} KB")]
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        line = getattr(getattr(exc, "problem_mark", None), "line", None)
        problem = getattr(exc, "problem", None) or str(exc)
        return None, [SpecIssue("", f"YAML syntax error: {problem}", line + 1 if line is not None else None)]
    if not isinstance(data, dict):
        return None, [SpecIssue("", "the spec must be a YAML mapping with name, traffic and services")]
    return data, []


def validate(text: str) -> tuple[SystemSpec | None, list[SpecIssue]]:
    data, issues = parse_yaml(text)
    if issues:
        return None, issues
    try:
        return SystemSpec.model_validate(data), []
    except ValidationError as exc:
        lines = _line_index(text)
        out: list[SpecIssue] = []
        for err in exc.errors(include_url=False):
            loc = tuple(err.get("loc", ()))
            path = ".".join(str(p) for p in loc)
            msg = err.get("msg", "invalid value")
            if err.get("type") == "missing" and loc:
                msg = f"missing required field '{loc[-1]}'"
            elif err.get("type") == "extra_forbidden" and loc:
                msg = f"unknown field '{loc[-1]}'" + _suggest(str(loc[-1]), _known_fields(loc))
            elif msg.startswith("Value error, "):
                msg = msg[len("Value error, ") :]
            line = None
            for k in range(len(loc), 0, -1):
                line = lines.get(tuple(str(p) for p in loc[:k]))
                if line:
                    break
            out.append(SpecIssue(path, msg, line))
        return None, out


def compile_system(spec: SystemSpec) -> dict[str, Any]:
    names = list(spec.services)
    services = []
    for name in names:
        s = spec.services[name]
        svc: dict[str, Any] = {
            "name": name,
            "instances": s.instances,
            "concurrency": s.concurrency,
            "queue": {"capacity": s.queue.capacity, "discipline": s.queue.discipline, "timeout_ms": s.queue.timeout},
            "service_time": s.latency.to_engine(),
            "error_rate": s.error_rate,
            "lb": s.lb,
            "calls": [],
        }
        if s.contention:
            svc["contention"] = {"knee": min(s.contention.knee, 0.99), "factor": s.contention.factor}
        for c in s.calls:
            call: dict[str, Any] = {"to": c.to, "probability": c.probability, "timeout_ms": c.timeout}
            if c.retry:
                call["retry"] = c.retry.to_engine()
            if c.budget:
                call["budget"] = c.budget.to_engine()
            if c.breaker:
                call["breaker"] = c.breaker.to_engine()
            svc["calls"].append(call)
        if s.cache:
            svc["cache"] = {
                "hit_ratio": s.cache.hit_ratio,
                "key_space": s.cache.key_space,
                "miss_target": s.cache.miss_target,
            }
        if s.autoscale:
            a = s.autoscale
            svc["autoscale"] = {
                "min": a.min,
                "max": a.max,
                "target_utilization": a.target_utilization,
                "interval_s": max(a.interval / 1000.0, 1.0),
                "provision_s": a.provision / 1000.0,
                "cooldown_s": a.cooldown / 1000.0,
            }
        services.append(svc)

    t = spec.traffic
    client: dict[str, Any] = {"timeout_ms": t.client.timeout, "slo_ms": t.client.slo or t.client.timeout}
    if t.client.retry:
        client["retry"] = t.client.retry.to_engine()
    if t.client.budget:
        client["budget"] = t.client.budget.to_engine()
    traffic: dict[str, Any] = {"rps": t.rps, "entry": t.entry or names[0], "client": client}
    if t.pattern and t.pattern.kind != "constant":
        p = t.pattern
        traffic["pattern"] = (
            {"kind": "step", "at_s": p.at / 1000.0, "factor": p.factor}
            if p.kind == "step"
            else {"kind": "diurnal", "amplitude": min(p.amplitude, 0.95), "period_s": max(p.period / 1000.0, 1.0)}
        )
    return {
        "network": {"latency_ms": spec.network.latency, "jitter_ms": min(spec.network.jitter, spec.network.latency)},
        "resilience": {
            "deadline_propagation": spec.resilience.deadline_propagation,
            "cancel_on_timeout": spec.resilience.cancel_on_timeout,
        },
        "traffic": traffic,
        "services": services,
    }


def compile_text(text: str) -> CompileResult:
    spec, issues = validate(text)
    if issues or spec is None:
        return CompileResult(errors=issues)
    system = compile_system(spec)
    return CompileResult(spec=spec, system=system, spec_hash=sha256_hex(canonical_json(system)))


def build_engine_spec(system: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    spec = json.loads(json.dumps(system))
    spec["seed"] = int(scenario.get("seed", 1))
    spec["horizon_s"] = float(scenario.get("horizon_s", 200))
    spec["warmup_s"] = float(scenario.get("warmup_s", 30))
    spec["window_s"] = float(scenario.get("window_s", 1.0))
    spec["traffic"]["load_multiplier"] = float(scenario.get("load", 1.0))
    spec["faults"] = list(scenario.get("faults", []))
    if "max_events" in scenario:
        spec["limits"] = {"max_events": int(scenario["max_events"])}
    return spec


def task_hash(spec_hash: str, scenario: dict[str, Any], engine_version: str) -> str:
    return sha256_hex(f"{engine_version}|{spec_hash}|{canonical_json(scenario)}")
