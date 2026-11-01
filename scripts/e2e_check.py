from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=30.0)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"X-CSRF-Token": self.http.cookies.get("ks_csrf") or ""}
        return h | (extra or {})

    def get(self, path: str) -> Any:
        r = self.http.get(path)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Any = None, key: str | None = None) -> tuple[int, Any]:
        r = self.http.post(path, json=body, headers=self._headers({"Idempotency-Key": key} if key else None))
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:300]}")
        return r.status_code, (r.json() if r.content else None)

    async def stream(self, topic: str, token: str, since: str, until, timeout_s: float) -> list[dict[str, Any]]:
        _, t = self.post("/api/v1/realtime/tickets")
        uri = self.base.replace("http", "ws", 1) + f"/ws?ticket={t['ticket']}"
        events: list[dict[str, Any]] = []
        async with websockets.connect(uri, max_size=8 * 2**20) as ws:
            await ws.send(json.dumps({"op": "sub", "topic": topic, "token": token, "since": since}))
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline and not until(events):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
                except TimeoutError:
                    break
                msg = json.loads(raw)
                if "event" in msg:
                    msg["_t"] = time.monotonic()
                    events.append(msg)
                elif msg.get("type") == "error":
                    raise RuntimeError(f"stream error: {msg}")
        return events


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


