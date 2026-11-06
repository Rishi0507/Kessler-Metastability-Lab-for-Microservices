import { layeredLayout } from "../lib/layout";
import type { Finding, Topology } from "../lib/types";

const COL_W = 128;
const ROW_H = 74;
const FIT_MAX_NODES = 14;

export function Topology2D({
  topo,
  findings,
  selected = null,
  onSelect,
}: {
  topo: Topology;
  findings: Finding[];
  selected?: string | null;
  onSelect?: (id: string) => void;
}) {
  const { layers } = layeredLayout(topo);
  const ids = layers.flat();
  const nLayers = layers.length;
  const maxRows = Math.max(1, ...layers.map((l) => l.length));
  const W = nLayers * COL_W + 40;
  const H = maxRows * ROW_H + 30;
  const pos = new Map<string, { x: number; y: number }>();
  layers.forEach((layer, d) =>
    layer.forEach((id, i) => pos.set(id, { x: 40 + d * COL_W, y: 26 + (i + (maxRows - layer.length) / 2) * ROW_H + ROW_H / 2 - 10 })),
  );

  const flagged = findings.filter((f) => f.severity === "critical" || f.severity === "high");
  const places = new Set(flagged.flatMap((f) => (f.locations?.length ? f.locations.slice(0, 3) : [f.location])));
  const riskyEdge = (from: string, to: string) => [...places].some((p) => p.includes(`${from} → ${to}`));
  const node = new Map(topo.nodes.map((n) => [n.id, n]));
  const big = ids.length > FIT_MAX_NODES;

  const svg = (
    <svg
      className="topo2d"
      viewBox={`0 0 ${W} ${H}`}
      style={big ? { width: W, minWidth: W } : undefined}
      role="img"
      aria-label={`Call graph of ${ids.length - 1} services`}
    >
      <defs>
        <marker id="t2d-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
          <path className="arrow" d="M0,0 L10,5 L0,10 z" />
        </marker>
      </defs>
      {topo.edges.map((e) => {
        const a = pos.get(e.from);
        const b = pos.get(e.to);
        if (!a || !b) return null;
        const mx = (a.x + b.x) / 2;
        const label = e.attempts > 1 ? `${e.attempts}× ${e.budget ? "budget" : e.jitter === "none" ? "no jitter" : ""}`.trim() : "";
        return (
          <g key={`${e.from}-${e.to}`}>
            <path
              className={`e${riskyEdge(e.from, e.to) ? " risky" : ""}`}
              d={`M${a.x + 22},${a.y} C${mx},${a.y} ${mx},${b.y} ${b.x - 24},${b.y}`}
              markerEnd="url(#t2d-arrow)"
            >
              <title>{`${e.from} calls ${e.to}${label ? `: ${label}` : ""}`}</title>
            </path>
            {label && !big && (
              <text x={mx} y={(a.y + b.y) / 2 - 7} textAnchor="middle" className="sub">
                {label}
              </text>
            )}
          </g>
        );
      })}
      {[...pos.entries()].map(([id, p]) => {
        const n = node.get(id);
        const u = n?.utilization ?? 0;
        const tone = id === "client" ? "" : u > 0.85 ? "hot" : u > 0.6 ? "warm" : "cool";
        const pick = onSelect && id !== "client" ? () => onSelect(id) : undefined;
        return (
          <g
            key={id}
            className={pick ? `pick${id === selected ? " sel" : ""}` : undefined}
            onClick={pick}
            onKeyDown={pick && ((e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), pick()))}
            role={pick ? "button" : undefined}
            tabIndex={pick ? 0 : undefined}
            aria-label={pick ? `Edit ${id}` : undefined}
          >
            <circle
              className={`n ${tone}${places.has(id) ? " risky" : ""}`}
              cx={p.x}
              cy={p.y}
              r={id === "client" ? 12 : 18}
              strokeWidth={id === topo.bottleneck ? 3 : 1.5}
            >
              <title>{n?.description ? `${id}: ${n.description}` : id}</title>
            </circle>
            <text x={p.x} y={p.y + 34} textAnchor="middle">
              {id === "client" ? "clients" : id}
            </text>
            {id !== "client" && (
              <text x={p.x} y={p.y + 47} textAnchor="middle" className="sub">
                {Math.round(u * 100)}% busy{id === topo.bottleneck ? ", bottleneck" : ""}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );

  if (!big) return svg;
  return (
    <>
      <p className="hint">
        {ids.length - 1} services. Scroll to explore.
      </p>
      <div className="topo2d-scroll" tabIndex={0} aria-label="Call graph, scrollable">
        {svg}
      </div>
    </>
  );
}
