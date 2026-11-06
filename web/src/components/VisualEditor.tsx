import { Plus, Trash2 } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { applyEdits, coerce, readSpec, type Edit, type Path } from "../lib/specdoc";
import type { SpecIssue } from "../lib/types";
import { InfoTip } from "./ui";

type Obj = Record<string, any>;
type Commit = (path: Path, value: unknown) => void;

const NAME = /^[a-z][a-z0-9_-]{0,47}$/;
const JITTER = ["none", "full", "equal", "decorrelated"];
const LB = ["round_robin", "random", "least_outstanding", "p2c"];
const DISCIPLINE = ["fifo", "lifo", "adaptive_lifo"];
const NEW_RETRY = { attempts: 2, base: "50ms", jitter: "full" };
const NEW_BREAKER = { failure_rate: "50%", window: "10s", open_for: "5s" };

const shown = (v: unknown) => (v === undefined || v === null ? "" : String(v));

function Text({
  label,
  value,
  placeholder,
  onCommit,
  raw = false,
  wide = false,
}: {
  label: string;
  value: unknown;
  placeholder?: string;
  onCommit: (v: unknown) => void;
  raw?: boolean;
  wide?: boolean;
}) {
  const id = useId();
  const [text, setText] = useState(shown(value));
  const focused = useRef(false);
  useEffect(() => {
    if (!focused.current) setText(shown(value));
  }, [value]);
  return (
    <div className={`field${wide ? " v-wide" : ""}`}>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        className="input"
        value={text}
        placeholder={placeholder}
        spellCheck={false}
        onFocus={() => (focused.current = true)}
        onBlur={() => {
          focused.current = false;
          setText(shown(value));
        }}
        onChange={(e) => {
          setText(e.target.value);
          onCommit(raw ? e.target.value.trim() || undefined : coerce(e.target.value));
        }}
      />
    </div>
  );
}

function Choice({ label, value, options, onCommit }: { label: string; value: string; options: string[]; onCommit: (v: string) => void }) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <select id={id} className="select" value={value} onChange={(e) => onCommit(e.target.value)}>
        {options.map((o) => (
          <option key={o} value={o}>
            {o.replace(/_/g, " ")}
          </option>
        ))}
      </select>
    </div>
  );
}

