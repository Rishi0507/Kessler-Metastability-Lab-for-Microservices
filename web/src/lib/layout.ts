import type { Topology } from "./types";

export interface Layered {
  layers: string[][];
  depth: Map<string, number>;
  slot: Map<string, number>;
  callers: Map<string, string[]>;
}

export function layeredLayout(topo: Topology): Layered {
  const ids = ["client", ...topo.nodes.filter((n) => n.id !== "client").map((n) => n.id)];
  const callers = new Map<string, string[]>(ids.map((id) => [id, []]));
  for (const e of topo.edges) callers.get(e.to)?.push(e.from);

  const depth = new Map<string, number>();
  const visit = (id: string, seen: Set<string>): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    if (seen.has(id)) return 0;
    seen.add(id);
    const ps = callers.get(id) ?? [];
    const d = id === "client" ? 0 : ps.length ? Math.max(...ps.map((p) => visit(p, seen))) + 1 : 1;
    depth.set(id, d);
    return d;
  };
  ids.forEach((id) => visit(id, new Set()));

  const layers: string[][] = [];
  for (const id of ids) (layers[depth.get(id) ?? 1] ??= []).push(id);
  for (let i = 0; i < layers.length; i++) layers[i] ??= [];

  const slot = new Map<string, number>();
  for (let sweep = 0; sweep < 2; sweep++) {
    for (const layer of layers) {
      const bary = (id: string) => {
        const ps = (callers.get(id) ?? []).filter((p) => slot.has(p));
        return ps.length ? ps.reduce((s, p) => s + (slot.get(p) ?? 0), 0) / ps.length : (slot.get(id) ?? 0);
      };
      layer.sort((a, b) => bary(a) - bary(b));
      layer.forEach((id, i) => slot.set(id, i - (layer.length - 1) / 2));
    }
  }
  return { layers, depth, slot, callers };
}
