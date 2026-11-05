import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, ArrowRight, Share2, Sparkles, Square } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router";
import { useSession } from "../app/session";
import { toast, toastError, useRegisterCommands } from "../app/stores";
import { MetastabilityMap, type MapCell } from "../components/MetastabilityMap";
import { InfoTip, SeverityDot, Skeleton, StatusDot } from "../components/ui";
import { api, ApiError, newIdempotencyKey } from "../lib/api";
import { aggregate, cellState, STATE_LABEL } from "../lib/cells";
import { axisValue, fmtCount, fmtMult, fmtPct, fmtSeconds, triggerPhrase } from "../lib/format";
import type { Analysis, AnalysisResult, AnalysisSnapshot, InferredState, SimSummary, SweepParams } from "../lib/types";
import { realtime } from "../lib/ws";
import { Terrain } from "../three/Terrain";
import { useScene } from "../three/useScene";

type Seeds = Map<number, SimSummary>;

function buildCells(params: SweepParams, seeds: Map<number, Seeds>, planned: Set<number>, result: AnalysisResult | null): MapCell[] {
  const n = params.loads.length * params.values.length;
  return Array.from({ length: n }, (_, i) => {
    const s = seeds.get(i);
    const complete = s && s.size >= params.seeds;
    return {
      agg: complete ? aggregate([...s.values()]) : undefined,
      pending: !complete && (planned.has(i) || (s?.size ?? 0) > 0),
      inferred: inferredState(result?.verdicts?.[i]),
    };
  });
}

function inferredState(v: AnalysisResult["verdicts"][number] | undefined): InferredState | undefined {
  if (!v?.inferred) return undefined;
  return v.state ?? (v.bad ? "metastable" : "ok");
}

function useAnalysisStream(id: string, enabled: boolean) {
  const qc = useQueryClient();
  const snap = useQuery({
    queryKey: ["analysis", id],
    queryFn: () => api.get<AnalysisSnapshot>(`/api/v1/analyses/${id}`),
    enabled,
  });
  const [seeds, setSeeds] = useState<Map<number, Seeds>>(new Map());
  const [planned, setPlanned] = useState<Set<number>>(new Set());
  const seeded = useRef<string | null>(null);

  useEffect(() => {
    if (!snap.data || seeded.current === id) return;
    seeded.current = id;
    const m = new Map<number, Seeds>();
    for (const c of snap.data.cells) {
      const sm: Seeds = new Map();
      for (const s of c.summaries ?? []) sm.set(s.seed, s);
      m.set(c.cell, sm);
    }
    setSeeds(m);
    setPlanned(new Set(snap.data.cells.map((c) => c.cell)));
  }, [snap.data, id]);

  const stream = snap.data?.stream;
  const status = snap.data?.analysis.status;
  useEffect(() => {
    if (!stream || status === "done" || status === "failed" || status === "cancelled") return;
    return realtime.subscribe(stream.topic, stream.token, stream.last_id, (msg) => {
      const ev = msg.event as Record<string, unknown> & { type: string };
      if (ev.type === "task") {
        const cell = ev.cell as number;
        const seed = ev.seed as number;
        setSeeds((prev) => {
          const next = new Map(prev);
          const sm = new Map(next.get(cell) ?? []);
          sm.set(seed, ev.summary as SimSummary);
          next.set(cell, sm);
          return next;
        });
      } else if (ev.type === "planned") {
        setPlanned((prev) => new Set([...prev, ...(ev.cells as number[])]));
      } else if (ev.type === "status") {
        qc.setQueryData<AnalysisSnapshot>(["analysis", id], (old) =>
          old
            ? {
                ...old,
                analysis: {
                  ...old.analysis,
                  status: ev.status as Analysis["status"],
                  result: (ev.result as AnalysisResult | undefined) ?? old.analysis.result,
                },
              }
            : old,
        );
        if (ev.status === "done") void qc.invalidateQueries({ queryKey: ["analyses"] });
      }
    });
  }, [stream, status, id, qc]);

  return { snap, seeds, planned };
}

