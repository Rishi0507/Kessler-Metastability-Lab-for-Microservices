import { useMemo, useState } from "react";
import { fmtMult } from "../lib/format";
import type { Topology } from "../lib/types";
import { Dialog } from "./ui";

export interface TriggerChoice {
  trigger: { kind: string; target?: string; duration_s?: number };
  seeds: number;
  mode: "adaptive" | "grid";
}

const KINDS: { kind: string; label: string; needsTarget: boolean; duration: boolean; explain: string }[] = [
  { kind: "latency", label: "Slowdown", needsTarget: true, duration: true, explain: "slow a service down by 1× to 8×" },
  { kind: "errors", label: "Error burst", needsTarget: true, duration: true, explain: "make a service fail 10% to 100% of requests" },
  { kind: "crash", label: "Outage", needsTarget: true, duration: false, explain: "take a service down for 5 to 60 seconds" },
  { kind: "spike", label: "Traffic spike", needsTarget: false, duration: true, explain: "multiply incoming traffic by 1.2× to 4×" },
  { kind: "cache_flush", label: "Cache flush", needsTarget: true, duration: false, explain: "empty a cache instantly" },
  { kind: "capacity_loss", label: "Capacity loss", needsTarget: true, duration: true, explain: "remove 10% to 80% of a service's workers" },
];

export function TriggerDialog({
  open,
  onClose,
  topo,
  maxLoad,
  onSubmit,
  busy,
}: {
  open: boolean;
  onClose: () => void;
  topo: Topology | undefined;
  maxLoad: number;
  onSubmit: (c: TriggerChoice) => void;
  busy: boolean;
}) {
  const services = topo?.nodes.filter((n) => n.kind !== "client") ?? [];
  const caches = services.filter((n) => n.kind === "cache");
  const defaultTarget = topo?.bottleneck && topo.bottleneck !== topo.entry ? topo.bottleneck : services.at(-1)?.id;
  const [kind, setKind] = useState("latency");
  const [target, setTarget] = useState<string | undefined>();
  const [duration, setDuration] = useState(20);
  const [seeds, setSeeds] = useState(3);
  const [mode, setMode] = useState<"adaptive" | "grid">("adaptive");
  const meta = KINDS.find((k) => k.kind === kind)!;
  const targets = kind === "cache_flush" ? caches : services;
  const pick = (id: string | undefined) => (id && targets.some((t) => t.id === id) ? id : undefined);
  const effTarget = meta.needsTarget ? (pick(target) ?? pick(defaultTarget) ?? targets[0]?.id) : undefined;
  const sims = useMemo(() => {
    const rows = kind === "cache_flush" ? 1 : 6;
    return mode === "grid" ? rows * 10 * seeds : rows * 6 * seeds;
  }, [kind, mode, seeds]);
  const lo = Math.min(0.2 * maxLoad, 0.5);
  const hi = Math.max(1.2 * maxLoad, 1.2);

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Find tipping points"
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            disabled={busy || (meta.needsTarget && !effTarget)}
            onClick={() =>
              onSubmit({
                trigger: { kind, ...(effTarget ? { target: effTarget } : {}), ...(meta.duration ? { duration_s: duration } : {}) },
                seeds,
                mode,
              })
            }
          >
            {busy ? "Starting…" : "Run the sweep"}
          </button>
        </>
      }
    >
      <div className="field">
        <span className="label">What goes wrong</span>
        <div className="fault-grid">
          {KINDS.filter((k) => k.kind !== "cache_flush" || caches.length > 0).map((k) => (
            <button key={k.kind} className={`btn btn-sm${k.kind === kind ? " btn-primary" : ""}`} aria-pressed={k.kind === kind} onClick={() => setKind(k.kind)}>
              {k.label}
            </button>
          ))}
        </div>
      </div>
      {meta.needsTarget && (
        <div className="field">
          <label htmlFor="trg-target">Where</label>
          <select id="trg-target" className="select" value={effTarget} onChange={(e) => setTarget(e.target.value)}>
            {targets.map((s) => (
              <option key={s.id} value={s.id}>
                {s.id}
                {s.id === topo?.bottleneck ? " (bottleneck)" : ""}
              </option>
            ))}
          </select>
        </div>
      )}
      {meta.duration && (
        <div className="field">
          <label htmlFor="trg-dur">For how long: {duration} s</label>
          <input id="trg-dur" type="range" min={5} max={120} step={5} value={duration} onChange={(e) => setDuration(Number(e.target.value))} />
        </div>
      )}
      <div className="row" style={{ gap: 18 }}>
        <div className="field" style={{ flex: 1 }}>
          <label htmlFor="trg-seeds">Repeats per cell: {seeds}</label>
          <input id="trg-seeds" type="range" min={1} max={5} value={seeds} onChange={(e) => setSeeds(Number(e.target.value))} />
        </div>
        <div className="field">
          <span className="label">Search</span>
          <div className="seg" role="group" aria-label="Search mode">
            <button aria-pressed={mode === "adaptive"} onClick={() => setMode("adaptive")}>
              Adaptive
            </button>
            <button aria-pressed={mode === "grid"} onClick={() => setMode("grid")}>
              Full grid
            </button>
          </div>
        </div>
      </div>
      <p className="muted" style={{ fontSize: "var(--t-14)" }}>
        Kessler will {meta.explain}
        {effTarget ? ` (${effTarget})` : ""} at 10 load levels from {fmtMult(lo)} to {fmtMult(hi)} today's traffic, repeating each
        combination {seeds} {seeds === 1 ? "time" : "times"}: about {sims} simulations.
        {mode === "adaptive" && " Adaptive search zooms in on the boundary and skips cells whose outcome is already implied."}
      </p>
    </Dialog>
  );
}
