from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .models import SystemSpec

Severity = Literal["critical", "high", "medium", "low", "info"]
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SEVERITY_WEIGHT = {"critical": 30, "high": 15, "medium": 7, "low": 3, "info": 0}


@dataclass
class Finding:
    rule: str
    severity: Severity
    title: str
    detail: str
    location: str
    evidence: dict[str, Any] = field(default_factory=dict)
    fix: str | None = None
    patch: list[dict[str, Any]] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.rule}:{self.location}"

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["id"] = self.id
        return d


def _fmt_ms(ms: float) -> str:
    if ms >= 10_000:
        return f"{ms / 1000:.0f} s"
    if ms >= 1000:
        return f"{ms / 1000:.1f} s"
    return f"{ms:.0f} ms" if ms >= 10 else f"{ms:.1f} ms"


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _call_index(spec: SystemSpec, frm: str, to: str) -> int | None:
    svc = spec.services.get(frm)
    if not svc:
        return None
    for i, c in enumerate(svc.calls):
        if c.to == to:
            return i
    return None


def _edge_path(spec: SystemSpec, frm: str, to: str, *rest: str) -> list[Any]:
    if frm == "client":
        return ["traffic", "client", *rest]
    idx = _call_index(spec, frm, to)
    return ["services", frm, "calls", idx, *rest]


def _layer_factor(retry: Any, budget: Any) -> float:
    attempts = retry.attempts if retry else 1
    if attempts <= 1:
        return 1.0
    return min(float(attempts), 1.0 + budget.ratio) if budget else float(attempts)


def effective_amplification(spec: SystemSpec) -> tuple[float, list[str]]:
    entry = spec.traffic.entry or next(iter(spec.services))
    best: dict[str, float] = {entry: _layer_factor(spec.traffic.client.retry, spec.traffic.client.budget)}
    parent: dict[str, str | None] = {entry: None}
    order: list[str] = []
    seen: set[str] = set()

    def visit(u: str) -> None:
        seen.add(u)
        for c in spec.services[u].calls:
            if c.to not in seen:
                visit(c.to)
        order.append(u)

    visit(entry)
    for u in reversed(order):
        for c in spec.services[u].calls:
            a = best.get(u, 0.0) * _layer_factor(c.retry, c.budget)
            if a > best.get(c.to, 0.0):
                best[c.to] = a
                parent[c.to] = u
    worst = max(best, key=lambda k: best[k])
    path: list[str] = []
    node: str | None = worst
    while node is not None:
        path.append(node)
        node = parent.get(node)
    path.append("client")
    return round(best[worst], 3), list(reversed(path))


