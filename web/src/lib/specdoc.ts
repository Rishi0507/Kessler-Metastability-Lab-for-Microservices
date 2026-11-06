import { Document, isCollection, isMap, isScalar, isSeq, parseDocument, YAMLMap, YAMLSeq, type Node } from "yaml";

export type Path = (string | number)[];
export type Edit =
  | { op: "set"; path: Path; value: unknown }
  | { op: "delete"; path: Path }
  | { op: "rename"; from: string; to: string };

const FLOW_MAX_KEYS = 6;

export function readSpec(text: string): { ok: boolean; spec: Record<string, any> } {
  const doc = parseDocument(text);
  if (doc.errors.length || !isMap(doc.contents)) return { ok: false, spec: {} };
  return { ok: true, spec: doc.toJS() ?? {} };
}

function isFlowable(v: unknown): boolean {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return false;
  const vals = Object.values(v);
  return vals.length <= FLOW_MAX_KEYS && vals.every((x) => typeof x !== "object" || x === null);
}

function toNode(value: unknown, inSeq = false): unknown {
  if (Array.isArray(value)) {
    const seq = new YAMLSeq();
    for (const v of value) seq.add(toNode(v, true));
    return seq;
  }
  if (typeof value === "object" && value !== null) {
    const map = new YAMLMap();
    for (const [k, v] of Object.entries(value)) map.set(k, toNode(v));
    map.flow = !inSeq && isFlowable(value);
    return map;
  }
  return value;
}

function render(node: unknown, col: number, block: boolean): string {
  const d = new Document();
  d.contents = node as Node;
  const out = d.toString({ lineWidth: 0 });
  const lines = out.replace(/\n$/, "").split("\n");
  const pad = " ".repeat(col);
  const body = lines.map((l, i) => (i === 0 || l === "" ? l : pad + l)).join("\n");
  return block ? body + "\n" : body;
}

function column(text: string, at: number): number {
  return at - (text.lastIndexOf("\n", at - 1) + 1);
}

function scalarText(value: unknown, inFlow: boolean): string {
  const s = new Document(value).toString({ lineWidth: 0 }).replace(/\n$/, "");
  return inFlow && typeof value === "string" && /[,{}[\]]/.test(s) && !/^["']/.test(s) ? JSON.stringify(value) : s;
}

function splice(text: string, from: number, to: number, insert: string): string {
  return text.slice(0, from) + insert + text.slice(to);
}

function replaceNode(text: string, node: Node): string {
  const [start, end] = node.range!;
  const block = text[end - 1] === "\n";
  return splice(text, start, end, render(node, column(text, start), block));
}

function nested(path: Path, value: unknown): unknown {
  return path.reduceRight<unknown>((acc, key) => ({ [key]: acc }), value);
}

function setValue(text: string, path: Path, value: unknown): string {
  const doc = parseDocument(text);
  const existing = doc.getIn(path, true) as Node | undefined;
  const parent = doc.getIn(path.slice(0, -1), true);
  if (isScalar(existing) && (typeof value !== "object" || value === null) && existing.range) {
    const [start, end] = existing.range;
    return splice(text, start, end, scalarText(value, !!(isCollection(parent) && parent.flow)));
  }
  let depth = path.length - 1;
  while (depth > 0 && !isCollection(doc.getIn(path.slice(0, depth), true))) depth--;
  if (depth === 0) {
    const tail = text.endsWith("\n") || text === "" ? "" : "\n";
    return text + tail + render(toNode(nested(path, value)), 0, true);
  }
  const anchor = doc.getIn(path.slice(0, depth), true) as Node;
  doc.setIn(path, toNode(value));
  return replaceNode(text, anchor);
}

function deleteValue(text: string, path: Path): string {
  const doc = parseDocument(text);
  if (path.length < 2 || !doc.hasIn(path)) return text;
  const parent = doc.getIn(path.slice(0, -1), true) as Node;
  if (!isCollection(parent)) return text;
  doc.deleteIn(path);
  return replaceNode(text, parent);
}

function renameService(text: string, from: string, to: string): string {
  const doc = parseDocument(text);
  const services = doc.get("services", true);
  if (!isMap(services) || from === to || services.has(to)) return text;
  const spots: [number, number][] = [];
  for (const pair of services.items) {
    if (isScalar(pair.key) && pair.key.value === from && pair.key.range) spots.push([pair.key.range[0], pair.key.range[1]]);
    if (!isMap(pair.value)) continue;
    const calls = pair.value.get("calls", true);
    if (isSeq(calls)) {
      for (const c of calls.items) {
        const t = isMap(c) ? c.get("to", true) : null;
        if (isScalar(t) && t.value === from && t.range) spots.push([t.range[0], t.range[1]]);
      }
    }
    const miss = pair.value.getIn(["cache", "miss_target"], true);
    if (isScalar(miss) && miss.value === from && miss.range) spots.push([miss.range[0], miss.range[1]]);
  }
  const entry = doc.getIn(["traffic", "entry"], true);
  if (isScalar(entry) && entry.value === from && entry.range) spots.push([entry.range[0], entry.range[1]]);
  spots.sort((a, b) => b[0] - a[0]);
  return spots.reduce((t, [s, e]) => splice(t, s, e, to), text);
}

export function applyEdits(text: string, edits: Edit[]): string {
  return edits.reduce((t, e) => {
    if (e.op === "set") return setValue(t, e.path, e.value);
    if (e.op === "delete") return deleteValue(t, e.path);
    return renameService(t, e.from, e.to);
  }, text);
}

export function coerce(raw: string): unknown {
  const s = raw.trim();
  if (s === "") return undefined;
  if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
  return s;
}
