import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, Navigate, useParams, useSearchParams } from "react-router";
import { useSession } from "../app/session";
import { toast, toastError, useRegisterCommands } from "../app/stores";
import { LiveCharts, type SeriesBuffer } from "../components/LiveCharts";
import { Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { fmtClock, fmtMult, triggerPhrase } from "../lib/format";
import type { Frame, RunSnapshot } from "../lib/types";
import { realtime } from "../lib/ws";
import { TopologyScene } from "../three/TopologyScene";
import { useScene } from "../three/useScene";

const WINDOW = 180;

export default function LiveRunPage() {
  const { id = "" } = useParams();
  const [search] = useSearchParams();
  const { data: user, isLoading: loadingUser } = useSession();
  const snap = useQuery({ queryKey: ["run", id], queryFn: () => api.get<RunSnapshot>(`/api/v1/runs/${id}`), enabled: !!user });
  const host = useRef<HTMLDivElement>(null);
  const [frame, setFrame] = useState<Frame | null>(null);
  const [state, setState] = useState<"waiting" | "running" | "ended">("waiting");
  const [selected, setSelected] = useState<string | null>(null);
  const [magnitude, setMagnitude] = useState(4);
  const [load, setLoad] = useState<number | null>(null);
  const [series, setSeries] = useState<SeriesBuffer>({ t: [], ratio: [], p99: [], amp: [] });
  const injected = useRef(false);
  const topo = snap.data?.topology ?? null;
  const topic = snap.data?.stream.topic ?? "";

  const scene = useScene(
    host,
    (el) => {
      const s = new TopologyScene(el, topo!);
      s.onSelect = setSelected;
      return s;
    },
    [topo ? JSON.stringify(topo.nodes.map((n) => n.id)) : null],
  );

  useEffect(() => {
    const stream = snap.data?.stream;
    if (!stream || snap.data?.run.status === "done") return;
    return realtime.subscribe(stream.topic, stream.token, stream.last_id, (msg) => {
      const ev = msg.event as Record<string, unknown> & { type: string };
      if (ev.type === "frames") {
        const frames = ev.frames as Frame[];
        const last = frames[frames.length - 1];
        if (!last) return;
        setState("running");
        setFrame(last);
        scene.current?.update(last);
        setSeries((prev) => {
          const next = { t: [...prev.t], ratio: [...prev.ratio], p99: [...prev.p99], amp: [...prev.amp] };
          for (const f of frames) {
            if (next.t.length && f.t <= next.t[next.t.length - 1]!) continue;
            next.t.push(f.t);
            next.ratio.push(f.client.offered > 0 ? Math.min(1, f.client.good / f.client.offered) : 1);
            next.p99.push(f.client.p99);
            next.amp.push(f.amp);
          }
          const drop = Math.max(0, next.t.length - WINDOW);
          return { t: next.t.slice(drop), ratio: next.ratio.slice(drop), p99: next.p99.slice(drop), amp: next.amp.slice(drop) };
        });
      } else if (ev.type === "started") {
        setState("running");
        if (ev.resumed) toast("The worker running this simulation changed. It resumed from the same point with identical results.");
      } else if (ev.type === "ended") {
        setState("ended");
      }
    });
  }, [snap.data, scene]);

  const send = useMutation({
    mutationFn: (cmd: Record<string, unknown>) => realtime.control(topic, cmd),
    onSuccess: (r) => {
      if (!r.ok) toastError(r.error ?? "The simulation rejected that command");
    },
  });

  const inject = useMemo(() => {
    const raw = search.get("inject");
    try {
      return raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
    } catch {
      return null;
    }
  }, [search]);
  useEffect(() => {
    if (inject && !injected.current && frame && frame.t >= 30) {
      injected.current = true;
      send.mutate({ op: "fault", fault: inject });
      toast(`Injected ${triggerPhrase(inject as { kind: string; target?: string; duration_s?: number }, inject.magnitude as number)}.`);
    }
  }, [inject, frame, send]);

  const target = selected ?? topo?.bottleneck ?? topo?.nodes.find((n) => n.kind !== "client")?.id ?? "";
  const fault = (kind: string, extra: Record<string, unknown> = {}) =>
    send.mutate({ op: "fault", fault: { kind, ...(kind === "spike" ? {} : { target }), duration_s: 20, ...extra } });
  const budgets = () => {
    for (const e of topo?.edges ?? []) {
      if (e.attempts > 1 && !e.budget) {
        send.mutate({ op: "policy", service: e.from, target: e.to, budget: { ratio: 0.1, min_per_s: 5 } });
      }
    }
    toast("Added a 10% retry budget to every retrying call.");
  };
  const svc = frame?.services.find((s) => s.name === target);
  const isCache = topo?.nodes.find((n) => n.id === target)?.kind === "cache";

  useRegisterCommands("live", [
    { id: "slow", group: "Break something", label: `Slow down ${target} ${magnitude}×`, run: () => fault("latency", { magnitude }) },
    { id: "errors", group: "Break something", label: `Error burst on ${target}`, run: () => fault("errors", { magnitude: 0.5 }) },
    { id: "crash", group: "Break something", label: `Take ${target} down for 15 s`, run: () => fault("crash", { duration_s: 15 }) },
    { id: "spike", group: "Break something", label: "Double the traffic for 20 s", run: () => fault("spike", { magnitude: 2 }) },
    { id: "clear", group: "Fix something", label: "Clear all faults", run: () => send.mutate({ op: "clear_faults" }) },
    { id: "budget", group: "Fix something", label: "Add retry budgets everywhere", run: budgets },
    { id: "deadline", group: "Fix something", label: "Turn on deadline propagation", run: () => send.mutate({ op: "resilience", deadline_propagation: true }) },
  ]);

  if (!loadingUser && !user) return <Navigate to="/" replace />;
  if (!snap.data || !topo) {
    return (
      <div className="page">
        <Skeleton h={480} />
      </div>
    );
  }
  const run = snap.data.run;

  return (
    <div className="live">
      <div className="live-canvas" ref={host} role="img" aria-label="Live 3D view of the system: nodes are services, moving dots are requests. Red dots are errors, amber dots are retries." />
      <div className="hud">
        <div className="crumbs" style={{ marginBottom: 0 }}>
          <Link to={`/systems/${run.system_id}`}>Back to the system</Link>
        </div>
        <div className="clock display" aria-live="off">
          {frame ? fmtClock(frame.t) : "00:00"}
        </div>
        <span className="muted" style={{ fontSize: "var(--t-14)" }}>
          {state === "waiting" ? "Waiting for a simulation worker…" : state === "ended" ? "Run finished" : `Live at ${run.params.speed}× speed, ${fmtMult(load ?? run.params.load)} load`}
        </span>
        {frame && frame.faults.length > 0 && (
          <div className="chips">
            {frame.faults.map((f, i) => (
              <span className="chip" key={i}>
                {triggerPhrase({ kind: f.kind, target: f.target }, f.magnitude)}
              </span>
            ))}
          </div>
        )}
      </div>

      <aside className="dock" aria-label="Controls">
        <div className="stack" style={{ gap: 6 }}>
          <h3>Selected: {target}</h3>
          {svc ? (
            <p className="muted num" style={{ fontSize: "var(--t-14)" }}>
              {Math.round(svc.util * 100)}% busy, {Math.round(svc.queue)} queued, {svc.instances} instance{svc.instances === 1 ? "" : "s"}
              {svc.hit_ratio !== undefined ? `, ${Math.round(svc.hit_ratio * 100)}% cache hits` : ""}
            </p>
          ) : (
            <p className="faint" style={{ fontSize: "var(--t-14)" }}>Click a service in the view to target it.</p>
          )}
        </div>
        <div className="stack" style={{ gap: 8 }}>
          <h3>Break something</h3>
          <div className="field">
            <label htmlFor="mag">Slowdown size: {magnitude}×</label>
            <input id="mag" type="range" min={2} max={10} value={magnitude} onChange={(e) => setMagnitude(Number(e.target.value))} />
          </div>
          <div className="fault-grid">
            <button className="btn btn-sm" onClick={() => fault("latency", { magnitude })}>Slow down</button>
            <button className="btn btn-sm" onClick={() => fault("errors", { magnitude: 0.5 })}>Error burst</button>
            <button className="btn btn-sm" onClick={() => fault("crash", { duration_s: 15 })}>Outage</button>
            {isCache ? (
              <button className="btn btn-sm" onClick={() => fault("cache_flush")}>Flush cache</button>
            ) : (
              <button className="btn btn-sm" onClick={() => fault("spike", { magnitude: 2 })}>Traffic spike</button>
            )}
          </div>
          <span className="hint">Each lasts 20 s. Watch whether the system recovers after it ends.</span>
        </div>
        <div className="stack" style={{ gap: 8 }}>
          <h3>Fix something</h3>
          <div className="fault-grid">
            <button className="btn btn-sm" onClick={budgets}>Retry budgets</button>
            <button className="btn btn-sm" onClick={() => send.mutate({ op: "resilience", deadline_propagation: true })}>Deadlines</button>
            <button className="btn btn-sm" onClick={() => send.mutate({ op: "queue", service: target, discipline: "adaptive_lifo", timeout_ms: 500 })}>Shed old work</button>
            <button className="btn btn-sm" onClick={() => send.mutate({ op: "scale", service: target, instances: (svc?.instances ?? 1) + 1 })}>Add instance</button>
          </div>
          <button className="btn btn-sm btn-ghost" onClick={() => send.mutate({ op: "clear_faults" })}>Clear all faults</button>
        </div>
        <div className="field">
          <label htmlFor="load">Traffic: {fmtMult(load ?? run.params.load)} today's</label>
          <input
            id="load"
            type="range"
            min={0.2}
            max={3}
            step={0.1}
            value={load ?? run.params.load}
            onChange={(e) => setLoad(Number(e.target.value))}
            onPointerUp={() => load !== null && send.mutate({ op: "load", multiplier: load })}
            onKeyUp={() => load !== null && send.mutate({ op: "load", multiplier: load })}
          />
        </div>
        <button className="btn btn-sm btn-danger" onClick={() => send.mutate({ op: "stop" })} disabled={state === "ended"}>
          Stop the run
        </button>
      </aside>

      <LiveCharts data={series} />
    </div>
  );
}
