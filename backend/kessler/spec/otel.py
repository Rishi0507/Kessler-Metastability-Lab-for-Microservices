from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

SERVER, CLIENT = 2, 3
_KINDS = {
    "SPAN_KIND_INTERNAL": 1,
    "SPAN_KIND_SERVER": 2,
    "SPAN_KIND_CLIENT": 3,
    "SPAN_KIND_PRODUCER": 4,
    "SPAN_KIND_CONSUMER": 5,
}
_PEER_KEYS = ("peer.service", "db.system", "server.address", "net.peer.name")


class TraceImportError(ValueError):
    pass


@dataclass
class _Span:
    service: str
    instance: str | None
    trace: str
    id: str
    parent: str
    kind: int
    start: int
    end: int
    error: bool
    attrs: dict[str, str]

    @property
    def ms(self) -> float:
        return max(self.end - self.start, 0) / 1e6


@dataclass
class _Edge:
    groups: int = 0
    attempts: list[int] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)


def _value(v: dict[str, Any]) -> str:
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return str(v[k])
    return ""


def _attrs(items: list[dict[str, Any]] | None) -> dict[str, str]:
    return {a["key"]: _value(a.get("value", {})) for a in items or [] if "key" in a}


def _kind(k: Any) -> int:
    return _KINDS.get(k, 0) if isinstance(k, str) else int(k or 0)


