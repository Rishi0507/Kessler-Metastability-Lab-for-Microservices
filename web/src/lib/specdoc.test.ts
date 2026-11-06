import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { applyEdits, coerce, readSpec } from "./specdoc";

const dir = join(__dirname, "../../../backend/kessler/spec/templates");
const templates = readdirSync(dir)
  .filter((f) => f.endsWith(".yaml"))
  .map((f) => [f, readFileSync(join(dir, f), "utf8").replace(/\r\n/g, "\n")] as const);
const storm = templates.find(([f]) => f === "checkout-retry-storm.yaml")![1];

const changedLines = (a: string, b: string) => {
  const x = a.split("\n");
  const y = b.split("\n");
  return y.filter((l, i) => l !== x[i]);
};

describe("spec edits keep the rest of the file", () => {
  it.each(templates)("rewriting every service leaves %s unchanged", (_, src) => {
    const names = Object.keys(readSpec(src).spec.services);
    let out = src;
    for (const n of names) {
      const v = readSpec(out).spec.services[n].instances;
      out = applyEdits(out, [{ op: "set", path: ["services", n, "description"], value: "x" }]);
      out = applyEdits(out, [{ op: "delete", path: ["services", n, "description"] }]);
      out = applyEdits(out, [{ op: "set", path: ["services", n, "instances"], value: v }]);
    }
    const original = readSpec(src).spec;
    for (const n of names) delete original.services[n].description;
    expect(readSpec(out).spec).toEqual(original);
    const top = src.slice(0, src.indexOf("services:"));
    expect(out.startsWith(top)).toBe(true);
  }, 30_000);

  it("changes one scalar in place", () => {
    const out = applyEdits(storm, [{ op: "set", path: ["services", "db", "concurrency"], value: 20 }]);
    expect(changedLines(storm, out)).toEqual(["    concurrency: 20"]);
  });

  it("edits inside a flow map", () => {
    const out = applyEdits(storm, [{ op: "set", path: ["traffic", "client", "retry", "jitter"], value: "full" }]);
    expect(changedLines(storm, out)).toEqual(["    retry: { attempts: 3, base: 100ms, cap: 400ms, jitter: full }"]);
  });

  it("adds a key to a flow map", () => {
    const out = applyEdits(storm, [{ op: "set", path: ["services", "db", "queue", "discipline"], value: "lifo" }]);
    expect(changedLines(storm, out)).toEqual(["    queue: { capacity: 2000, discipline: lifo }"]);
  });

  it("adds a small map in flow style", () => {
    const out = applyEdits(storm, [{ op: "set", path: ["services", "inventory", "autoscale"], value: { min: 2, max: 6 } }]);
    expect(out).toContain("    autoscale: { min: 2, max: 6 }\n");
    expect(readSpec(out).spec.services.inventory.autoscale).toEqual({ min: 2, max: 6 });
  });

  it("adds a service and a call", () => {
    let out = applyEdits(storm, [
      { op: "set", path: ["services", "payments"], value: { instances: 2, concurrency: 16, latency: { p50: "5ms", p99: "20ms" } } },
    ]);
    const calls = readSpec(out).spec.services.checkout.calls;
    out = applyEdits(out, [{ op: "set", path: ["services", "checkout", "calls"], value: [...calls, { to: "payments", timeout: "300ms" }] }]);
    const spec = readSpec(out).spec;
    expect(spec.services.payments.latency).toEqual({ p50: "5ms", p99: "20ms" });
    expect(spec.services.checkout.calls.at(-1)).toEqual({ to: "payments", timeout: "300ms" });
    expect(out).toContain("      - to: payments\n        timeout: 300ms\n");
  });

  it("adds a top-level section at the end", () => {
    const out = applyEdits(storm, [{ op: "set", path: ["resilience", "deadline_propagation"], value: true }]);
    expect(readSpec(out).spec.resilience).toEqual({ deadline_propagation: true });
    expect(out.startsWith(storm)).toBe(true);
  });

  it("renames a service and every reference to it", () => {
    const out = applyEdits(storm, [{ op: "rename", from: "db", to: "orders-db" }]);
    const spec = readSpec(out).spec;
    expect(Object.keys(spec.services)).toEqual(["gateway", "checkout", "inventory", "orders-db"]);
    expect(spec.services.checkout.calls.map((c: { to: string }) => c.to)).toEqual(["inventory", "orders-db"]);
    expect(changedLines(storm, out)).toHaveLength(2);
  });

  it("renames the entry service", () => {
    const out = applyEdits(storm, [{ op: "rename", from: "gateway", to: "edge" }]);
    expect(readSpec(out).spec.traffic.entry).toBe("edge");
  });

  it("refuses a rename onto an existing service", () => {
    expect(applyEdits(storm, [{ op: "rename", from: "db", to: "checkout" }])).toBe(storm);
  });

  it("quotes strings with commas inside flow maps", () => {
    const out = applyEdits(storm, [{ op: "set", path: ["services", "db", "latency", "p50"], value: "5ms, 6ms" }]);
    expect(readSpec(out).spec.services.db.latency.p50).toBe("5ms, 6ms");
  });

  it("reports broken YAML", () => {
    expect(readSpec("services: [").ok).toBe(false);
  });

  it("turns form text into numbers or strings", () => {
    expect(coerce(" 12 ")).toBe(12);
    expect(coerce("0.5")).toBe(0.5);
    expect(coerce("250ms")).toBe("250ms");
    expect(coerce("")).toBeUndefined();
  });
});
