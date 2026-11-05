import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { MetastabilityMap, type MapCell } from "../components/MetastabilityMap";
import { InfoTip, SeverityDot, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { aggregate } from "../lib/cells";
import { fmtPct, triggerPhrase } from "../lib/format";
import type { AnalysisResult, CellView, SimSummary, SweepParams } from "../lib/types";

interface Report {
  analysis: { id: string; created_at: string; finished_at: string; result: AnalysisResult; params: SweepParams; sims_done: number };
  cells: (CellView & { summaries?: (SimSummary & { seed: number })[] })[];
  system: { name: string; version: number; spec_yaml: string };
  author: string;
}

export default function ReportPage() {
  const { slug = "" } = useParams();
  const q = useQuery({ queryKey: ["report", slug], queryFn: () => api.get<Report>(`/api/v1/reports/${slug}`), staleTime: Infinity });
  if (q.isError) {
    return (
      <div className="page">
        <div className="empty">
          <h1 className="display">Report not found</h1>
          <p className="muted">The link may be mistyped.</p>
        </div>
      </div>
    );
  }
  const r = q.data;
  if (!r) return <div className="page"><Skeleton h={480} /></div>;
  const p = r.analysis.params;
  const n = p.loads.length * p.values.length;
  const byCell = new Map(r.cells.map((c) => [c.cell, c]));
  const cells: MapCell[] = Array.from({ length: n }, (_, i) => {
    const c = byCell.get(i);
    const sums = c?.summaries ?? [];
    return {
      agg: sums.length >= p.seeds ? aggregate(sums) : undefined,
      pending: false,
      inferredBad: r.analysis.result.verdicts?.[i]?.inferred ? r.analysis.result.verdicts[i]!.bad : undefined,
    };
  });

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <p className="faint" style={{ fontSize: "var(--t-14)" }}>
            Shared by {r.author}, {new Date(r.analysis.finished_at).toLocaleDateString()}
          </p>
          <h1 className="display">
            {r.system.name}: what happens after {triggerPhrase(p.trigger)}
          </h1>
          <p className="sub">
            {fmtPct(r.analysis.result.safe_fraction)} of load and trigger combinations recover on their own. {r.analysis.sims_done} deterministic
            simulations, version {r.system.version}.
          </p>
        </div>
        <div className="spacer" />
        <Link className="btn btn-primary" to="/">
          Map your own system
        </Link>
      </div>
      <div className="analysis-grid">
        <div className="map-wrap">
          <MetastabilityMap params={p} cells={cells} />
        </div>
        <aside className="stack">
          {r.analysis.result.findings.length > 0 && (
            <div className="panel panel-pad">
              <ul className="rows">
                {r.analysis.result.findings.map((f) => (
                  <li key={f.rule} className="row-item">
                    <SeverityDot severity={f.severity} />
                    <span className="row-title">{f.title}</span>
                    <InfoTip>{f.detail}</InfoTip>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <details className="panel panel-pad">
            <summary style={{ cursor: "pointer", fontWeight: 600 }}>System spec</summary>
            <pre className="code" style={{ overflowX: "auto", fontSize: 12.5, marginTop: 12 }}>{r.system.spec_yaml}</pre>
          </details>
        </aside>
      </div>
    </div>
  );
}