def _docs(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        pass
    docs = []
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip():
            try:
                docs.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise TraceImportError(f"line {i} is not valid JSON: {e.msg}") from None
    return docs


def _spans(text: str) -> list[_Span]:
    out = []
    for doc in _docs(text):
        if not isinstance(doc, dict):
            continue
        for rs in doc.get("resourceSpans", []):
            res = _attrs(rs.get("resource", {}).get("attributes"))
            svc = res.get("service.name")
            if not svc:
                continue
            inst = res.get("service.instance.id") or res.get("host.name")
            for ss in rs.get("scopeSpans", []) + rs.get("instrumentationLibrarySpans", []):
                for s in ss.get("spans", []):
                    status = s.get("status", {}).get("code", 0)
                    out.append(
                        _Span(
                            service=svc,
                            instance=inst,
                            trace=s.get("traceId", ""),
                            id=s.get("spanId", ""),
                            parent=s.get("parentSpanId", "") or "",
                            kind=_kind(s.get("kind")),
                            start=int(s.get("startTimeUnixNano", 0)),
                            end=int(s.get("endTimeUnixNano", 0)),
                            error=status in (2, "STATUS_CODE_ERROR"),
                            attrs=_attrs(s.get("attributes")),
                        )
                    )
    if not out:
        raise TraceImportError("no spans with a service.name found; export traces as OTLP JSON")
    return out


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")[:48] or "service"
    if not s[0].isalpha():
        s = ("svc-" + s)[:48]
    return "client-svc" if s == "client" else s


def _pct(xs: list[float], q: float) -> float:
    ys = sorted(xs)
    return ys[min(len(ys) - 1, max(0, math.ceil(q * len(ys)) - 1))]


def _dur(ms: float) -> str:
    if ms >= 1000:
        return f"{float(f'{ms / 1000:.2g}'):g}s"
    if ms >= 1:
        return f"{float(f'{ms:.2g}'):g}ms"
    return f"{max(round(ms, 3), 0.001):g}ms"


def _busy(intervals: list[tuple[int, int]]) -> int:
    total, end = 0, -1
    for a, b in sorted(intervals):
        if b <= end:
            continue
        total += b - max(a, end)
        end = b
    return total


def _reaches(graph: dict[str, set[str]], src: str, dst: str) -> bool:
    seen, stack = set(), [src]
    while stack:
        u = stack.pop()
        if u == dst:
            return True
        if u not in seen:
            seen.add(u)
            stack.extend(graph.get(u, ()))
    return False


def import_otlp(text: str, name: str = "Imported system") -> tuple[str, list[str]]:
    spans = _spans(text)
    by_id = {(s.trace, s.id): s for s in spans}
    children: dict[tuple[str, str], list[_Span]] = defaultdict(list)
    for s in spans:
        if s.parent:
            children[(s.trace, s.parent)].append(s)

    notes: list[str] = []
    servers: dict[str, list[_Span]] = defaultdict(list)
    for s in spans:
        if s.kind == SERVER or (not s.parent and s.kind != CLIENT):
            servers[_slug(s.service)].append(s)

    edges: dict[tuple[str, str], _Edge] = defaultdict(_Edge)
    leaf_samples: dict[str, list[float]] = defaultdict(list)
    groups: dict[tuple[str, str, str, str], list[_Span]] = defaultdict(list)
    for c in spans:
        if c.kind != CLIENT:
            continue
        src = _slug(c.service)
        kids = [k for k in children.get((c.trace, c.id), []) if k.kind == SERVER and k.service != c.service]
        if kids:
            dst = _slug(kids[0].service)
        else:
            peer = next((c.attrs[k] for k in _PEER_KEYS if c.attrs.get(k)), None)
            if not peer:
                continue
            dst = _slug(peer)
            leaf_samples[dst].append(c.ms)
        if dst != src:
            groups[(c.trace, c.parent, src, dst)].append(c)

    for (_, _, src, dst), cs in groups.items():
        e = edges[(src, dst)]
        e.groups += 1
        e.attempts.append(len(cs))
        e.durations.extend(c.ms for c in cs)

    names = list(dict.fromkeys([*servers, *leaf_samples, *(d for _, d in edges)]))
    if not names:
        raise TraceImportError("no server or client spans found")
    if len(names) > 64:
        raise TraceImportError(f"found {len(names)} services; Kessler models up to 64")

    t0 = min(s.start for s in spans)
    t1 = max(s.end for s in spans)
    window_s = max((t1 - t0) / 1e9, 1.0)
    roots: dict[str, int] = defaultdict(int)
    for svc, ss in servers.items():
        roots[svc] = sum(1 for s in ss if not s.parent or (s.trace, s.parent) not in by_id)
    entry = max(names, key=lambda n: (roots.get(n, 0), len(servers.get(n, []))))
    rps = max(roots.get(entry, 0), 1) / window_s

    graph: dict[str, set[str]] = defaultdict(set)
    kept: list[tuple[str, str, _Edge]] = []
    for (src, dst), e in sorted(edges.items(), key=lambda kv: -kv[1].groups):
        if _reaches(graph, dst, src):
            notes.append(f"dropped the call {src} -> {dst}: it would form a cycle")
            continue
        graph[src].add(dst)
        kept.append((src, dst, e))

    lines = [
        f"name: {json.dumps(name)}",
        "description: >",
        f"  Imported from {len(spans)} spans over {window_s:.0f} s. Traffic, latency and the call graph",
        "  come from the traces. Timeouts are guesses; instances and concurrency are estimates.",
        "traffic:",
        f"  rps: {float(f'{rps:.3g}'):g}",
        f"  entry: {entry}",
    ]
    entry_ms = [s.ms for s in servers.get(entry, [])] or leaf_samples.get(entry, [1.0])
    lines += ["  client:", f"    timeout: {_dur(max(4 * _pct(entry_ms, 0.99), 2000))}  # guess"]
    lines.append("services:")
    for n in names:
        ss = servers.get(n, [])
        if ss:
            own = []
            for s in ss:
                calls = [(k.start, k.end) for k in children.get((s.trace, s.id), []) if k.kind == CLIENT]
                own.append(max(s.ms - _busy(calls) / 1e6, s.ms * 0.01, 0.001))
            total = [s.ms for s in ss]
            count = len(ss)
            instances = len({s.instance for s in ss if s.instance}) or 1
        else:
            own = total = leaf_samples.get(n) or [1.0]
            count = len(own)
            instances = 1
        svc_rps = count / window_s
        conc = max(16, math.ceil(2 * svc_rps * (sum(total) / len(total)) / 1000 / instances))
        p50, p99 = _pct(own, 0.5), _pct(own, 0.99)
        lines += [
            f"  {n}:",
            f"    instances: {instances}",
            f"    concurrency: {conc}  # estimate",
            f"    latency: {{ p50: {_dur(p50)}, p99: {_dur(max(p99, p50))} }}",
        ]
        if ss:
            errs = sum(1 for s in ss if s.error and not any(k.error for k in children.get((s.trace, s.id), [])))
            if errs / count >= 0.005:
                lines.append(f"    error_rate: {errs / count:.3f}")
        out = [(dst, e) for src, dst, e in kept if src == n]
        if out:
            lines.append("    calls:")
        for dst, e in out:
            lines.append(f"      - to: {dst}")
            prob = e.groups / count if ss else 1.0
            if prob < 0.995:
                lines.append(f"        probability: {min(prob, 1):.2f}")
            lines.append(f"        timeout: {_dur(max(4 * _pct(e.durations, 0.99), 1000))}  # guess")
            most = max(e.attempts)
            if most > 1:
                retry = f"{{ attempts: {min(most, 10)}, base: 50ms, jitter: full }}"
                lines.append(f"        retry: {retry}  # attempts seen in traces")
    return "\n".join(lines) + "\n", notes