def lint(spec: SystemSpec, analysis: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    svc_info = {s["name"]: s for s in analysis.get("services", [])}
    edges = analysis.get("edges", [])
    deadlines = bool(analysis.get("resilience", {}).get("deadline_propagation"))

    amp, path = effective_amplification(spec)
    raw_amp = float(analysis.get("worst_amplification", 1))
    if amp >= 4 and len(path) >= 2:
        sev: Severity = "critical" if amp >= 27 else "high" if amp >= 8 else "medium"
        last = path[-1]
        patch = []
        retrying = [e for e in edges if e["max_attempts"] > 1 and not e["budget"]]
        for e in retrying:
            patch.append({"op": "set", "path": _edge_path(spec, e["from"], e["to"], "budget"), "value": "10%"})
        out.append(
            Finding(
                rule="retry-amplification",
                severity=sev,
                title=f"Retries multiply to {amp:g}× along {' → '.join(path)}",
                detail=(
                    f"Every layer retries on its own. If {last} slows down, one client request can turn into "
                    f"{amp:g} attempts at {last}, arriving exactly when it is least able to serve them."
                ),
                location=" → ".join(path),
                evidence={
                    "effective_amplification": amp,
                    "raw_amplification": raw_amp,
                    "path": path,
                    "retrying_edges": len(retrying),
                },
                fix="Give every retrying edge a 10% retry budget so retries can never add more than 10% extra load.",
                patch=patch,
            )
        )

    inverted = []
    for e in edges:
        callee = spec.services.get(e["to"])
        if not callee or not callee.calls:
            continue
        worst = float(e["callee_worst_case_ms"])
        if e["timeout_ms"] < worst and not deadlines:
            inverted.append(e)
            ratio = worst / max(e["timeout_ms"], 1e-9)
            out.append(
                Finding(
                    rule="timeout-inversion",
                    severity="high" if ratio >= 2 else "medium",
                    title=f"{e['from']} gives up on {e['to']} before {e['to']} can finish",
                    detail=(
                        f"{e['from']} waits {_fmt_ms(e['timeout_ms'])}, but {e['to']} can keep working for up to "
                        f"{_fmt_ms(worst)} while it retries its own dependencies. Everything {e['to']} does after "
                        f"{e['from']} has given up is wasted, and that wasted work is what keeps a slow dependency "
                        "busy."
                    ),
                    location=f"{e['from']} → {e['to']}",
                    evidence={"caller_timeout_ms": e["timeout_ms"], "callee_worst_case_ms": worst},
                    fix="Propagate deadlines so each hop only spends the time its caller is still willing to wait.",
                    patch=[{"op": "set", "path": ["resilience", "deadline_propagation"], "value": True}],
                )
            )

    for e in edges:
        if e["max_attempts"] > 1 and e["jitter"] == "none":
            out.append(
                Finding(
                    rule="no-jitter",
                    severity="medium",
                    title=f"Retries from {e['from']} to {e['to']} are synchronised",
                    detail=(
                        "Without jitter, requests that failed together retry together, so a brief failure "
                        "comes back as waves of load at fixed intervals."
                    ),
                    location=f"{e['from']} → {e['to']}",
                    evidence={"backoff_base_ms": e["backoff_base_ms"], "max_attempts": e["max_attempts"]},
                    fix="Use full jitter: sleep a random time between zero and the backoff.",
                    patch=[
                        {"op": "set", "path": _edge_path(spec, e["from"], e["to"], "retry", "jitter"), "value": "full"}
                    ],
                )
            )

    if amp < 4:
        for e in edges:
            if e["max_attempts"] > 1 and not e["budget"]:
                out.append(
                    Finding(
                        rule="unbudgeted-retries",
                        severity="low",
                        title=f"Retries from {e['from']} to {e['to']} have no budget",
                        detail=(
                            f"During an incident each request may be tried {e['max_attempts']} times, adding up to "
                            f"{(e['max_attempts'] - 1) * 100}% extra load on {e['to']} when it is already struggling."
                        ),
                        location=f"{e['from']} → {e['to']}",
                        evidence={"max_attempts": e["max_attempts"]},
                        fix="Add a 10% retry budget.",
                        patch=[{"op": "set", "path": _edge_path(spec, e["from"], e["to"], "budget"), "value": "10%"}],
                    )
                )

    depth = len(path)
    if not deadlines and not inverted and depth >= 3:
        out.append(
            Finding(
                rule="no-deadlines",
                severity="low",
                title="Deadlines are not propagated",
                detail=(
                    f"Requests cross {depth - 1} hops and each one starts a fresh timeout, so deep services can keep "
                    "working on requests whose clients have already given up."
                ),
                location="resilience",
                evidence={"depth": depth},
                fix="Propagate deadlines through every hop.",
                patch=[{"op": "set", "path": ["resilience", "deadline_propagation"], "value": True}],
            )
        )

    caller_timeouts: dict[str, float] = {}
    for e in edges:
        caller_timeouts[e["to"]] = min(caller_timeouts.get(e["to"], math.inf), float(e["timeout_ms"]))
    for name, info in svc_info.items():
        timeout = caller_timeouts.get(name)
        wait = float(info.get("max_queue_wait_ms", 0))
        svc = spec.services[name]
        if timeout is None or wait <= timeout or svc.queue.timeout > 0:
            continue
        ratio = wait / timeout
        out.append(
            Finding(
                rule="queue-outlives-timeout",
                severity="high" if ratio >= 4 else "medium",
                title=f"{name}'s queue holds requests longer than callers wait",
                detail=(
                    f"A full queue at {name} holds about {_fmt_ms(wait)} of work, but callers give up after "
                    f"{_fmt_ms(timeout)}. Requests at the back of a full queue are already dead when they start, "
                    "and serving them wastes capacity."
                ),
                location=name,
                evidence={"max_queue_wait_ms": wait, "caller_timeout_ms": timeout},
                fix=(
                    f"Drop requests that waited longer than {_fmt_ms(timeout)} and serve the newest first "
                    "under pressure."
                ),
                patch=[
                    {"op": "set", "path": ["services", name, "queue", "timeout"], "value": f"{timeout:g}ms"},
                    {"op": "set", "path": ["services", name, "queue", "discipline"], "value": "adaptive_lifo"},
                ],
            )
        )

    for name, info in svc_info.items():
        u = float(info.get("utilization", 0))
        if u <= 0.7 or not info.get("reachable", True):
            continue
        svc = spec.services[name]
        want = max(svc.instances + 1, math.ceil(svc.instances * u / 0.6))
        out.append(
            Finding(
                rule="low-headroom",
                severity="critical" if u >= 1 else "high" if u >= 0.85 else "medium",
                title=f"{name} runs at {_pct(u)} of capacity at baseline",
                detail=(
                    f"{name} needs {info['arrival_rps']:.0f} rps of its estimated {info['capacity_rps']:.0f} rps. "
                    + (
                        "It is overloaded before anything goes wrong."
                        if u >= 1
                        else "With so little headroom a small slowdown downstream is enough to fill its queues."
                    )
                ),
                location=name,
                evidence={"utilization": u, "capacity_rps": info["capacity_rps"], "arrival_rps": info["arrival_rps"]},
                fix=f"Run {want} instances (about 60% utilisation).",
                patch=[{"op": "set", "path": ["services", name, "instances"], "value": want}]
                + (
                    [
                        {
                            "op": "set",
                            "path": ["services", name, "autoscale", "max"],
                            "value": max(want, svc.autoscale.max),
                        }
                    ]
                    if svc.autoscale
                    else []
                ),
            )
        )

    for name, svc in spec.services.items():
        if not svc.cache:
            continue
        info = svc_info.get(name, {})
        target = svc.cache.miss_target
        tinfo = svc_info.get(target, {})
        idx = _call_index(spec, name, target)
        prob = svc.calls[idx].probability if idx is not None else 1.0
        cold = float(info.get("arrival_rps", 0)) * prob
        cap = float(tinfo.get("capacity_rps", 0))
        warm = float(tinfo.get("arrival_rps", 0))
        if cap > 0 and cold > cap:
            out.append(
                Finding(
                    rule="cold-cache-collapse",
                    severity="high" if cold > 1.5 * cap else "medium",
                    title=f"A cold {name} would overload {target}",
                    detail=(
                        f"With a warm cache {target} sees about {warm:.0f} rps. If {name} is flushed or restarts, "
                        f"{target} receives about {cold:.0f} rps but can serve about {cap:.0f} rps, so it cannot "
                        "refill the cache it depends on."
                    ),
                    location=f"{name} → {target}",
                    evidence={"warm_rps": warm, "cold_rps": cold, "capacity_rps": cap},
                    fix=f"Budget retries on the miss path and drop stale work at {target} so the cache can re-warm.",
                    patch=[
                        {"op": "set", "path": ["services", name, "calls", idx, "budget"], "value": "10%"},
                        {
                            "op": "set",
                            "path": ["services", target, "queue", "timeout"],
                            "value": f"{svc.calls[idx].timeout:g}ms" if idx is not None else "200ms",
                        },
                    ],
                )
            )

    client_timeout = spec.traffic.client.timeout
    for name, svc in spec.services.items():
        a = svc.autoscale
        if a and a.provision > 10 * client_timeout:
            out.append(
                Finding(
                    rule="autoscale-lag",
                    severity="info",
                    title=f"Autoscaling cannot rescue {name} from a spike",
                    detail=(
                        f"New {name} instances arrive {_fmt_ms(a.provision)} after a scale-up decision, while clients "
                        f"give up after {_fmt_ms(client_timeout)}. Autoscaling covers sustained growth, not bursts; "
                        "bursts need headroom or load shedding."
                    ),
                    location=name,
                    evidence={"provision_ms": a.provision, "client_timeout_ms": client_timeout},
                )
            )

    fan_in: dict[str, int] = {}
    for s in spec.services.values():
        for c in s.calls:
            fan_in[c.to] = fan_in.get(c.to, 0) + 1
    for name, s in spec.services.items():
        for i, c in enumerate(s.calls):
            if fan_in.get(c.to, 0) >= 2 and c.retry and c.retry.attempts > 1 and not c.breaker:
                out.append(
                    Finding(
                        rule="no-breaker",
                        severity="low",
                        title=f"{name} keeps calling {c.to} while it is failing",
                        detail=(
                            f"{c.to} is shared by {fan_in[c.to]} callers. Without a circuit breaker, {name} keeps "
                            f"sending (and retrying) requests into an outage instead of failing fast."
                        ),
                        location=f"{name} → {c.to}",
                        evidence={"fan_in": fan_in[c.to]},
                        fix="Open a breaker at 50% failures over 10 s and probe again after 5 s.",
                        patch=[
                            {
                                "op": "set",
                                "path": ["services", name, "calls", i, "breaker"],
                                "value": {"failure_rate": "50%", "window": "10s", "open_for": "5s"},
                            }
                        ],
                    )
                )

    for name, info in svc_info.items():
        if not info.get("bistable") or not info.get("reachable", True):
            continue
        svc = spec.services[name]
        collapse = float(info.get("collapse_rps", 0))
        peak = float(info.get("capacity_rps", 0))
        arrival = float(info.get("arrival_rps", 0))
        knee = float(info.get("contention_knee", 0.7))
        in_band = arrival > collapse
        want = max(svc.instances + 1, math.ceil(svc.instances * (arrival / max(peak, 1e-9)) / 0.6))
        out.append(
            Finding(
                rule="congestion-collapse",
                severity="high" if in_band else "medium" if arrival > 0.6 * collapse else "low",
                title=f"{name} gets slower the busier it is",
                detail=(
                    f"Past {knee * 100:.0f}% busy, every {name} request takes longer, so its throughput peaks at about "
                    f"{peak:.0f} rps and falls to about {collapse:.0f} rps once it is overloaded. "
                    + (
                        f"Today it serves {arrival:.0f} rps, inside the band where one burst can tip it into the "
                        "slow state and keep it there."
                        if in_band
                        else f"Today it serves {arrival:.0f} rps; a trigger that pushes it past the knee can leave it "
                        "stuck in the slow state."
                    )
                ),
                location=name,
                evidence={"peak_rps": peak, "collapse_rps": collapse, "arrival_rps": arrival, "knee": knee},
                fix=f"Run {want} instances so normal load stays well below the knee.",
                patch=[{"op": "set", "path": ["services", name, "instances"], "value": want}]
                + (
                    [
                        {
                            "op": "set",
                            "path": ["services", name, "autoscale", "max"],
                            "value": max(want, svc.autoscale.max),
                        }
                    ]
                    if svc.autoscale
                    else []
                ),
            )
        )

    out = _consolidate(out)
    out.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], f.rule, f.location))
    return out


