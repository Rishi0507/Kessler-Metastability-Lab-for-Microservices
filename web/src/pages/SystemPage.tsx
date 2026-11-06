import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Play, Save, Upload, Wrench } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router";
import { useSession } from "../app/session";
import { toast, toastError, useRegisterCommands } from "../app/stores";
import { SpecEditor, type SpecEditorHandle } from "../components/SpecEditor";
import { Topology2D } from "../components/Topology2D";
import { VisualEditor } from "../components/VisualEditor";
import { TriggerDialog, type TriggerChoice } from "../components/TriggerDialog";
import { Grade, InfoTip, Segmented, SeverityDot, Skeleton, StatusDot, useTitle } from "../components/ui";
import { api, ApiError, newIdempotencyKey, request } from "../lib/api";
import { fmtAgo, fmtPct } from "../lib/format";
import type { Analysis, CheckResult, Finding, SystemDetail } from "../lib/types";

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

type EditorMode = "visual" | "yaml";
const MODE_KEY = "kessler.editor";

function loadMode(): EditorMode {
  try {
    return localStorage.getItem(MODE_KEY) === "yaml" ? "yaml" : "visual";
  } catch {
    return "visual";
  }
}

function FindingsList({ findings, onApply, busy }: { findings: Finding[]; onApply: (f: Finding) => void; busy: boolean }) {
  if (findings.length === 0) {
    return <p className="muted">No risks found in the spec.</p>;
  }
  return (
    <ul className="rows">
      {findings.map((f) => (
        <li key={f.id} className="row-item">
          <SeverityDot severity={f.severity} />
          <span className="row-title">{f.title}</span>
          <InfoTip label="Why this is a risk">
            <p>{f.detail}</p>
            {f.fix && <p className="tip-fix">Fix: {f.fix}</p>}
          </InfoTip>
          {f.patch.length > 0 && (
            <button className="btn btn-sm" disabled={busy} onClick={() => onApply(f)} title={f.fix ?? "Apply fix"}>
              <Wrench size={13} /> Fix
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

export default function SystemPage() {
  const { id = "" } = useParams();
  const { data: user, isLoading: loadingUser } = useSession();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const editor = useRef<SpecEditorHandle>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [triggerOpen, setTriggerOpen] = useState(false);
  const [mode, setModeState] = useState<EditorMode>(loadMode);
  const [selected, setSelected] = useState<string | null>(null);
  const setMode = useCallback((m: EditorMode) => {
    setModeState(m);
    try {
      localStorage.setItem(MODE_KEY, m);
    } catch {
    }
  }, []);

  const system = useQuery({
    queryKey: ["system", id],
    queryFn: () => api.get<SystemDetail>(`/api/v1/systems/${id}`),
    enabled: !!user,
  });
  useTitle(system.data?.name);
  const analyses = useQuery({
    queryKey: ["analyses", id],
    queryFn: () => api.get<{ items: Analysis[] }>(`/api/v1/analyses?system_id=${id}&limit=10`),
    enabled: !!user,
  });

  const saved = system.data?.yaml ?? "";
  const current = draft ?? saved;
  const dirty = draft !== null && draft !== saved;
  const debounced = useDebounced(current, 350);
  const check = useQuery({
    queryKey: ["check", debounced],
    queryFn: () => api.post<CheckResult>("/api/v1/specs/check", { yaml: debounced }),
    enabled: !!user && debounced.length > 0,
    placeholderData: (prev) => prev,
    staleTime: Infinity,
  });
  const live: CheckResult | undefined = check.data;
  const findings = (live?.ok ? live.findings : system.data?.checks.findings) ?? [];
  const score = (live?.ok ? live.score : system.data?.checks.score) ?? null;
  const topo = (live?.ok ? live.topology : system.data?.checks.topology) ?? undefined;
  const issues = live && !live.ok ? live.errors : [];

  const save = useMutation({
    mutationFn: async () => {
      const r = await request<SystemDetail>("PUT", `/api/v1/systems/${id}`, {
        body: { yaml: current },
        ifMatch: `"v${system.data?.latest_version}"`,
      });
      return r.data;
    },
    onSuccess: (s) => {
      qc.setQueryData(["system", id], s);
      setDraft(null);
      void qc.invalidateQueries({ queryKey: ["systems"] });
      toast(`Saved as version ${s.version}`);
    },
    onError: (e) => {
      const err = e as ApiError;
      if (err.status === 412) toastError(`${err.message}. ${err.detail ?? ""}`);
      else if (err.status === 422) toastError("Fix the errors in the spec before saving.");
      else toastError(err.message);
    },
  });

  const applyFix = useMutation({
    mutationFn: (f: Finding) => api.post<{ yaml: string }>("/api/v1/specs/apply", { yaml: current, ops: f.patch }),
    onSuccess: (r, f) => {
      editor.current?.setValue(r.yaml);
      setDraft(r.yaml);
      toast(`Applied: ${(f.fix ?? f.title).replace(/\.$/, "")}. Save to keep it.`);
    },
    onError: (e) => toastError((e as Error).message),
  });

  const meshFile = useRef<HTMLInputElement>(null);
  const importMesh = useMutation({
    mutationFn: async (file: File) =>
      api.post<{ yaml: string; notes: string[] }>("/api/v1/specs/import-istio", { yaml: current, config: await file.text() }),
    onSuccess: (r) => {
      editor.current?.setValue(r.yaml);
      setDraft(r.yaml);
      toast(["Imported Istio settings. Save to keep them.", ...r.notes.slice(0, 3)].join(" "));
    },
    onError: (e) => toastError((e as ApiError).detail ?? (e as Error).message),
  });

  const startAnalysis = useMutation({
    mutationFn: (c: TriggerChoice) =>
      api.post<{ analysis: Analysis }>(
        "/api/v1/analyses",
        { system_id: id, trigger: c.trigger, seeds: c.seeds, mode: c.mode },
        newIdempotencyKey(),
      ),
    onSuccess: (r) => {
      setTriggerOpen(false);
      navigate(`/analyses/${r.analysis.id}`);
    },
    onError: (e) => toastError((e as ApiError).detail ?? (e as Error).message),
  });

  const startRun = useMutation({
    mutationFn: () => api.post<{ run: { id: string } }>("/api/v1/runs", { system_id: id, load: 1, speed: 4 }, newIdempotencyKey()),
    onSuccess: (r) => navigate(`/runs/${r.run.id}`),
    onError: (e) => toastError((e as ApiError).detail ?? (e as Error).message),
  });

  const onRunSweep = useCallback(() => {
    if (dirty) {
      toastError("Save your changes first: analyses run on a saved version.");
      return;
    }
    setTriggerOpen(true);
  }, [dirty]);

  useRegisterCommands("system", [
    { id: "sweep", group: "This system", label: "Find tipping points", hint: "run a sweep", run: onRunSweep },
    { id: "live", group: "This system", label: "Watch it live", hint: "start a live run", run: () => startRun.mutate() },
    { id: "save", group: "This system", label: "Save changes", run: () => save.mutate() },
    ...findings
      .filter((f) => f.patch.length)
      .slice(0, 6)
      .map((f) => ({ id: `fix-${f.id}`, group: "Fixes", label: `Apply: ${f.fix ?? f.title}`, run: () => applyFix.mutate(f) })),
  ]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (dirty) save.mutate();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dirty, save]);

  const capacity = live?.ok ? live.analysis : system.data?.checks.analysis;

  if (!loadingUser && !user) return <Navigate to="/" replace />;
  if (system.isError) {
    return (
      <div className="page">
        <div className="empty">
          <h1 className="display">System not found</h1>
          <Link className="btn" to="/systems">
            Back to systems
          </Link>
        </div>
      </div>
    );
  }
  const s = system.data;

  return (
    <div className="page">
      <div className="crumbs">
        <Link to="/systems">Systems</Link>
        <span>/</span>
        <span>{s?.name ?? "…"}</span>
      </div>
      <div className="page-head">
        <div className="stack" style={{ gap: 14 }}>
          <h1 className="display">{s?.name ?? <Skeleton h={34} w={300} />}</h1>
          {capacity && (
            <div className="stats">
              <div>
                <b className="num">{Math.round(capacity.max_rps).toLocaleString()} rps</b>
                <span>capacity</span>
              </div>
              <div>
                <b className="num">{capacity.max_load_multiplier.toFixed(1)}×</b>
                <span>headroom</span>
              </div>
              <div>
                <b>{capacity.bottleneck}</b>
                <span>bottleneck</span>
              </div>
              <InfoTip label="About these numbers">
                Estimated from the spec without simulating: the system saturates at about {Math.round(capacity.max_rps)} rps,{" "}
                {capacity.max_load_multiplier.toFixed(1)}× today's {Math.round(capacity.offered_rps)} rps.
              </InfoTip>
            </div>
          )}
        </div>
        <div className="spacer" />
        <button className="btn" onClick={() => startRun.mutate()} disabled={startRun.isPending || !s}>
          <Activity size={16} /> Watch live
        </button>
        <button className="btn btn-primary" onClick={onRunSweep} disabled={!s || startAnalysis.isPending}>
          <Play size={16} /> Find tipping points
        </button>
      </div>

      <div className="workspace">
        <div className="editor-frame">
          <div className="editor-bar">
            <Segmented
              label="Editor"
              value={mode}
              onChange={setMode}
              options={[
                { value: "visual", label: "Form" },
                { value: "yaml", label: "YAML" },
              ]}
            />
            <span className="faint">v{s?.latest_version ?? " - "}</span>
            {dirty && <span style={{ color: "var(--slow)" }}>unsaved</span>}
            {issues.length > 0 && <span style={{ color: "var(--meta)" }}>{issues.length} error{issues.length > 1 ? "s" : ""}</span>}
            <div className="spacer" />
            <input
              ref={meshFile}
              type="file"
              accept=".yaml,.yml,.json"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                e.target.value = "";
                if (f) importMesh.mutate(f);
              }}
            />
            <button
              className="btn btn-sm btn-ghost"
              disabled={!s || issues.length > 0 || importMesh.isPending}
              onClick={() => meshFile.current?.click()}
              title="Read timeouts, retries, outlier detection and load balancing from Istio VirtualService and DestinationRule files"
            >
              <Upload size={14} /> Istio
            </button>
            <button className="btn btn-sm" disabled={!dirty || save.isPending || issues.length > 0} onClick={() => save.mutate()}>
              <Save size={14} /> Save
            </button>
          </div>
          {!s ? (
            <div style={{ padding: 16 }}>
              <Skeleton h={480} />
            </div>
          ) : mode === "visual" ? (
            <VisualEditor
              text={current}
              onChange={setDraft}
              issues={issues}
              selected={selected}
              onSelect={setSelected}
              onOpenYaml={() => setMode("yaml")}
            />
          ) : (
            <SpecEditor ref={editor} value={current} onChange={setDraft} issues={issues} label="System spec (YAML)" />
          )}
        </div>

        <div className="insight">
          <div className="panel panel-pad grade-card">
            {score ? <Grade score={score.score} grade={score.grade} /> : <Skeleton h={64} w={64} />}
            <div className="stack" style={{ gap: 2 }}>
              <div className="title-row">
                <h2 className="section-title">Resilience grade</h2>
                <InfoTip>Kessler grades the spec by reading it, before running any simulation. Each risk below lowers the grade.</InfoTip>
              </div>
              <p className="muted">
                {findings.length === 0
                  ? "No risks found"
                  : `${findings.filter((f) => f.severity === "critical" || f.severity === "high").length} serious, ${
                      findings.filter((f) => f.severity === "medium").length
                    } moderate`}
              </p>
            </div>
          </div>
          {topo && (
            <div className="panel panel-pad">
              <div className="title-row">
                <h2 className="section-title">Call graph</h2>
                <InfoTip>Ring colour shows how busy each service is at today's traffic. Dashed red lines are risky calls.</InfoTip>
              </div>
              <Topology2D
                topo={topo}
                findings={findings}
                selected={mode === "visual" ? selected : null}
                onSelect={(n) => {
                  setSelected(n);
                  setMode("visual");
                }}
              />
            </div>
          )}
          <div className="panel panel-pad">
            <h2 className="section-title">Risks</h2>
            <FindingsList findings={findings} onApply={(f) => applyFix.mutate(f)} busy={applyFix.isPending} />
          </div>
          <div className="panel panel-pad">
            <div className="title-row">
              <h2 className="section-title">Analyses</h2>
              <InfoTip>An analysis simulates this system across load levels and fault sizes, and maps where it stops recovering.</InfoTip>
            </div>
            {(analyses.data?.items ?? []).length === 0 ? (
              <p className="muted">None yet</p>
            ) : (
              <ul className="rows">
                {analyses.data!.items.map((a) => (
                  <li key={a.id}>
                    <Link to={`/analyses/${a.id}`} className="row-item row-link">
                      <StatusDot status={a.status} />
                      <span className="row-title">
                        {a.parent_id ? "With fixes: " : ""}
                        {a.params.trigger.kind.replace("_", " ")}
                        {a.params.trigger.target ? ` on ${a.params.trigger.target}` : ""}
                      </span>
                      <span className="muted num">
                        {a.status === "done" && a.result ? `${fmtPct(a.result.safe_fraction)} safe` : a.status}
                      </span>
                      <span className="faint">{fmtAgo(a.created_at)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>

      <TriggerDialog
        open={triggerOpen}
        onClose={() => setTriggerOpen(false)}
        topo={topo}
        maxLoad={(live?.ok ? live.analysis?.max_load_multiplier : system.data?.checks.analysis?.max_load_multiplier) ?? 1}
        busy={startAnalysis.isPending}
        onSubmit={(c) => startAnalysis.mutate(c)}
      />
    </div>
  );
}
