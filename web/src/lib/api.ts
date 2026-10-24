import { backoffMs, RetryBudget, sleep } from "./retry";

export interface ProblemField {
  path: string;
  message: string;
  line?: number | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly detail?: string,
    readonly errors?: ProblemField[],
    readonly extra: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

const budget = new RetryBudget();
let minLsn: string | null = null;
let refreshing: Promise<boolean> | null = null;

function lsnValue(lsn: string): bigint {
  const [hi = "0", lo = "0"] = lsn.split("/");
  return (BigInt(`0x${hi}`) << 32n) | BigInt(`0x${lo}`);
}

function noteLsn(lsn: string | null): void {
  if (!lsn) return;
  if (!minLsn || lsnValue(lsn) > lsnValue(minLsn)) minLsn = lsn;
}

function csrfToken(): string | null {
  const m = document.cookie.match(/(?:^|;\s*)ks_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1] ?? "") : null;
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

interface Options {
  body?: unknown;
  idempotencyKey?: string;
  ifMatch?: string;
  signal?: AbortSignal;
  noRefresh?: boolean;
}

async function refreshSession(): Promise<boolean> {
  if (!refreshing) {
    refreshing = fetch("/api/v1/auth/refresh", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken() ?? "" },
    })
      .then((r) => r.ok)
      .catch(() => false)
      .finally(() => {
        setTimeout(() => (refreshing = null), 0);
      });
  }
  return refreshing;
}

async function toError(res: Response): Promise<ApiError> {
  let body: Record<string, unknown> = {};
  try {
    body = await res.json();
  } catch {
  }
  const { title, code, detail, errors, ...extra } = body as {
    title?: string;
    code?: string;
    detail?: string;
    errors?: ProblemField[];
  };
  return new ApiError(
    res.status,
    code ?? `http_${res.status}`,
    title ?? res.statusText ?? "Request failed",
    detail,
    errors,
    extra,
  );
}

export async function request<T>(method: string, path: string, opts: Options = {}): Promise<{ data: T; headers: Headers }> {
  const safe = method === "GET" || method === "HEAD";
  const retryable = safe || !!opts.idempotencyKey;
  budget.deposit();
  for (let attempt = 0; ; attempt++) {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (!safe) headers["X-CSRF-Token"] = csrfToken() ?? "";
    if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
    if (opts.ifMatch) headers["If-Match"] = opts.ifMatch;
    if (safe && minLsn) headers["X-Kessler-Min-LSN"] = minLsn;

    let res: Response;
    try {
      res = await fetch(path, {
        method,
        headers,
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
        credentials: "same-origin",
        signal: opts.signal,
      });
    } catch (err) {
      if ((err as Error).name === "AbortError") throw err;
      if (retryable && attempt < 3 && budget.withdraw()) {
        await sleep(backoffMs(attempt), opts.signal);
        continue;
      }
      throw new ApiError(0, "network", "Can't reach Kessler. Check your connection and try again.");
    }

    noteLsn(res.headers.get("x-kessler-lsn"));

    if (res.status === 401 && !opts.noRefresh && path !== "/api/v1/auth/refresh") {
      if (await refreshSession()) {
        opts = { ...opts, noRefresh: true };
        continue;
      }
    }
    if ((res.status === 429 || res.status === 502 || res.status === 503 || res.status === 504) && retryable && attempt < 3) {
      const after = Number(res.headers.get("retry-after"));
      const wait = Number.isFinite(after) && after > 0 ? Math.min(after * 1000, 30_000) : backoffMs(attempt);
      if (budget.withdraw()) {
        await sleep(wait, opts.signal);
        continue;
      }
    }
    if (res.status === 304) return { data: undefined as T, headers: res.headers };
    if (!res.ok) throw await toError(res);
    if (res.status === 204) return { data: undefined as T, headers: res.headers };
    return { data: (await res.json()) as T, headers: res.headers };
  }
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>("GET", path, { signal }).then((r) => r.data),
  post: <T>(path: string, body?: unknown, idempotencyKey?: string) =>
    request<T>("POST", path, { body, idempotencyKey }).then((r) => r.data),
  put: <T>(path: string, body: unknown, ifMatch: string) => request<T>("PUT", path, { body, ifMatch }),
  del: (path: string) => request<void>("DELETE", path).then(() => undefined),
};
