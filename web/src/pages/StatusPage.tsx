import { useQuery } from "@tanstack/react-query";
import { Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { fmtCount, fmtPct, fmtSeconds } from "../lib/format";

interface Kpis {
  generated_at: number;
  slo: {
    time_to_first_insight_p50_s: number | null;
    time_to_first_insight_p95_s: number | null;
    analysis_completion_p50_s: number | null;
    analysis_completion_p95_s: number | null;
  };
  throughput: {
    analyses_24h: number;
    analyses_done_24h: number;
    analyses_active: number;
    simulations_24h: number;
    cache_hit_ratio: number | null;
    adaptive_sims_avoided: number | null;
    engine_events_per_s: number;
  };
  realtime: { live_runs: number; ws_connections: number; fanout_p99_ms: number | null };
  capacity: {
    workers: number;
    worker_slots: number;
    worker_busy: number;
    gateways: number;
    api_replicas: number;
    scheduler_leader: string | null;
    backlog: number;
    inflight: number;
    results_lag: number;
    tenants_waiting?: number;
  };
  product: { active_users_24h: number; fix_adoption_7d: number | null; stable_region_gain_avg: number | null };
  instances: Record<string, Record<string, string>[]>;
}

function Slo({ name, target, value, ok, unit }: { name: string; target: string; value: string; ok: boolean | null; unit?: string }) {
  return (
    <tr>
      <td>{name}</td>
      <td className="muted">{target}</td>
      <td className="n">
        {value}
        {unit}
      </td>
      <td>{ok === null ? <span className="faint">no data yet</span> : ok ? <span className="ok-text">Meeting</span> : <span className="bad-text">Missing</span>}</td>
    </tr>
  );
}

export default function StatusPage() {
  const q = useQuery({
    queryKey: ["kpis"],
    queryFn: () => api.get<Kpis>("/api/v1/platform/kpis"),
    refetchInterval: 5000,
    staleTime: 2000,
  });
  const k = q.data;
  const ttfi = k?.slo.time_to_first_insight_p95_s ?? null;
  const done95 = k?.slo.analysis_completion_p95_s ?? null;
  const fan = k?.realtime.fanout_p99_ms ?? null;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">Platform status</h1>
          <p className="sub">
            Live numbers from every Kessler service, refreshed every 5 seconds. The same figures are exported to Prometheus.
          </p>
        </div>
      </div>
      {!k ? (
        <Skeleton h={360} />
      ) : (
        <div className="stack" style={{ gap: 28 }}>
          <section className="panel">
            <table className="slo-table">
              <thead>
                <tr>
                  <th>Service level</th>
                  <th>Target</th>
                  <th style={{ textAlign: "right" }}>Last 24 hours</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                <Slo name="First result after submitting an analysis (95th percentile)" target="under 3 s" value={fmtSeconds(ttfi)} ok={ttfi === null ? null : ttfi < 3} />
                <Slo name="Analysis finished (95th percentile)" target="under 90 s" value={fmtSeconds(done95)} ok={done95 === null ? null : done95 < 90} />
                <Slo name="Live update delivery, worker to browser (99th percentile)" target="under 150 ms" value={fan === null ? " - " : `${fan}`} unit={fan === null ? "" : " ms"} ok={fan === null || fan === 0 ? null : fan < 150} />
              </tbody>
            </table>
          </section>

          <section>
            <h2 className="section-title">Work</h2>
            <div className="metrics-grid">
              <div><strong className="num">{fmtCount(k.throughput.analyses_24h)}</strong><span>analyses in 24 hours</span></div>
              <div><strong className="num">{fmtCount(k.throughput.simulations_24h)}</strong><span>simulations in 24 hours</span></div>
              <div><strong className="num">{fmtPct(k.throughput.cache_hit_ratio)}</strong><span>simulations answered from the result cache</span></div>
              <div><strong className="num">{fmtPct(k.throughput.adaptive_sims_avoided)}</strong><span>of each grid skipped by adaptive search</span></div>
              <div><strong className="num">{fmtCount(k.throughput.engine_events_per_s)}</strong><span>simulated events per second right now</span></div>
              <div><strong className="num">{k.throughput.analyses_active}</strong><span>analyses running</span></div>
            </div>
          </section>

          <section>
            <h2 className="section-title">Capacity</h2>
            <div className="metrics-grid">
              <div><strong className="num">{k.capacity.worker_busy}/{k.capacity.worker_slots}</strong><span>simulation slots busy across {k.capacity.workers} workers</span></div>
              <div><strong className="num">{fmtCount(k.capacity.backlog)}</strong><span>simulations waiting for a slot</span></div>
              <div><strong className="num">{k.capacity.api_replicas}</strong><span>api replicas behind the load balancer</span></div>
              <div><strong className="num">{k.capacity.gateways}</strong><span>WebSocket gateways</span></div>
              <div><strong className="num">{k.realtime.ws_connections}</strong><span>open live connections</span></div>
              <div><strong className="num">{k.realtime.live_runs}</strong><span>live runs</span></div>
            </div>
            <p className="faint" style={{ fontSize: "var(--t-14)", marginTop: 8 }}>
              Scheduler leader: {k.capacity.scheduler_leader ?? "electing"}.
            </p>
          </section>

          <section>
            <h2 className="section-title">Is it helping people?</h2>
            <div className="metrics-grid">
              <div><strong className="num">{k.product.active_users_24h}</strong><span>people who ran an analysis in 24 hours</span></div>
              <div><strong className="num">{fmtPct(k.product.fix_adoption_7d)}</strong><span>of analyses followed by a fix experiment (7 days)</span></div>
              <div><strong className="num">{k.product.stable_region_gain_avg === null ? " - " : `+${fmtPct(k.product.stable_region_gain_avg)}`}</strong><span>average gain in safe combinations after a fix</span></div>
            </div>
          </section>

          <section>
            <h2 className="section-title">Running instances</h2>
            <div className="panel" style={{ overflowX: "auto" }}>
              <table className="slo-table">
                <thead>
                  <tr>
                    <th>Kind</th>
                    <th>Instance</th>
                    <th>Details</th>
                    <th style={{ textAlign: "right" }}>Last heartbeat</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(k.instances).flatMap(([kind, list]) =>
                    list.map((i) => (
                      <tr key={`${kind}-${i.instance}`}>
                        <td>{kind}</td>
                        <td className="code">{i.instance}</td>
                        <td className="muted">
                          {kind === "worker" && `${i.busy}/${i.slots} slots busy, ${i.live_runs} live runs`}
                          {kind === "gateway" && `${i.connections} connections, ${i.subscriptions ?? 0} subscriptions`}
                          {kind === "api" && `port ${i.port}, ${i.inflight} in flight`}
                          {kind === "scheduler" && (i.leader === "1" ? `leader, ${i.dispatched} tasks dispatched` : "standby")}
                        </td>
                        <td className="n">{Math.max(0, Math.round(k.generated_at - Number(i.ts)))} s ago</td>
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
