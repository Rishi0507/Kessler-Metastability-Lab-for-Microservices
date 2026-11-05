import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router";
import { useAfterSignIn, useSession } from "../app/session";
import { toastError, useRegisterCommands } from "../app/stores";
import { Dialog, InfoTip, Skeleton, useTitle } from "../components/ui";
import { api } from "../lib/api";
import { fmtAgo } from "../lib/format";
import type { SystemDetail, SystemSummary, Template } from "../lib/types";

const BLANK = `name: My service
traffic:
  rps: 200
  client:
    timeout: 1s
services:
  api:
    instances: 2
    concurrency: 32
    latency: { p50: 4ms, p99: 20ms }
    calls:
      - to: db
        timeout: 300ms
        retry: { attempts: 2, base: 50ms, jitter: full }
  db:
    instances: 1
    concurrency: 16
    latency: { p50: 6ms, p99: 30ms }
`;

function TemplateGrid({ templates, onPick, busy }: { templates: Template[]; onPick: (t: Template) => void; busy: boolean }) {
  return (
    <div className="templates">
      {templates.map((t) => (
        <button key={t.id} className="template" onClick={() => onPick(t)} disabled={busy}>
          <h3>{t.name}</h3>
          <p title={t.description}>{t.description}</p>
          <div className="tags">
            {t.tags.map((tag) => (
              <span className="tag" key={tag}>
                {tag}
              </span>
            ))}
          </div>
        </button>
      ))}
    </div>
  );
}

export default function SystemsPage() {
  useTitle("Systems");
  const { data: user, isLoading: loadingUser } = useSession();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [picking, setPicking] = useState(false);
  const [removing, setRemoving] = useState<SystemSummary | null>(null);
  const started = useRef(false);

  const systems = useQuery({
    queryKey: ["systems"],
    queryFn: () => api.get<{ items: SystemSummary[] }>("/api/v1/systems?limit=50"),
    enabled: !!user,
  });
  const templates = useQuery({
    queryKey: ["templates"],
    queryFn: () => api.get<{ templates: Template[] }>("/api/v1/templates"),
    staleTime: Infinity,
  });
  const remove = useMutation({
    mutationFn: (s: SystemSummary) => api.del(`/api/v1/systems/${s.id}`),
    onSuccess: () => {
      setRemoving(null);
      void qc.invalidateQueries({ queryKey: ["systems"] });
    },
    onError: (e) => toastError((e as Error).message),
  });
  const create = useMutation({
    mutationFn: (t: { yaml: string; id?: string }) =>
      api.post<SystemDetail>("/api/v1/systems", { yaml: t.yaml, template_id: t.id }),
    onSuccess: (s) => {
      void qc.invalidateQueries({ queryKey: ["systems"] });
      navigate(`/systems/${s.id}`);
    },
    onError: (e) => toastError((e as Error).message),
  });

  useEffect(() => {
    const start = params.get("start");
    if (!start || started.current || !user || !templates.data) return;
    started.current = true;
    setParams({}, { replace: true });
    useAfterSignIn.getState().set(null);
    const t = templates.data.templates.find((x) => x.id === start);
    if (t) create.mutate({ yaml: t.yaml, id: t.id });
  }, [params, setParams, user, templates.data, create]);

  useRegisterCommands("systems", [
    { id: "new-system", group: "Systems", label: "New system from an example", run: () => setPicking(true) },
    ...(systems.data?.items ?? []).slice(0, 8).map((s) => ({
      id: `open-${s.id}`,
      group: "Systems",
      label: `Open ${s.name}`,
      run: () => navigate(`/systems/${s.id}`),
    })),
  ]);

  if (!loadingUser && !user) return <Navigate to="/" replace />;
  const items = systems.data?.items ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <div className="title-row">
          <h1 className="display">Systems</h1>
          <InfoTip>Each system describes your services and their resilience settings. Every save creates a new version.</InfoTip>
        </div>
        <div className="spacer" />
        <button className="btn btn-primary" onClick={() => setPicking(true)}>
          <Plus size={16} /> New system
        </button>
      </div>

      {systems.isLoading ? (
        <div className="stack">
          <Skeleton h={48} />
          <Skeleton h={48} />
          <Skeleton h={48} />
        </div>
      ) : items.length === 0 ? (
        <div className="stack" style={{ gap: 20 }}>
          <div className="title-row">
            <h2 className="display">Start from an example</h2>
            <InfoTip>Each example fails in a different way. Pick one to see its risks, then find how much load it takes to tip it over.</InfoTip>
          </div>
          {templates.data && (
            <TemplateGrid templates={templates.data.templates} busy={create.isPending} onPick={(t) => create.mutate({ yaml: t.yaml, id: t.id })} />
          )}
        </div>
      ) : (
        <table className="systems-table">
          <thead>
            <tr>
              <th>Grade</th>
              <th>Name</th>
              <th>Version</th>
              <th>Analyses</th>
              <th>Updated</th>
              <th><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.id} className="clickable" onClick={() => navigate(`/systems/${s.id}`)}>
                <td style={{ width: 70 }}>
                  {s.score ? <span className={`letter ${s.score.grade}`}>{s.score.grade}</span> : <span className="faint"> - </span>}
                </td>
                <td>
                  <a href={`/systems/${s.id}`} onClick={(e) => e.preventDefault()} style={{ color: "var(--ink)", fontWeight: 600, textDecoration: "none" }}>
                    {s.name}
                  </a>
                </td>
                <td className="num muted">v{s.latest_version}</td>
                <td className="num muted">{s.analyses}</td>
                <td className="muted">{fmtAgo(s.updated_at)}</td>
                <td style={{ width: 52 }}>
                  <button
                    className="btn btn-ghost icon-btn row-action"
                    aria-label={`Delete ${s.name}`}
                    title="Delete"
                    onClick={(e) => {
                      e.stopPropagation();
                      setRemoving(s);
                    }}
                  >
                    <Trash2 size={16} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <Dialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        title="Delete this system?"
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => setRemoving(null)}>
              Cancel
            </button>
            <button className="btn btn-primary btn-destructive" disabled={remove.isPending} onClick={() => removing && remove.mutate(removing)}>
              {remove.isPending ? "Deleting…" : "Delete"}
            </button>
          </>
        }
      >
        <p className="muted">{removing?.name} and its analyses will disappear from your list.</p>
      </Dialog>

      <Dialog open={picking} onClose={() => setPicking(false)} title="New system" wide>
        {templates.data ? (
          <TemplateGrid
            templates={templates.data.templates}
            busy={create.isPending}
            onPick={(t) => {
              setPicking(false);
              create.mutate({ yaml: t.yaml, id: t.id });
            }}
          />
        ) : (
          <Skeleton h={160} />
        )}
        <div>
          <button
            className="btn"
            disabled={create.isPending}
            onClick={() => {
              setPicking(false);
              create.mutate({ yaml: BLANK });
            }}
          >
            Start blank
          </button>
        </div>
      </Dialog>
    </div>
  );
}
