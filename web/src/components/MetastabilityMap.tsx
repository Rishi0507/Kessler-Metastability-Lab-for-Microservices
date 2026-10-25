import { cellState, type CellState } from "../lib/cells";
import { axisValue, fmtMult, fmtSeconds } from "../lib/format";
import type { CellAggregate, SweepParams } from "../lib/types";

export interface MapCell {
  agg?: CellAggregate;
  pending: boolean;
  inferredBad?: boolean | null;
}

export function MetastabilityMap({
  params,
  cells,
  selected,
  onSelect,
  compact = false,
  title,
}: {
  params: SweepParams;
  cells: MapCell[];
  selected?: number | null;
  onSelect?: (cell: number) => void;
  compact?: boolean;
  title?: string;
}) {
  const nCols = params.loads.length;
  const nRows = params.values.length;
  const left = compact ? 46 : 62;
  const top = title ? 26 : 8;
  const W = compact ? 420 : 860;
  const cellW = (W - left - 8) / nCols;
  const cellH = compact ? Math.min(30, 150 / nRows) : Math.min(46, 300 / nRows);
  const H = top + nRows * cellH + (compact ? 34 : 50);
  const kind = params.trigger.kind;

  const state = (i: number): { cls: string; label: string } => {
    const c = cells[i];
    if (!c) return { cls: "s-empty", label: "Not simulated" };
    if (c.agg) {
      const s: CellState = cellState(c.agg);
      const rec = c.agg.recovery_s !== null ? `, median recovery ${fmtSeconds(c.agg.recovery_s)}` : "";
      return { cls: `s-${s}`, label: `${s}${rec}` };
    }
    if (c.pending) return { cls: "s-pending", label: "Simulating" };
    if (c.inferredBad !== undefined && c.inferredBad !== null) {
      return { cls: `${c.inferredBad ? "s-metastable" : "s-stable"} inferred`, label: c.inferredBad ? "Inferred: does not recover" : "Inferred: recovers" };
    }
    return { cls: "s-empty", label: "Not simulated" };
  };

  const xOfLoad = (load: number): number | null => {
    const L = params.loads;
    if (load < (L[0] ?? 0) || load > (L[nCols - 1] ?? 0)) return null;
    for (let c = 0; c < nCols - 1; c++) {
      const a = L[c] ?? 0;
      const b = L[c + 1] ?? 0;
      if (load >= a && load <= b) {
        const f = b > a ? (load - a) / (b - a) : 0;
        return left + (c + 0.5 + f) * cellW;
      }
    }
    return left + (nCols - 0.5) * cellW;
  };

  const boundaryX: number[] = [];
  for (let r = 0; r < nRows; r++) {
    const row = nRows - 1 - r;
    let x = left + nCols * cellW;
    for (let col = 0; col < nCols; col++) {
      const c = cells[row * nCols + col];
      const bad = c?.agg ? cellState(c.agg) === "metastable" || cellState(c.agg) === "overloaded" : c?.inferredBad;
      if (bad) {
        x = left + col * cellW;
        break;
      }
    }
    boundaryX.push(x);
  }
  const anyBoundary = boundaryX.some((x) => x < left + nCols * cellW);
  let path = "";
  boundaryX.forEach((x, r) => {
    const y0 = top + r * cellH;
    path += `${r === 0 ? "M" : "L"}${x},${y0} L${x},${y0 + cellH} `;
  });
  const today = xOfLoad(1);
  const showEvery = nCols > 8 ? 2 : 1;

  return (
    <svg className="map-svg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title ?? "Metastability map"}>
      <defs>
        <pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="6" stroke="var(--surface)" strokeWidth="2" />
        </pattern>
      </defs>
      {title && (
        <text x={left} y={16} className="axis-title">
          {title}
        </text>
      )}
      {Array.from({ length: nRows }, (_, r) => {
        const row = nRows - 1 - r;
        return (
          <text key={`y${row}`} x={left - 8} y={top + r * cellH + cellH / 2 + 4} textAnchor="end">
            {axisValue(kind, params.values[row] ?? 0)}
          </text>
        );
      })}
      {Array.from({ length: nRows * nCols }, (_, i) => {
        const row = Math.floor(i / nCols);
        const col = i % nCols;
        const r = nRows - 1 - row;
        const x = left + col * cellW;
        const y = top + r * cellH;
        const s = state(i);
        return (
          <g key={i}>
            <rect
              className={`cell ${s.cls}${selected === i ? " sel" : ""}`}
              x={x}
              y={y}
              width={cellW}
              height={cellH}
              rx={3}
              onClick={() => onSelect?.(i)}
              tabIndex={onSelect ? 0 : -1}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") onSelect?.(i);
              }}
            >
              <title>{`Load ${fmtMult(params.loads[col] ?? 0)}, ${axisValue(kind, params.values[row] ?? 0)}: ${s.label}`}</title>
            </rect>
            {s.cls.includes("inferred") && <rect x={x} y={y} width={cellW} height={cellH} rx={3} fill="url(#hatch)" pointerEvents="none" />}
          </g>
        );
      })}
      {anyBoundary && <path className="boundary" d={path} />}
      {today !== null && (
        <>
          <line className="today" x1={today} x2={today} y1={top - 4} y2={top + nRows * cellH + 4} />
          {!compact && (
            <text className="today-label" x={today} y={top + nRows * cellH + 18} textAnchor="middle">
              today
            </text>
          )}
        </>
      )}
      {params.loads.map((l, col) =>
        col % showEvery === 0 || col === nCols - 1 ? (
          <text key={`x${col}`} x={left + (col + 0.5) * cellW} y={top + nRows * cellH + (compact ? 14 : 34)} textAnchor="middle">
            {fmtMult(l)}
          </text>
        ) : null,
      )}
      {!compact && (
        <text className="axis-title" x={left + (nCols * cellW) / 2} y={H - 2} textAnchor="middle">
          Load, as a multiple of today's traffic
        </text>
      )}
    </svg>
  );
}