GROUP_MIN = 4

_GROUPS: dict[str, tuple[str, str, str | None]] = {
    "timeout-inversion": (
        "{n} callers give up before the service they call can finish",
        "Each of these callers stops waiting while its dependency can still be working, often retrying its own "
        "dependencies. Everything done after a caller has given up is wasted, and that wasted work is what keeps "
        "a slow dependency busy.",
        "Propagate deadlines so each hop only spends the time its caller is still willing to wait.",
    ),
    "queue-outlives-timeout": (
        "{n} services queue requests for longer than their callers wait",
        "A full queue at each of these services holds more work than its callers will wait for, so requests at "
        "the back are already dead when they start and serving them wastes capacity.",
        "Drop requests that waited longer than their callers' timeout and serve the newest first under pressure.",
    ),
    "no-jitter": (
        "Retries on {n} edges are synchronised",
        "Without jitter, requests that failed together retry together, so a brief failure comes back as waves of "
        "load at fixed intervals.",
        "Use full jitter on every one of them: sleep a random time between zero and the backoff.",
    ),
    "unbudgeted-retries": (
        "Retries on {n} edges have no budget",
        "During an incident each of these edges multiplies the load on a dependency that is already struggling.",
        "Add a 10% retry budget to each of them.",
    ),
    "low-headroom": (
        "{n} services run with little headroom",
        "Each of these services is busy more than 70% of the time at today's load, so a small slowdown or spike "
        "pushes it into queueing.",
        "Add instances so each runs at about 60% utilisation.",
    ),
    "cold-cache-collapse": (
        "{n} caches would overload their backing store if flushed",
        "If any of these caches is flushed or restarts, its misses exceed what the store behind it can serve, so "
        "it cannot refill the cache it depends on.",
        "Budget retries on each miss path and drop stale work at the stores so the caches can re-warm.",
    ),
    "autoscale-lag": (
        "Autoscaling cannot rescue {n} services from a spike",
        "New instances arrive long after clients have given up, so autoscaling covers sustained growth but not "
        "bursts; bursts need headroom or load shedding.",
        None,
    ),
    "no-breaker": (
        "{n} callers keep calling a shared dependency while it is failing",
        "These retried calls into dependencies shared by several callers have no circuit breaker, so they keep "
        "sending (and retrying) requests into an outage instead of failing fast.",
        "Open a breaker at 50% failures over 10 s on each of them and probe again after 5 s.",
    ),
    "congestion-collapse": (
        "{n} services get slower the busier they are",
        "Past their knee these services take longer per request, so throughput falls exactly when load is "
        "highest, and a burst can leave them stuck in the slow state.",
        "Add instances so normal load stays well below each service's knee.",
    ),
}


