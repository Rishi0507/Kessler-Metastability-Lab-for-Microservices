import "uplot/dist/uPlot.min.css";
import { useEffect, useRef } from "react";
import uPlot from "uplot";
import { cssVar } from "../app/theme";

export interface SeriesBuffer {
  t: number[];
  ratio: number[];
  p99: number[];
  amp: number[];
}

function Chart({ label, value, xs, ys, color, fmt, yMax }: {
  label: string;
  value: string;
  xs: number[];
  ys: number[];
  color: string;
  fmt: (v: number) => string;
  yMax?: number;
}) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const stroke = cssVar(color) || "#3346d3";
    const grid = cssVar("--rule") || "#dce1ea";
    const ink = cssVar("--ink-3") || "#7a8396";
    const u = new uPlot(
      {
        width: el.clientWidth || 280,
        height: 74,
        cursor: { show: false },
        legend: { show: false },
        scales: { x: { time: false }, y: { range: (_u, min, max) => [Math.min(0, min), yMax ?? Math.max(max * 1.15, 1e-6)] } },
        axes: [
          { show: false },
          { stroke: ink, size: 40, grid: { stroke: grid, width: 1 }, ticks: { show: false }, values: (_u, vals) => vals.map(fmt), font: "11px -apple-system, \"Inter Variable\", sans-serif" },
        ],
        series: [{}, { stroke, width: 2, fill: `${stroke}22`, points: { show: false } }],
      },
      [[], []],
      el,
    );
    plot.current = u;
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height: 74 }));
    ro.observe(el);
    return () => {
      ro.disconnect();
      u.destroy();
      plot.current = null;
    };
  }, [color, fmt, yMax]);

  useEffect(() => {
    plot.current?.setData([xs, ys]);
  });

  return (
    <div className="chart">
      <div className="head">
        <span>{label}</span>
        <span className="value">{value}</span>
      </div>
      <div ref={host} />
    </div>
  );
}

const pct = (v: number) => `${Math.round(v * 100)}%`;
const ms = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`);
const mult = (v: number) => `${v.toFixed(1)}×`;

export function LiveCharts({ data }: { data: SeriesBuffer }) {
  const last = (a: number[]) => a[a.length - 1];
  return (
    <div className="charts">
      <Chart label="Requests succeeding" value={last(data.ratio) !== undefined ? pct(last(data.ratio)!) : " - "} xs={data.t} ys={data.ratio} color="--stable" fmt={pct} yMax={1.05} />
      <Chart label="Slowest 1% of requests" value={last(data.p99) !== undefined && last(data.ratio) !== 0 ? ms(last(data.p99)!) : " - "} xs={data.t} ys={data.p99} color="--slow" fmt={ms} />
      <Chart label="Load amplification" value={last(data.amp) !== undefined ? mult(last(data.amp)!) : " - "} xs={data.t} ys={data.amp} color="--meta" fmt={mult} />
    </div>
  );
}