function TerrainView({ params, cells }: { params: SweepParams; cells: MapCell[] }) {
  const host = useRef<HTMLDivElement>(null);
  const todayCol = useMemo(() => {
    const i = params.loads.findIndex((l) => l >= 1);
    return i >= 0 ? i : null;
  }, [params.loads]);
  const scene = useScene(host, (el) => new Terrain(el, params.values.length, params.loads.length, todayCol), [params.values.length, params.loads.length]);
  useEffect(() => {
    scene.current?.update(cells.map((c) => ({ agg: c.agg, pending: c.pending, inferred: c.inferred })));
  }, [cells, scene]);
  return (
    <div className="terrain-host" ref={host} role="img" aria-label="3D terrain of the metastability map. Plateaus are combinations that never recover.">
      <p className="hint" style={{ position: "absolute", left: 8, bottom: 6 }}>
        Drag to orbit
      </p>
    </div>
  );
}

export default function AnalysisPage() {
  const { id = "" } = useParams();
  const { data: user, isLoading: loadingUser } = useSession();
  const navigate = useNavigate();
  const { snap, seeds, planned } = useAnalysisStream(id, !!user);
  const [view, setView] = useState<"map" | "terrain">("map");
  const [selected, setSelected] = useState<number | null>(null);

  const a = snap.data?.analysis;
  const params = snap.data?.params;
  const result = a?.status === "done" ? a.result : null;
  const cells = useMemo(() => (params ? buildCells(params, seeds, planned, result) : []), [params, seeds, planned, result]);
  const simsDone = useMemo(() => [...seeds.values()].reduce((n, s) => n + s.size, 0), [seeds]);

  const parent = useQuery({
    queryKey: ["analysis", a?.parent_id],
    queryFn: () => api.get<AnalysisSnapshot>(`/api/v1/analyses/${a!.parent_id}`),
    enabled: !!a?.parent_id,
  });

  const experiment = useMutation({
    mutationFn: () => api.post<{ analysis: Analysis }>(`/api/v1/analyses/${id}/experiments`, {}, newIdempotencyKey()),
    onSuccess: (r) => {
      toast("Applied every recommended fix as a new version. Re-running the same sweep.");
      navigate(`/analyses/${r.analysis.id}`);
    },
    onError: (e) => toastError((e as ApiError).detail ?? (e as Error).message),
  });
  const share = useMutation({
    mutationFn: () => api.post<{ slug: string; url: string }>(`/api/v1/analyses/${id}/report`),
    onSuccess: async (r) => {
      const url = `${location.origin}/r/${r.slug}`;
      try {
        await navigator.clipboard.writeText(url);
        toast("Link to the shared report copied.");
      } catch {
        toast(`Shared report: ${url}`);
      }
    },
    onError: (e) => toastError((e as Error).message),
  });
  const cancel = useMutation({
    mutationFn: () => api.post(`/api/v1/analyses/${id}/cancel`),
    onError: (e) => toastError((e as Error).message),
  });
  const reproduce = useMutation({
    mutationFn: (load: number) =>
      api.post<{ run: { id: string } }>("/api/v1/runs", { system_id: a!.system_id, version: a!.system_version, load, speed: 4 }, newIdempotencyKey()),
    onSuccess: (r, _load) => {
      const row = selected !== null && params ? Math.floor(selected / params.loads.length) : 0;
      const fault = { kind: params!.trigger.kind, target: params!.trigger.target,
        ...(params!.axis2 === "magnitude" ? { magnitude: params!.values[row], duration_s: params!.trigger.duration_s ?? 20 } : { magnitude: 1, duration_s: params!.values[row] }) };
      navigate(`/runs/${r.run.id}?inject=${encodeURIComponent(JSON.stringify(fault))}`);
    },
    onError: (e) => toastError((e as ApiError).detail ?? (e as Error).message),
  });

  useRegisterCommands("analysis", [
    { id: "terrain", group: "This analysis", label: view === "map" ? "Show as 3D terrain" : "Show as map", run: () => setView(view === "map" ? "terrain" : "map") },
    ...(a?.status === "done" ? [{ id: "fix", group: "This analysis", label: "Apply all fixes and compare", run: () => experiment.mutate() },
      { id: "share", group: "This analysis", label: "Share a read-only report", run: () => share.mutate() }] : []),
  ]);

  if (!loadingUser && !user) return <Navigate to="/" replace />;
  if (snap.isError) {
    return (
      <div className="page">
        <div className="empty">
          <h1 className="display">Analysis not found</h1>
          <Link className="btn" to="/systems">Back to systems</Link>
        </div>
      </div>
    );
  }
  if (!a || !params) {
    return (
      <div className="page">
        <Skeleton h={34} w={360} />
        <div style={{ height: 20 }} />
        <Skeleton h={420} />
      </div>
    );
  }

  const running = a.status === "queued" || a.status === "running";
  const total = Math.max(a.sims_total, simsDone);
  const firstBad = cells.findIndex((c) => c.agg && (cellState(c.agg) === "metastable"));
  const headline = result?.findings?.[0];
  const sel = selected !== null ? cells[selected] : null;
  const selRow = selected !== null ? Math.floor(selected / params.loads.length) : 0;
  const selCol = selected !== null ? selected % params.loads.length : 0;
  const parentResult = parent.data?.analysis.result;

  return (
    <div className="page">
      <div className="crumbs">
        <Link to="/systems">Systems</Link>
        <span>/</span>
        <Link to={`/systems/${a.system_id}`}>{snap.data?.system?.name ?? "System"}</Link>
        <span>/</span>
        <span>Analysis</span>
      </div>
      <div className="page-head">
        <div className="title-row">
          <h1 className="display">After {triggerPhrase(params.trigger)}</h1>
          <InfoTip label="About this sweep">
            Version {a.system_version}, {params.values.length} trigger sizes by {params.loads.length} load levels, {params.seeds}{" "}
            {params.seeds === 1 ? "run" : "runs"} per cell{a.parent_id ? ", with every recommended fix applied" : ""}.
          </InfoTip>
        </div>
        <div className="spacer" />
        {running ? (
          <button className="btn" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
            <Square size={14} /> Stop
          </button>
        ) : (
          <>
            <button className="btn" onClick={() => share.mutate()} disabled={share.isPending || a.status !== "done"}>
              <Share2 size={15} /> Share
            </button>
            {!a.parent_id && a.status === "done" && (
              <button className="btn btn-primary" onClick={() => experiment.mutate()} disabled={experiment.isPending}>
                <Sparkles size={15} /> Fix and compare
              </button>
            )}
          </>
        )}
      </div>

      <div className="analysis-grid">
        <div className="map-wrap">
          <div className="map-toolbar">
            <div className="seg" role="group" aria-label="View">
              <button aria-pressed={view === "map"} onClick={() => setView("map")}>Map</button>
              <button aria-pressed={view === "terrain"} onClick={() => setView("terrain")}>Terrain</button>
            </div>
            <InfoTip label="How to read the map">
              Each cell is one load level (column) and one {params.trigger.kind === "crash" ? "outage length" : "fault size"} (row). The
              line marks the tipping boundary. Click a cell for details.
            </InfoTip>
          </div>
          {view === "map" ? (
            <MetastabilityMap params={params} cells={cells} selected={selected} onSelect={setSelected} />
          ) : (
            <TerrainView params={params} cells={cells} />
          )}
          <div className="legend" aria-hidden="true">
            <span><i className="sw stable" />Recovers</span>
            <span><i className="sw slow" />Recovers slowly</span>
            <span><i className="sw metastable" />Never recovers</span>
            <span><i className="sw overloaded" />Overloaded</span>
            <span><i className="sw inferred" />Inferred</span>
          </div>
        </div>

        <aside className="stack">
          <div className="panel panel-pad verdict">
            <div className="row" style={{ gap: 8 }}>
              <StatusDot status={a.status} />
              <span className="muted" style={{ fontSize: "var(--t-14)" }}>
                {running ? `${simsDone} of ${total || "…"} simulations` : a.status === "done" ? "Finished" : a.status}
              </span>
            </div>
            {running && (
              <div className="progress" aria-hidden="true">
                <div style={{ width: `${total ? Math.min(100, (simsDone / total) * 100) : 4}%` }} />
              </div>
            )}
            {headline ? (
              <div className="title-row">
                <h2 className="display">{headline.title}</h2>
                <InfoTip>{headline.detail}</InfoTip>
              </div>
            ) : firstBad >= 0 ? (
              <h2 className="display">
                Already found a tipping point at {fmtMult(params.loads[firstBad % params.loads.length] ?? 0)} load.
              </h2>
            ) : (
              <h2 className="display">{running ? "Mapping the safe region…" : "No result"}</h2>
            )}
            {result && (
              <dl className="kv">
                <dt>Safe</dt>
                <dd>{fmtPct(result.safe_fraction)}</dd>
                <dt>Simulations</dt>
                <dd>{result.savings.simulated_sims} of {result.savings.full_grid_sims}</dd>
                <dt>Skipped</dt>
                <dd>{fmtPct(result.savings.avoided_fraction)}</dd>
                <dt>From cache</dt>
                <dd>{a.sims_cached}</dd>
                {a.time_to_first_insight_s !== undefined && (
                  <>
                    <dt>First result after</dt>
                    <dd>{fmtSeconds(a.time_to_first_insight_s)}</dd>
                  </>
                )}
              </dl>
            )}
          </div>

          {result && result.findings.length > 1 && (
            <div className="panel panel-pad">
              <ul className="rows">
                {result.findings.slice(1).map((f) => (
                  <li key={f.rule} className="row-item">
                    <SeverityDot severity={f.severity} />
                    <span className="row-title">{f.title}</span>
                    <InfoTip>{f.detail}</InfoTip>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {sel && (
            <div className="panel panel-pad stack" style={{ gap: 10 }}>
              <div className="section-title" style={{ marginBottom: 0 }}>
                {fmtMult(params.loads[selCol] ?? 0)} load, {axisValue(params.trigger.kind, params.values[selRow] ?? 0)}
              </div>
              {sel.agg ? (
                <>
                  <p className="muted">{STATE_LABEL[cellState(sel.agg)]}.</p>
                  <dl className="kv">
                    <dt>Never recovered</dt>
                    <dd>{fmtPct(sel.agg.p_metastable)}</dd>
                    <dt>Median recovery</dt>
                    <dd>{sel.agg.recovery_s === null ? "never" : fmtSeconds(sel.agg.recovery_s)}</dd>
                    <dt>Peak amplification</dt>
                    <dd>{fmtMult(sel.agg.peak_amplification)}</dd>
                    <dt>Wasted work</dt>
                    <dd>{fmtPct(sel.agg.wasted_fraction)}</dd>
                    <dt>Requests lost</dt>
                    <dd>{fmtCount(sel.agg.lost_requests)}</dd>
                  </dl>
                </>
              ) : (
                <p className="muted">
                  {sel.pending
                    ? "Still simulating."
                    : sel.inferred === "overloaded"
                      ? "Inferred: overloaded before any fault at this load."
                      : sel.inferred
                        ? "Inferred from the loads on either side."
                        : "Not simulated."}
                </p>
              )}
              <button className="btn" onClick={() => reproduce.mutate(params.loads[selCol] ?? 1)} disabled={reproduce.isPending}>
                <Activity size={15} /> Reproduce it live
              </button>
            </div>
          )}
        </aside>
      </div>

      {a.parent_id && parent.data && (
        <section className="stack" style={{ marginTop: 28 }}>
          <div className="title-row">
            <h2 className="display" style={{ fontSize: "var(--t-25)" }}>
              Before and after
            </h2>
            <InfoTip>Both sweeps used the same loads, faults and random seeds, so the difference comes from the fixes alone.</InfoTip>
          </div>
          {result && parentResult && (
            <p className="delta num">
              <span>{fmtPct(parentResult.safe_fraction)}</span>
              <ArrowRight size={22} aria-label="to" />
              <b>{fmtPct(result.safe_fraction)}</b>
              <span className="muted">safe</span>
            </p>
          )}
          <div className="row" style={{ alignItems: "flex-start", gap: 20 }}>
            <div className="map-wrap" style={{ flex: "1 1 380px" }}>
              <MetastabilityMap
                compact
                title={`Before (version ${parent.data.analysis.system_version})`}
                params={parent.data.params}
                cells={buildCells(parent.data.params, new Map(parent.data.cells.map((c) => [c.cell, new Map((c.summaries ?? []).map((s) => [s.seed, s]))])), new Set(), parentResult ?? null)}
              />
            </div>
            <div className="map-wrap" style={{ flex: "1 1 380px" }}>
              <MetastabilityMap compact title={`After (version ${a.system_version})`} params={params} cells={cells} />
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