class E2E:
    def __init__(self, base: str, template: str) -> None:
        self.c = Client(base)
        self.template_id = template
        self.results: dict[str, Any] = {}

    def report(self, name: str, ok: bool, summary: str, **data: Any) -> None:
        self.results[name] = {"pass": ok, "summary": summary, **data}
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:12} {summary}", flush=True)

    def setup(self) -> None:
        self.c.post("/api/v1/auth/guest")
        templates = self.c.get("/api/v1/templates")["templates"]
        self.tpl = next(t for t in templates if t["id"] == self.template_id)
        t0 = time.perf_counter()
        _, sys_ = self.c.post("/api/v1/systems", {"yaml": self.tpl["yaml"], "template_id": self.tpl["id"]})
        create_ms = (time.perf_counter() - t0) * 1000
        self.system_id = sys_["id"]
        detail = self.c.get(f"/api/v1/systems/{self.system_id}")
        checks = detail.get("checks") or {}
        nodes = len((checks.get("topology") or {}).get("nodes", [])) - 1
        lat = []
        for _ in range(8):
            t0 = time.perf_counter()
            self.c.post("/api/v1/specs/check", {"yaml": self.tpl["yaml"]})
            lat.append((time.perf_counter() - t0) * 1000)
        self.report(
            "spec",
            nodes > 0 and bool(checks.get("findings") is not None),
            f"{nodes} services; created in {create_ms:.0f} ms; {len(checks.get('findings', []))} findings, "
            f"grade {checks.get('score', {}).get('grade')}; /specs/check p50 {statistics.median(lat):.0f} ms, "
            f"max {max(lat):.0f} ms on {len(self.tpl['yaml']) // 1024} KB of YAML",
            services=nodes,
            create_ms=create_ms,
            check_p50_ms=statistics.median(lat),
            check_max_ms=max(lat),
        )

    def start_sweep(self, trigger: dict[str, Any], seeds: int, key: str | None = None) -> str:
        key = key or uuid.uuid4().hex
        _, r = self.c.post("/api/v1/analyses", {"system_id": self.system_id, "trigger": trigger, "seeds": seeds}, key)
        return r["analysis"]["id"]

    def idempotency(self, trigger: dict[str, Any]) -> str:
        key = uuid.uuid4().hex
        a = self.start_sweep(trigger, 2, key)
        b = self.start_sweep(trigger, 2, key)
        self.report(
            "idempotency",
            a == b,
            f"retried POST with the same key returned {'the same' if a == b else 'a different'} analysis",
        )
        return a

    async def follow(self, analysis_id: str, timeout_s: float = 900, sample_api: bool = False) -> dict[str, Any]:
        snap = self.c.get(f"/api/v1/analyses/{analysis_id}")
        st = snap["stream"]
        t0 = time.monotonic()
        api_lat: list[float] = []
        stop = asyncio.Event()

        async def sampler() -> None:
            async with httpx.AsyncClient(base_url=self.c.base, cookies=self.c.http.cookies, timeout=30) as ac:
                while not stop.is_set():
                    s = time.perf_counter()
                    r = await ac.get(f"/api/v1/systems/{self.system_id}")
                    if r.status_code == 200:
                        api_lat.append((time.perf_counter() - s) * 1000)
                    await asyncio.sleep(0.25)

        def done(evs: list[dict[str, Any]]) -> bool:
            return any(
                e["event"].get("type") == "status" and e["event"].get("status") in ("done", "failed", "cancelled")
                for e in evs
            )

        task = asyncio.create_task(sampler()) if sample_api else None
        already_done = snap["analysis"]["status"] in ("done", "failed", "cancelled")
        events = [] if already_done else await self.c.stream(st["topic"], st["token"], st["last_id"], done, timeout_s)
        stop.set()
        if task:
            await task
        final = self.c.get(f"/api/v1/analyses/{analysis_id}")
        tasks = [e for e in events if e["event"].get("type") == "task"]
        first = min((e["_t"] for e in tasks), default=None)
        return {
            "snapshot": snap,
            "final": final,
            "events": events,
            "tasks": tasks,
            "first_s": (first - t0) if first else None,
            "total_s": time.monotonic() - t0,
            "api_ms": api_lat,
        }

    def check_sweep(self, f: dict[str, Any]) -> None:
        a = f["final"]["analysis"]
        seen = {(e["event"]["cell"], e["event"]["seed"]) for e in f["tasks"]}
        dupes = len(f["tasks"]) - len(seen)
        streamed_or_snap = len(
            seen | {(c["cell"], s["seed"]) for c in f["snapshot"]["cells"] for s in c.get("summaries", [])}
        )
        ok = a["status"] == "done" and streamed_or_snap == a["sims_done"] and a["result"] is not None
        sav = (a["result"] or {}).get("savings", {})
        self.report(
            "sweep",
            ok,
            f"{a['status']}: {a['sims_done']} simulations ({sav.get('avoided_fraction', 0):.0%} skipped by adaptive "
            f"search) in {f['total_s']:.0f} s; first result after {f['first_s'] or 0:.1f} s; "
            f"{len(f['tasks'])} task events, {dupes} duplicates",
            sims=a["sims_done"],
            total_s=f["total_s"],
            first_s=f["first_s"],
            duplicates=dupes,
            headline=(a["result"] or {}).get("findings", [{}])[0].get("title"),
        )
        lat = f["api_ms"]
        if lat:
            self.report(
                "api-latency",
                pct(lat, 0.99) < 1000,
                f"GET /systems/:id while the sweep ran: p50 {statistics.median(lat):.0f} ms, "
                f"p95 {pct(lat, 0.95):.0f} ms, p99 {pct(lat, 0.99):.0f} ms over {len(lat)} requests",
                p50_ms=statistics.median(lat),
                p95_ms=pct(lat, 0.95),
                p99_ms=pct(lat, 0.99),
                n=len(lat),
            )

    def check_integrity(self, f: dict[str, Any], samples: int = 4) -> None:
        from kessler.analysis.planner import SweepParams
        from kessler.engine import get_engine
        from kessler.spec import build_engine_spec, compile_text

        system = compile_text(self.tpl["yaml"]).system
        params = SweepParams.from_dict(f["final"]["params"])
        stored = [
            (c["cell"], s) for c in f["final"]["cells"] for s in c.get("summaries", []) if s.get("verdict") != "failed"
        ]
        picks = random.Random(7).sample(stored, min(samples, len(stored)))
        engine = get_engine()
        mismatches = []
        for cell, s in picks:
            local = engine.run(build_engine_spec(system, params.scenario(cell, s["seed"]) | {"max_events": 80_000_000}))
            for k in ("verdict", "recovery_s", "peak_amplification", "lost_requests", "min_goodput_ratio"):
                lv, sv = local.get(k), s.get(k)
                if isinstance(lv, float) and isinstance(sv, (int, float)):
                    same = abs(lv - sv) <= 1e-6 * max(1.0, abs(lv))
                else:
                    same = lv == sv
                if not same:
                    mismatches.append({"cell": cell, "seed": s["seed"], "field": k, "local": lv, "stored": sv})
        self.report(
            "integrity",
            not mismatches,
            f"{len(picks)} stored simulations recomputed locally: "
            f"{'identical' if not mismatches else f'{len(mismatches)} fields differ'}",
            mismatches=mismatches,
        )

    async def cache(self, trigger: dict[str, Any], seeds: int) -> None:
        aid = self.start_sweep(trigger, seeds)
        f = await self.follow(aid, timeout_s=300)
        a = f["final"]["analysis"]
        ok = a["status"] == "done" and a["sims_cached"] == a["sims_done"] > 0
        self.report(
            "cache",
            ok,
            f"re-run: {a['sims_cached']}/{a['sims_done']} simulations from the cache in {f['total_s']:.1f} s",
            cached=a["sims_cached"],
            sims=a["sims_done"],
            total_s=f["total_s"],
        )

    async def live(self, fault: dict[str, Any]) -> None:
        _, r = self.c.post("/api/v1/runs", {"system_id": self.system_id, "load": 1, "speed": 4}, uuid.uuid4().hex)
        rid = r["run"]["id"]
        snap = self.c.get(f"/api/v1/runs/{rid}")
        st = snap["stream"]
        _, t = self.c.post("/api/v1/realtime/tickets")
        uri = self.c.base.replace("http", "ws", 1) + f"/ws?ticket={t['ticket']}"
        frames, messages, sizes, acked = 0, 0, [], None
        async with websockets.connect(uri, max_size=8 * 2**20) as ws:
            await ws.send(json.dumps({"op": "sub", "topic": st["topic"], "token": st["token"], "since": st["last_id"]}))
            t0 = time.monotonic()
            sent = False
            while time.monotonic() - t0 < 25:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except TimeoutError:
                    continue
                msg = json.loads(raw)
                ev = msg.get("event") or {}
                if ev.get("type") == "frames":
                    messages += 1
                    frames += len(ev.get("frames") or [])
                    sizes.append(len(raw))
                if ev.get("type") == "ack" and ev.get("req") == "e2e-fault":
                    acked = bool(ev.get("ok"))
                    break
                if messages >= 8 and not sent:
                    await ws.send(
                        json.dumps(
                            {
                                "op": "ctl",
                                "topic": st["topic"],
                                "req": "e2e-fault",
                                "cmd": {"op": "fault", "fault": fault},
                            }
                        )
                    )
                    sent = True
            elapsed = time.monotonic() - t0
        try:
            self.c.post(f"/api/v1/runs/{rid}/commands", {"command": {"op": "stop"}})
        except RuntimeError:
            pass
        ok = messages >= 8 and acked is True
        self.report(
            "live",
            ok,
            f"{messages} messages ({frames} frames) in {elapsed:.0f} s, {messages / max(elapsed, 1e-9):.1f} "
            f"messages/s, median message {statistics.median(sizes) / 1024 if sizes else 0:.1f} KB; fault command "
            f"{'acknowledged' if acked else 'not acknowledged'}",
            messages=messages,
            frames=frames,
            median_message_bytes=statistics.median(sizes) if sizes else 0,
            acked=acked,
        )

    async def failover(self, trigger: dict[str, Any]) -> None:
        aid = self.start_sweep(trigger, 2)
        snap = self.c.get(f"/api/v1/analyses/{aid}")
        st = snap["stream"]
        crashed: dict[str, str] = {}
        timeline: list[tuple[float, str]] = []
        t0 = time.monotonic()

        def devstack(*cmd: str) -> str:
            r = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "devstack.py"), *cmd],
                capture_output=True,
                text=True,
                check=False,
            )
            return (r.stdout + r.stderr).strip()

        def leader() -> str | None:
            for i, port in ((1, 9201), (2, 9202)):
                try:
                    text = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=2).text
                except httpx.HTTPError:
                    continue
                if re.search(r"^kessler_scheduler_leader 1\.0$", text, re.M):
                    return f"scheduler-{i}"
            return None

        def until(evs: list[dict[str, Any]]) -> bool:
            tasks = sum(1 for e in evs if e["event"].get("type") == "task")
            if "worker" not in crashed and tasks >= 6:
                crashed["worker"] = "worker-1"
                devstack("kill", "worker-1")
                timeline.append((time.monotonic() - t0, "killed worker-1"))
            if "worker" in crashed and "leader" not in crashed and tasks >= 14:
                name = leader()
                if name:
                    crashed["leader"] = name
                    devstack("kill", name)
                    timeline.append((time.monotonic() - t0, f"killed {name} (the leader)"))
            return any(
                e["event"].get("type") == "status" and e["event"].get("status") in ("done", "failed") for e in evs
            )

        events = await self.c.stream(st["topic"], st["token"], st["last_id"], until, 1200)
        for name in crashed.values():
            devstack("restart", name)
        new_leader = leader()
        final = self.c.get(f"/api/v1/analyses/{aid}")
        a = final["analysis"]
        times = [e["_t"] - t0 for e in events if e["event"].get("type") == "task"]
        keys_seen = [(e["event"]["cell"], e["event"]["seed"]) for e in events if e["event"].get("type") == "task"]
        gaps = [b - a_ for a_, b in zip(times, times[1:], strict=False)]
        stored = {(c["cell"], x["seed"]) for c in final["cells"] for x in c.get("summaries", [])}
        ok = (
            a["status"] == "done"
            and len(crashed) == 2
            and len(keys_seen) == len(set(keys_seen))
            and len(stored) == a["sims_done"]
            and a["sims_cached"] == 0
        )
        self.report(
            "failover",
            ok,
            f"{' then '.join(f'{what} at {t:.0f} s' for t, what in timeline)}; sweep {a['status']} at "
            f"{(times[-1] if times else 0):.0f} s with {a['sims_done']} simulations "
            f"({a['sims_cached']} from cache), "
            f"{len(keys_seen) - len(set(keys_seen))} duplicates, longest pause {max(gaps, default=0):.0f} s; "
            f"leader afterwards: {new_leader}",
            timeline=timeline,
            sims=a["sims_done"],
            longest_pause_s=max(gaps, default=0),
            new_leader=new_leader,
        )
        self.check_integrity({"final": final}, samples=4)