function Toggle({ label, on, onChange }: { label: string; on: boolean; onChange: (on: boolean) => void }) {
  return (
    <label className="v-check">
      <input type="checkbox" checked={on} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

function Section({ title, tip, children, action }: { title: string; tip?: string; children: ReactNode; action?: ReactNode }) {
  return (
    <section className="v-section">
      <div className="title-row">
        <h3 className="v-title">{title}</h3>
        {tip && <InfoTip>{tip}</InfoTip>}
        <div className="spacer" />
        {action}
      </div>
      {children}
    </section>
  );
}

function RetryFields({ at, retry, budget, set }: { at: Path; retry: Obj | undefined; budget: unknown; set: Commit }) {
  const ratioPath = typeof budget === "object" && budget !== null ? [...at, "budget", "ratio"] : [...at, "budget"];
  const ratio = typeof budget === "object" && budget !== null ? (budget as Obj).ratio : budget;
  return (
    <>
      <div className="v-row">
        <Toggle label="Retries" on={!!retry} onChange={(on) => set([...at, "retry"], on ? NEW_RETRY : undefined)} />
        <Toggle label="Retry budget" on={budget !== undefined} onChange={(on) => set([...at, "budget"], on ? "10%" : undefined)} />
      </div>
      {(retry || budget !== undefined) && (
        <div className="v-grid">
          {retry && (
            <>
              <Text label="Attempts" value={retry.attempts} placeholder="1" onCommit={(v) => set([...at, "retry", "attempts"], v)} />
              <Text label="First backoff" value={retry.base} placeholder="50ms" onCommit={(v) => set([...at, "retry", "base"], v)} />
              <Text label="Max backoff" value={retry.cap} placeholder="1000ms" onCommit={(v) => set([...at, "retry", "cap"], v)} />
              <Choice label="Jitter" value={retry.jitter ?? "full"} options={JITTER} onCommit={(v) => set([...at, "retry", "jitter"], v)} />
            </>
          )}
          {budget !== undefined && <Text label="Budget" value={ratio} placeholder="10%" onCommit={(v) => set(ratioPath, v)} />}
        </div>
      )}
    </>
  );
}

function CallCard({ svc, i, call, others, set, onRemove }: { svc: string; i: number; call: Obj; others: string[]; set: Commit; onRemove: () => void }) {
  const at: Path = ["services", svc, "calls", i];
  return (
    <div className="v-call">
      <div className="v-grid">
        <Choice label="Calls" value={call.to} options={others.includes(call.to) ? others : [call.to, ...others]} onCommit={(v) => set([...at, "to"], v)} />
        <Text label="Timeout" value={call.timeout} placeholder="1000ms" onCommit={(v) => set([...at, "timeout"], v)} />
        <Text label="Share of requests" value={call.probability} placeholder="100%" onCommit={(v) => set([...at, "probability"], v)} />
      </div>
      <RetryFields at={at} retry={call.retry} budget={call.budget} set={set} />
      <div className="v-row">
        <Toggle label="Circuit breaker" on={!!call.breaker} onChange={(on) => set([...at, "breaker"], on ? NEW_BREAKER : undefined)} />
        <div className="spacer" />
        <button type="button" className="btn btn-sm btn-ghost" onClick={onRemove}>
          <Trash2 size={13} /> Remove call
        </button>
      </div>
    </div>
  );
}

function ServiceForm({ spec, name, edit, set, onRenamed }: { spec: Obj; name: string; edit: (...e: Edit[]) => void; set: Commit; onRenamed: (n: string) => void }) {
  const svc: Obj = spec.services[name] ?? {};
  const names = Object.keys(spec.services);
  const others = names.filter((n) => n !== name);
  const calls: Obj[] = Array.isArray(svc.calls) ? svc.calls : [];
  const lat: Obj = svc.latency ?? {};
  const pForm = lat.p50 !== undefined || Object.keys(lat).length === 0;
  const queue: Obj = svc.queue ?? {};
  const at: Path = ["services", name];
  const [rename, setRename] = useState(name);
  useEffect(() => setRename(name), [name]);
  const renameError = rename === name ? null : !NAME.test(rename) ? "Use lowercase letters, digits, - or _" : names.includes(rename) ? "That name is taken" : rename === "client" ? "That name is reserved" : null;

  const commitRename = () => {
    if (rename === name || renameError) return;
    edit({ op: "rename", from: name, to: rename });
    onRenamed(rename);
  };

  const removeCall = (i: number) => edit(calls.length === 1 ? { op: "delete", path: [...at, "calls"] } : { op: "delete", path: [...at, "calls", i] });

  const addCall = () => {
    const to = others.find((o) => !calls.some((c) => c.to === o)) ?? others[0];
    if (!to) return;
    set([...at, "calls"], [...calls, { to, timeout: "500ms" }]);
  };

  const removeService = () => {
    const edits: Edit[] = [];
    for (const [other, s] of Object.entries<Obj>(spec.services)) {
      if (other === name) continue;
      const cs: Obj[] = Array.isArray(s.calls) ? s.calls : [];
      const keep = cs.filter((c) => c.to !== name);
      if (keep.length === cs.length) continue;
      edits.push(keep.length ? { op: "set", path: ["services", other, "calls"], value: keep } : { op: "delete", path: ["services", other, "calls"] });
      if (s.cache?.miss_target === name) edits.push({ op: "delete", path: ["services", other, "cache"] });
    }
    if (spec.traffic?.entry === name) edits.push({ op: "delete", path: ["traffic", "entry"] });
    edits.push({ op: "delete", path: at });
    edit(...edits);
    onRenamed(others[0] ?? "");
  };

  return (
    <div className="v-service">
      <div className="v-grid">
        <div className="field">
          <label htmlFor="v-svc-name">Name</label>
          <input
            id="v-svc-name"
            className="input"
            value={rename}
            spellCheck={false}
            aria-invalid={!!renameError}
            onChange={(e) => setRename(e.target.value.toLowerCase())}
            onBlur={commitRename}
            onKeyDown={(e) => e.key === "Enter" && commitRename()}
          />
          {renameError && <span className="form-error">{renameError}</span>}
        </div>
        <Text label="Description" value={svc.description} raw wide onCommit={(v) => set([...at, "description"], v)} />
        <Text label="Instances" value={svc.instances} placeholder="1" onCommit={(v) => set([...at, "instances"], v)} />
        <Text label="Concurrency each" value={svc.concurrency} placeholder="16" onCommit={(v) => set([...at, "concurrency"], v)} />
        {pForm ? (
          <>
            <Text label="Latency p50" value={lat.p50} placeholder="5ms" onCommit={(v) => set([...at, "latency", "p50"], v)} />
            <Text label="Latency p99" value={lat.p99} placeholder="4 × p50" onCommit={(v) => set([...at, "latency", "p99"], v)} />
          </>
        ) : (
          <p className="hint v-wide">This service uses a different latency shape. Edit it in YAML.</p>
        )}
        <Text label="Error rate" value={svc.error_rate} placeholder="0%" onCommit={(v) => set([...at, "error_rate"], v)} />
        <Choice label="Load balancing" value={svc.lb ?? "round_robin"} options={LB} onCommit={(v) => set([...at, "lb"], v)} />
        <Text label="Queue size" value={queue.capacity} placeholder="1000" onCommit={(v) => set([...at, "queue", "capacity"], v)} />
        <Choice label="Queue order" value={queue.discipline ?? "fifo"} options={DISCIPLINE} onCommit={(v) => set([...at, "queue", "discipline"], v)} />
        <Text label="Queue timeout" value={queue.timeout} placeholder="none" onCommit={(v) => set([...at, "queue", "timeout"], v)} />
      </div>

      <div className="v-row">
        <Toggle
          label="Autoscaling"
          on={!!svc.autoscale}
          onChange={(on) => {
            const n = Number(svc.instances) || 1;
            set([...at, "autoscale"], on ? { min: n, max: n * 3, target_utilization: "60%" } : undefined);
          }}
        />
      </div>
      {svc.autoscale && (
        <div className="v-grid">
          <Text label="Min instances" value={svc.autoscale.min} onCommit={(v) => set([...at, "autoscale", "min"], v)} />
          <Text label="Max instances" value={svc.autoscale.max} onCommit={(v) => set([...at, "autoscale", "max"], v)} />
          <Text label="Target busy" value={svc.autoscale.target_utilization} placeholder="60%" onCommit={(v) => set([...at, "autoscale", "target_utilization"], v)} />
          <Text label="Start-up time" value={svc.autoscale.provision} placeholder="60s" onCommit={(v) => set([...at, "autoscale", "provision"], v)} />
        </div>
      )}

      <Section
        title="Calls"
        tip="Services this one calls on every request, with the timeout and retry settings it uses."
        action={
          others.length > 0 && (
            <button type="button" className="btn btn-sm" onClick={addCall}>
              <Plus size={13} /> Add call
            </button>
          )
        }
      >
        {calls.length === 0 ? (
          <p className="hint">{name} calls nothing.</p>
        ) : (
          calls.map((c, i) => <CallCard key={i} svc={name} i={i} call={c} others={others} set={set} onRemove={() => removeCall(i)} />)
        )}
      </Section>

      {names.length > 1 && (
        <div className="v-row">
          <div className="spacer" />
          <button type="button" className="btn btn-sm btn-ghost v-danger" onClick={removeService}>
            <Trash2 size={13} /> Delete {name}
          </button>
        </div>
      )}
    </div>
  );
}

export function VisualEditor({
  text,
  onChange,
  issues,
  selected,
  onSelect,
  onOpenYaml,
}: {
  text: string;
  onChange: (text: string) => void;
  issues: SpecIssue[];
  selected: string | null;
  onSelect: (name: string) => void;
  onOpenYaml: () => void;
}) {
  const { ok, spec } = useMemo(() => readSpec(text), [text]);
  const services: Obj = ok && typeof spec.services === "object" && spec.services ? spec.services : {};
  const names = Object.keys(services);

  if (!ok || names.length === 0) {
    return (
      <div className="visual">
        <p className="muted">The form needs valid YAML with at least one service.</p>
        <div>
          <button type="button" className="btn btn-sm" onClick={onOpenYaml}>
            Open YAML
          </button>
        </div>
      </div>
    );
  }

  const edit = (...edits: Edit[]) => onChange(applyEdits(text, edits));
  const set: Commit = (path, value) => edit(value === undefined ? { op: "delete", path } : { op: "set", path, value });
  const current = selected && names.includes(selected) ? selected : names[0]!;
  const traffic: Obj = spec.traffic ?? {};
  const client: Obj = traffic.client ?? {};
  const res: Obj = spec.resilience ?? {};

  const addService = () => {
    let i = names.length + 1;
    while (names.includes(`service-${i}`)) i++;
    const name = `service-${i}`;
    set(["services", name], { instances: 1, concurrency: 16, latency: { p50: "5ms", p99: "20ms" } });
    onSelect(name);
  };

  return (
    <div className="visual">
      {issues.length > 0 && (
        <ul className="v-issues" aria-live="polite">
          {issues.map((iss, k) => {
            const svc = /^services\.([^.[]+)/.exec(iss.path ?? "")?.[1];
            return (
              <li key={k}>
                {svc && names.includes(svc) ? (
                  <button type="button" className="v-link" onClick={() => onSelect(svc)}>
                    {iss.path}
                  </button>
                ) : (
                  iss.path && <b>{iss.path}</b>
                )}{" "}
                {iss.message}
              </li>
            );
          })}
        </ul>
      )}

      <Section title="Traffic" tip="Requests per second at normal load and how the callers outside the system behave.">
        <div className="v-grid">
          <Text label="Name" value={spec.name} raw wide onCommit={(v) => set(["name"], v ?? "")} />
          <Text label="Requests per second" value={traffic.rps} onCommit={(v) => set(["traffic", "rps"], v)} />
          <Choice label="Entry service" value={traffic.entry ?? names[0]} options={names} onCommit={(v) => set(["traffic", "entry"], v)} />
          <Text label="Caller timeout" value={client.timeout} placeholder="2000ms" onCommit={(v) => set(["traffic", "client", "timeout"], v)} />
        </div>
        <RetryFields at={["traffic", "client"]} retry={client.retry} budget={client.budget} set={set} />
        <div className="v-row">
          <Toggle label="Pass deadlines downstream" on={!!res.deadline_propagation} onChange={(on) => set(["resilience", "deadline_propagation"], on)} />
          <Toggle label="Cancel work on timeout" on={!!res.cancel_on_timeout} onChange={(on) => set(["resilience", "cancel_on_timeout"], on)} />
        </div>
      </Section>

      <Section
        title="Services"
        tip="Pick a service to edit it. You can also click a service in the call graph."
        action={
          <button type="button" className="btn btn-sm" onClick={addService}>
            <Plus size={13} /> Add service
          </button>
        }
      >
        <div className="v-chips" role="group" aria-label="Services">
          {names.map((n) => (
            <button key={n} type="button" className="v-chip" aria-pressed={n === current} onClick={() => onSelect(n)}>
              {n}
            </button>
          ))}
        </div>
        <ServiceForm key={current} spec={spec} name={current} edit={edit} set={set} onRenamed={onSelect} />
      </Section>

      <p className="hint">Caches, contention, traffic patterns and network settings stay in YAML.</p>
    </div>
  );
}