def _consolidate(findings: list[Finding]) -> list[Finding]:
    by_rule: dict[str, list[Finding]] = {}
    for f in findings:
        by_rule.setdefault(f.rule, []).append(f)
    out: list[Finding] = []
    for rule, group in by_rule.items():
        if len(group) < GROUP_MIN or rule not in _GROUPS:
            out.extend(group)
            continue
        title, common, fix = _GROUPS[rule]
        group.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], f.location))
        places = [f.location for f in group]
        more = f" and {len(places) - 3} more" if len(places) > 3 else ""
        out.append(
            Finding(
                rule=rule,
                severity=group[0].severity,
                title=title.format(n=len(group)),
                detail=f"{common} Worst first: {', '.join(places[:3])}{more}.",
                location=f"{len(group)} places",
                locations=places,
                evidence={
                    "count": len(group),
                    "items": [{"location": f.location, "severity": f.severity, **f.evidence} for f in group[:50]],
                },
                fix=fix or group[0].fix,
                patch=merge_patches(group),
            )
        )
    return out


def score(findings: list[Finding]) -> dict[str, Any]:
    penalty = 0.0
    for f in findings:
        spread = min(max(len(f.locations), 1) - 1, 10)
        penalty += _SEVERITY_WEIGHT[f.severity] * (1 + 0.1 * spread)
    s = max(0, round(100 - penalty))
    grade = "A" if s >= 90 else "B" if s >= 75 else "C" if s >= 60 else "D" if s >= 40 else "F"
    return {"score": s, "grade": grade}


def merge_patches(findings: list[Finding]) -> list[dict[str, Any]]:
    by_path: dict[tuple[Any, ...], dict[str, Any]] = {}
    for f in findings:
        for op in f.patch:
            by_path[tuple(op["path"])] = op
    return list(by_path.values())