async def amain(args: argparse.Namespace) -> int:
    e = E2E(args.base, args.template)
    t_start = time.perf_counter()
    e.setup()
    trigger = dict(e.tpl["trigger"])
    if args.failover:
        fresh = random.SystemRandom().randint(21, 59)
        await e.failover({"kind": "errors", "target": e.tpl["trigger"].get("target"), "duration_s": fresh})
    else:
        aid = e.idempotency(trigger)
        f = await e.follow(aid, sample_api=True)
        e.check_sweep(f)
        e.check_integrity(f)
        await e.cache(trigger, 2)
        await e.live({"kind": trigger["kind"], "target": trigger.get("target"), "magnitude": 4, "duration_s": 20})
    passed = sum(1 for r in e.results.values() if r["pass"])
    print(f"{passed}/{len(e.results)} checks passed in {time.perf_counter() - t_start:.0f} s")
    if args.report:
        args.report.write_text(json.dumps(e.results, indent=2, default=str), encoding="utf-8")
    return 0 if passed == len(e.results) else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--template", default="marketplace-at-scale")
    ap.add_argument("--failover", action="store_true", help="crash a worker and the scheduler leader mid-sweep")
    ap.add_argument("--report", type=Path)
    sys.exit(asyncio.run(amain(ap.parse_args())))


if __name__ == "__main__":
    main()
