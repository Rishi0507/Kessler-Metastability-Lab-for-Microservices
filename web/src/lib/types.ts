export interface User {
  id: string;
  handle: string;
  email: string | null;
  is_guest: boolean;
  created_at: string;
}

export type Severity = "critical" | "high" | "medium" | "low" | "info";

export interface PatchOp {
  op: "set";
  path: (string | number)[];
  value: unknown;
}

export interface Finding {
  id: string;
  rule: string;
  severity: Severity;
  title: string;
  detail: string;
  location: string;
  locations?: string[];
  evidence: Record<string, unknown>;
  fix: string | null;
  patch: PatchOp[];
}

export interface TopologyNode {
  id: string;
  kind: "client" | "service" | "cache";
  label: string;
  description?: string | null;
  instances?: number;
  concurrency?: number;
  utilization?: number;
  capacity_rps?: number;
  arrival_rps?: number;
  hold_ms?: number;
  rps?: number;
}

export interface TopologyEdge {
  from: string;
  to: string;
  probability: number;
  timeout_ms: number;
  attempts: number;
  jitter: string;
  budget: boolean;
  breaker: boolean;
}

export interface Topology {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  entry: string;
  bottleneck: string | null;
}

export interface SpecIssue {
  path: string;
  message: string;
  line: number | null;
}

export interface CheckResult {
  ok: boolean;
  errors: SpecIssue[];
  name?: string;
  description?: string | null;
  spec_hash?: string;
  analysis?: {
    max_rps: number;
    max_load_multiplier: number;
    bottleneck: string;
    worst_amplification: number;
    offered_rps: number;
    services: { name: string; utilization: number; capacity_rps: number; visits: number }[];
  };
  findings?: Finding[];
  score?: { score: number; grade: string };
  topology?: Topology;
}

export interface Template {
  id: string;
  name: string;
  description: string;
  tags: string[];
  trigger: Record<string, unknown>;
  yaml: string;
}

export interface SystemSummary {
  id: string;
  name: string;
  latest_version: number;
  created_at: string;
  updated_at: string;
  score: { score: number; grade: string } | null;
  analyses: number;
}

export interface SystemDetail {
  id: string;
  name: string;
  version: number;
  latest_version: number;
  created_at: string;
  updated_at: string;
  yaml: string;
  spec_hash: string;
  checks: {
    analysis?: CheckResult["analysis"];
    findings?: Finding[];
    score?: { score: number; grade: string };
    topology?: Topology;
    experiment_of?: { analysis: string; version: number };
  };
}

export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";

export interface SweepParams {
  trigger: { kind: string; target?: string; duration_s?: number; magnitude?: number };
  loads: number[];
  values: number[];
  axis2: "magnitude" | "duration";
  seeds: number;
  mode: "adaptive" | "grid";
  warmup_s: number;
  trigger_start_s: number;
  observe_s: number;
}

export type Verdict = "stable" | "recovered" | "metastable" | "overloaded" | "failed";

export interface SimSummary {
  verdict: Verdict;
  recovery_s: number | null;
  peak_amplification: number | null;
  wasted_fraction: number | null;
  min_goodput_ratio: number | null;
  lost_requests: number | null;
  events?: number | null;
  wall_ms?: number | null;
  truncated?: boolean | null;
}

export interface CellAggregate {
  seeds: number;
  p_metastable: number;
  p_overloaded: number;
  bad: boolean;
  recovery_s: number | null;
  peak_amplification: number;
  wasted_fraction: number;
  min_goodput_ratio: number;
  lost_requests: number;
  failed?: boolean;
}

export interface CellView {
  cell: number;
  row: number;
  col: number;
  load: number;
  value: number;
  seeds_done: number;
  seeds_total: number;
  cached: number;
  aggregate?: CellAggregate;
  summaries?: (SimSummary & { seed: number })[];
}

export interface AnalysisFindingOut {
  rule: string;
  severity: Severity;
  title: string;
  detail: string;
  evidence: Record<string, unknown>;
}

export type InferredState = "ok" | "metastable" | "overloaded";

export interface AnalysisResult {
  safe_cells: number;
  total_cells: number;
  safe_fraction: number;
  boundary: { value: number; tipping_load: number | null }[];
  findings: AnalysisFindingOut[];
  savings: { full_grid_sims: number; simulated_sims: number; avoided_fraction: number };
  verdicts: { bad: boolean; inferred: boolean; state?: InferredState }[];
}

export interface Analysis {
  id: string;
  system_id: string;
  system_version: number;
  status: JobStatus;
  parent_id: string | null;
  cells_total: number;
  cells_done: number;
  sims_total: number;
  sims_done: number;
  sims_cached: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  first_result_at: string | null;
  time_to_first_insight_s?: number;
  result: AnalysisResult | null;
  params: SweepParams;
  system_name?: string;
}

export interface StreamInfo {
  topic: string;
  token: string;
  last_id: string;
}

export interface AnalysisSnapshot {
  analysis: Analysis;
  params: SweepParams;
  cells: CellView[];
  system: { name: string; version: number; spec_yaml: string; checks: SystemDetail["checks"] } | null;
  stream: StreamInfo;
}

export interface FrameService {
  name: string;
  in: number;
  ok: number;
  err: number;
  shed: number;
  dropped: number;
  timeouts: number;
  retries: number;
  wasted: number;
  util: number;
  queue: number;
  queue_max: number;
  p50: number;
  p99: number;
  instances: number;
  pending: number;
  hit_ratio?: number;
  latency_mult: number;
}

export interface FrameEdge {
  from: string;
  to: string;
  rps: number;
  err: number;
  retries: number;
  timeouts: number;
  cb_rejects: number;
  budget_denied: number;
  breaker: string;
}

export interface Frame {
  t: number;
  rate: number;
  client: { offered: number; ok: number; err: number; good: number; retries: number; p50: number; p99: number };
  amp: number;
  wasted: number;
  services: FrameService[];
  edges: FrameEdge[];
  faults: { kind: string; target?: string; magnitude: number }[];
}

export interface Run {
  id: string;
  system_id: string;
  system_version: number;
  status: JobStatus;
  params: { load: number; speed: number; horizon_s: number; seed: number };
  sim_time: number;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
}

export interface RunSnapshot {
  run: Run;
  commands: { seq: number; sim_time: number; command: Record<string, unknown>; created_at: string }[];
  topology: Topology | null;
  stream: StreamInfo;
}

export interface StreamMessage<E = Record<string, unknown>> {
  topic: string;
  id: string;
  event: E & { type: string; ts?: number };
}
