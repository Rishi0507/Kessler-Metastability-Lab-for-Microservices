import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router";
import { useAfterSignIn, useSession } from "../app/session";
import { toastError, useRegisterCommands } from "../app/stores";
import { Dialog, Skeleton } from "../components/ui";
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
          <p>{t.description}</p>
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
  const { data: user, isLoading: loadingUser } = useSession();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [picking, setPicking] = useState(false);
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
        <div>
          <h1 className="display">Systems</h1>
          <p className="sub">Each system is a versioned description of your services and their resilience settings.</p>
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
          <div className="empty">
            <h2 className="display">Start from an example</h2>
            <p className="muted" style={{ maxWidth: "62ch" }}>
              Each example fails in a different way. Pick one to see its risks, then find out how much load it takes to
              tip it over.
            </p>
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
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <Dialog open={picking} onClose={() => setPicking(false)} title="New system" wide>
        <p className="muted">Start from an example, or from a minimal two-service system.</p>
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
            Start from a minimal system
          </button>
        </div>
      </Dialog>
    </div>
  );
}
