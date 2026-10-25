import type { Finding, Topology } from "../lib/types";

export function Topology2D({ topo, findings }: { topo: Topology; findings: Finding[] }) {
  const depth = new Map<string, number>([["client", 0]]);
  const q = ["client"];
  while (q.length) {
    const u = q.shift()!;
    for (const e of topo.edges.filter((x) => x.from === u)) {
      if (!depth.has(e.to)) {
        depth.set(e.to, (depth.get(u) ?? 0) + 1);
        q.push(e.to);
      }
    }
  }
  const layers = new Map<number, string[]>();
  for (const n of topo.nodes) layers.set(depth.get(n.id) ?? 1, [...(layers.get(depth.get(n.id) ?? 1) ?? []), n.id]);
  const maxD = Math.max(...layers.keys());
  const maxRows = Math.max(...[...layers.values()].map((l) => l.length));
  const colW = 128;
  const rowH = 74;
  const W = (maxD + 1) * colW + 40;
  const H = Math.max(1, maxRows) * rowH + 30;
  const pos = new Map<string, { x: number; y: number }>();
  for (const [d, ids] of layers) {
    ids.forEach((id, i) => pos.set(id, { x: 40 + d * colW, y: 26 + (i + (maxRows - ids.length) / 2) * rowH + rowH / 2 - 10 }));
  }
  const risky = new Set(findings.filter((f) => f.severity === "critical" || f.severity === "high").map((f) => f.location));
  const node = new Map(topo.nodes.map((n) => [n.id, n]));

  return (
    <svg className="topo2d" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Call graph of the system">
      <defs>
        <marker id="t2d-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
          <path className="arrow" d="M0,0 L10,5 L0,10 z" />
        </marker>
      </defs>
      {topo.edges.map((e) => {
        const a = pos.get(e.from);
        const b = pos.get(e.to);
        if (!a || !b) return null;
        const isRisky = risky.has(`${e.from} → ${e.to}`) || [...risky].some((r) => r.includes(`${e.from} → ${e.to}`));
        const mx = (a.x + b.x) / 2;
        const label = e.attempts > 1 ? `${e.attempts}× ${e.budget ? "budget" : e.jitter === "none" ? "no jitter" : ""}`.trim() : "";
        return (
          <g key={`${e.from}-${e.to}`}>
            <path
              className={`e${isRisky ? " risky" : ""}`}
              d={`M${a.x + 22},${a.y} C${mx},${a.y} ${mx},${b.y} ${b.x - 24},${b.y}`}
              markerEnd="url(#t2d-arrow)"
            />
            {label && (
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
        return (
          <g key={id}>
            <circle className={`n ${tone}`} cx={p.x} cy={p.y} r={id === "client" ? 12 : 18} strokeWidth={id === topo.bottleneck ? 3 : 1.5} />
            <text x={p.x} y={p.y + 34} textAnchor="middle">
              {id === "client" ? "clients" : id}
            </text>
            {id !== "client" && (
              <text x={p.x} y={p.y + 47} textAnchor="middle" className="sub">
                {Math.round(u * 100)}% busy{id === topo.bottleneck ? " · bottleneck" : ""}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}
