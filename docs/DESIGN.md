# Kessler System Design

> **Kessler finds the tipping points that send your services into a failure that outlives its trigger.**
>
> *Kessler syndrome:* a cascade of orbital collisions that keeps producing more collisions with no new launches.
> A *metastable failure* is the distributed-systems equivalent: a short trigger (a 20 s database slowdown, a cache
> flush, a traffic spike) tips a system into a degraded state that **keeps itself alive after the trigger is gone**,
> through retries, wasted work and cold caches.

---

## 1. Problem

In most large outages, the *response* to a fault does more damage than the fault itself:

* **Retry amplification.** Three retries at each of four layers can turn one user click into 3⁴ = 81 backend calls,
  while the backend runs at its slowest.
* **Wasted work.** A caller times out and moves on, but the callee keeps working on a request nobody is waiting for.
  Effective capacity collapses while utilisation reads 100 %.
* **Cold-cache collapse.** A cache flush sends every read to the database. The overloaded database fails the reads that
  would have re-warmed the cache, so the cache never warms.
* **Autoscaling lag.** New capacity arrives after the queues have already filled with doomed requests.

Teams pick timeout, retry, backoff, budget, breaker and pool values by intuition. The research community has
characterised metastability (Bronson et al. HotOS '21, Huang et al. OSDI '22, Isaacs et al. / AWS 2025,
arXiv 2510.03551), and those papers note that "exhaustive simulation over the parameter space is too costly".
No developer tool lets a team point at **their own configuration** and get answers to these questions:

1. At what load does a trigger of a given size tip us into a self-sustaining failure?
2. Which mechanism sustains it: retries, wasted work, or the cache?
3. Does this proposed fix (retry budget, deadline propagation, jitter, LIFO queues, breaker) widen the safe region, and by how much?
4. Did this pull request make us less resilient?

Kessler makes the large parameter sweep cheap: a fast deterministic C++ engine, distributed workers, an
adaptive boundary search, and a content-addressed result cache. It then presents the answer as a single picture,
the **metastability map**.

## 2. Users and core concepts

| User | Job to be done |
|---|---|
| Backend / SRE engineer | Validate a service mesh's resilience config before an incident does it for them |
| Tech lead in design review | Compare two designs ("retries at the edge only" vs "everywhere") quantitatively |
| CI pipeline | Fail a PR that shrinks the stable operating region (resilience regression test) |
| Learner / interview prep | Build intuition for retry storms, tail latency and cascading failure with a live, interactive lab |

* **System spec** (`kessler.yaml`): services, instances, concurrency, queues, service-time distributions, call graph,
  per-edge timeouts, retries, backoff and jitter, retry budgets, circuit breakers, deadline propagation, caches,
  contention, autoscaling and traffic. Every version is immutable and content-addressed.
* **Run**: one simulation of a spec under a scenario. *Live runs* stream telemetry at 4 Hz and accept chaos
  commands ("slow the DB 5× now", "scale checkout to 8") mid-flight.
* **Analysis**: a sweep of load × trigger-magnitude, with several seeds per cell. It produces the **metastability
  map**: the probability of a self-sustaining failure and the recovery time for every cell, plus the **tipping
  boundary**.
* **Finding**: a risk with severity, evidence and a recommended fix. Findings come from static lint (instant) or from dynamic analysis.
* **Fix experiment**: apply a suggested patch, rerun the same sweep, and diff the maps ("+38 % stable region").
* **Report**: an immutable, shareable snapshot. Because the engine is deterministic, it is cacheable forever.

## 3. Requirements

**Functional:** auth (accounts plus one-click guest), spec editor with live validation and lint, templates, live runs with
chaos console, analyses with streaming progress, fix experiments, shareable reports, CI API keys and CLI, platform
status page.

**Non-functional (SLOs):**

| SLI | SLO |
|---|---|
| API availability (non-5xx / total, 30-day window) | ≥ 99.9 % |
| API latency, reads / writes (p99) | < 300 ms / < 500 ms |
| Time-to-first-insight (analysis submit → first cell rendered) | p95 < 3 s |
| Live tick fan-out (worker publish → client receive) | p99 < 150 ms |
| Default analysis completion (2 workers) | p95 < 90 s |
| Lost analysis work on a worker crash | 0 cells lost (at-least-once + idempotent writes) |
| Live-run failover after a worker crash | < 5 s, bit-identical continuation |

**Out of scope (v1):** importing topologies from OpenTelemetry traces (future work, §18), SSO, billing.

## 4. Back-of-the-envelope estimates (target: 10 k users, 1 k DAU, 100 concurrent at peak)

* **Sim cost.** About 1,000 RPS × 180 sim-seconds × about 25 events per request comes to about 4.5 M events per simulation. At
  roughly 15 M events/s/core that is about 0.3 core-s per sim.
* **Analysis cost.** The full grid is 6 × 10 cells × 3 seeds = 180 sims. The adaptive search needs about 90 sims, or about
  27 core-s.
* **Peak compute.** 100 users × 1 analysis per 5 min = 0.33 analyses/s, which needs about 9 cores. Assuming a 30 % cache hit
  rate, that drops to about 6 cores, so 2-3 worker nodes.
* **Live runs.** At 4× speed a run needs about 100 k events/s, under 1 % of a core. Each viewer receives 4 Hz × 3 KB = 12 KB/s;
  1,000 viewers need about 12 MB/s of egress.
* **Storage.** Summary rows are about 1 KB. Compressed series (float32 + zlib) are about 6 KB and are stored only for boundary
  cells. That is about 150 KB per analysis, about 150 MB/day and about 4.5 GB/month, with an LRU TTL that evicts
  unreferenced results after 30 days.
* **Postgres.** About 100 read QPS and 30 write QPS at peak. A single primary is plenty; the replica exists for read scaling and HA.
* **Redis.** About 2 k ops/s, which is negligible. WebSockets peak around 1 k concurrent; at about 10 k per gateway node,
  2 nodes are for HA rather than capacity.

## 5. Architecture

```
                         Browser (React + TS + three.js)
                     HTTPS /api/*            WSS /ws
                           │                    │
                 ┌─────────▼────────────────────▼─────────┐
                 │  L4 load balancer  (nginx stream, TCP)  │   prod: cloud NLB across AZs
                 └─────────┬────────────────────┬─────────┘
                 ┌─────────▼────────┐  ┌────────▼─────────┐
                 │ nginx edge #1 L7 │  │ nginx edge #2 L7 │   static SPA, routing, rate limits,
                 └──┬────────────┬──┘  └──┬────────────┬──┘   micro-cache, WS upgrade, req-ids
          least_conn│            │ws      │            │
          ┌─────────▼───┐   ┌────▼────────▼┐
          │  api  × 3   │   │ gateway × 2  │  FastAPI (stateless) │ asyncio WS fan-out
          └──┬───────┬──┘   └──────┬───────┘
             │       │  pub/sub +  │ streams
   ┌─────────▼──┐  ┌─▼────────────▼──────────┐      ┌─────────────────────────┐
   │ PgBouncer  │  │ Redis 8                  │◄────►│ scheduler × 2 (1 leader)│ outbox relay, DRR fair
   └─────┬──────┘  │ streams · pub/sub · cache│      │ advisory-lock election  │ share, adaptive search
   ┌─────▼──────┐  │ rate limits · leases     │      └─────────────────────────┘
   │ PG primary │  └─▲───────────────────────┘      ┌─────────────────────────┐
   │  ──WAL──►  │    └─────────────────────────────►│ sim-worker × N          │ Python orchestrator +
   │ PG replica │◄────────────────────────────────── │ C++ engine via C ABI    │ C++ DES (GIL released)
   └────────────┘                                    └─────────────────────────┘
   Observability: Prometheus ◄─ /metrics everywhere · Grafana dashboards · OpenTelemetry → Jaeger
```

| Component | Responsibility | Scaling unit | State |
|---|---|---|---|
| L4 LB (nginx `stream`) | TCP balancing across edges; removes the edge as a SPOF | 1 (cloud NLB in prod) | none |
| Edge (nginx L7) | TLS, SPA assets, `/api` → api pool, `/ws` → gateway pool, `limit_req`/`limit_conn`, micro-cache, immutable report cache, security headers, `X-Request-ID` | 2+ | none |
| api (FastAPI) | Auth, CRUD, validation and lint, job submission (outbox), reports, WS tickets, KPIs | 3+ (CPU) | none |
| gateway (asyncio WS) | Ticket auth, capability-token topic authz, Redis → client fan-out, conflation, resume, live controls | 2+ (connections) | in-memory subscriptions only |
| scheduler | Outbox relay, task expansion, adaptive bisection, DRR fair-share dispatch, lease watchdog, finaliser | 2 (active/standby) | leader lease |
| sim-worker | Executes sims via C++ engine on a thread pool; live-run loop with lease + fencing | N (cores) | lease per live run |
| Postgres 18 | Source of truth; primary + streaming replica | vertical + replicas | durable |
| PgBouncer | Transaction pooling; caps server connections | 1-2 | none |
| Redis 8 | Streams (tasks, results, progress), pub/sub (ticks), cache, rate limit, tickets, leases | 1 (+replica/sentinel in prod) | ephemeral |

## 6. Key request flows

### 6.1 Submit an analysis (durable, exactly-once effects)
1. `POST /api/v1/analyses` with an `Idempotency-Key` header. In **one transaction**, the api inserts the `analyses` row,
   an `outbox` row and the idempotency record, then returns `202 {id, stream_token}`.
2. The scheduler leader's **outbox relay** claims unpublished rows (`FOR UPDATE SKIP LOCKED`), expands the analysis into
   cells, and pushes tasks onto the owner's **tenant queue**. A crash between steps leaves the outbox row
   unpublished, so the relay retries it and loses nothing.
3. Each task is keyed by `sha256(spec_hash ‖ load ‖ magnitude ‖ seed ‖ engine_version)`. A **cache hit** in
   `sim_results` completes the task immediately.
4. The **DRR dispatcher** moves tasks from tenant queues into `stream:tasks` only while
   `in_flight < worker_slots × 1.5`. Fairness and backpressure both depend on this bounded prefetch.
5. Workers `XREADGROUP`, run the engine with the GIL released, upsert `sim_results`, transition the cell
   **idempotently** (`UPDATE … WHERE status <> 'done' RETURNING`), and `XADD` a progress event. Duplicates
   (redelivery after a crash) are no-ops.
6. The scheduler consumes results. In adaptive mode it **bisects each magnitude row** toward the tipping boundary and
   enqueues only the cells that matter. When all cells are finished it computes the boundary and findings and marks the analysis `done`.
7. The gateway forwards progress events. Clients resume after a disconnect by sending the last stream ID
   (*subscribe-then-backfill*, de-duplicated by ID), so they see no gaps and no duplicates.

### 6.2 Live run with chaos controls (interactive, failover by replay)
1. `POST /api/v1/runs` creates the run and an outbox row. The scheduler publishes it to `stream:live`.
2. A worker claims it and acquires `lease:run:{id}` with an incrementing **fencing token**. It then steps the engine
   every 250 ms, publishes ticks to `run:{id}` (pub/sub, conflated), and keeps a capped tick stream for late joiners.
3. Chaos commands travel client → gateway (WS) → `stream:ctl:{id}` → the worker applies them at the next tick boundary and
   appends them to `run_commands`, an event-sourced log with sim-timestamps.
4. If the worker dies, its lease expires and another worker claims the run with token + 1. Because the engine is
   deterministic, the new worker replays `seed + run_commands` up to the last published sim-time (much faster than
   real time) and carries on with the same numbers. The gateway drops frames that carry a lower fencing token.

### 6.3 WebSocket authentication
Browsers can't set headers on a WebSocket. `POST /api/v1/ws-tickets` (cookie auth plus CSRF) returns a 30 s,
single-use ticket stored in Redis and consumed with `GETDEL`. Per-topic authorisation uses **capability tokens**:
`HMAC(topic ‖ user ‖ exp)`, returned by the HTTP resource endpoints. That lets the gateway authorise subscriptions
**without a database round-trip**.

## 7. Transport decisions: HTTP, WebSocket or SSE

| Interaction | Transport | Why |
|---|---|---|
| Auth, specs CRUD, templates | HTTPS REST | Request/response, cacheable, standard semantics, ETag / If-Match |
| Validate-as-you-type | HTTPS POST, debounced 300 ms, keyed by content hash | Pure function; cache by hash; no server state |
| Submit analysis / start run | HTTPS POST + `Idempotency-Key` → `202` | Durable state change; must be exactly-once and retry-safe |
| Analysis progress | **WebSocket**, resumable by stream ID | Many small ordered events over minutes; gap-free resume |
| Live telemetry ticks | **WebSocket**, server push, conflated | 4 Hz snapshots; a stale frame is worse than a dropped one |
| Live chaos controls | **WebSocket** client → server with acks | Latency-sensitive input on an already-authenticated, ordered channel; ephemeral, and replayable from the command log |
| Reports | HTTPS GET, `immutable`, edge-cached | Deterministic results never change |
| Platform status / KPIs | HTTPS GET polling every 5 s, 2 s nginx micro-cache | Low frequency, many viewers; one origin hit per 2 s regardless of viewer count |
| CI CLI | HTTPS + polling with jittered backoff | Firewall-friendly and simple |

We considered SSE for the two push streams and rejected it. Live controls need client→server messages, and
one multiplexed connection per tab costs less than SSE plus a separate POST channel.

## 8. Data model (Postgres 18)

```
users(id, email citext UNIQUE, handle UNIQUE, password_hash, is_guest, created_at, last_seen_at)
refresh_tokens(id, user_id, family_id, token_hash UNIQUE, expires_at, revoked_at, replaced_by)  -- rotation + reuse detection
api_keys(id, user_id, prefix UNIQUE, key_hash, name, last_used_at, revoked_at)
systems(id, owner_id, name, latest_version, row_version, created_at, updated_at, deleted_at)       -- OCC via row_version
system_versions(system_id, version, spec_yaml, spec_json JSONB, spec_hash, created_at)  PK(system_id, version)
analyses(id, owner_id, system_id, system_version, spec_hash, params JSONB, mode, status, priority,
         cells_total, cells_done, cells_cached, sims_executed, result JSONB, parent_id, created_at, started_at, finished_at)
analysis_cells(analysis_id, cell_id, load, magnitude, status, result_hash, summary JSONB, inferred)  PK(analysis_id, cell_id)
sim_results(hash PK, engine_version, summary JSONB, series BYTEA (zlib float32), events, wall_ms, hits, last_hit_at)
runs(id, owner_id, system_id, system_version, spec_hash, params JSONB, status, lease_token, sim_time, created_at, ended_at)
run_commands(run_id, seq, sim_time, cmd JSONB, issued_by, created_at)  PK(run_id, seq)            -- event-sourced
reports(slug PK, analysis_id UNIQUE, snapshot JSONB, created_at)
idempotency_keys(user_id, key, request_hash, status_code, response JSONB, created_at)  PK(user_id, key)
outbox(id, topic, payload JSONB, created_at, published_at)    -- partial index WHERE published_at IS NULL
activity_log(id, user_id, kind, data JSONB, created_at) PARTITION BY RANGE (created_at)          -- monthly partitions
```

* **Identifiers** use `uuidv7()` (Postgres 18), which is time-ordered and so keeps B-tree inserts local. API-visible IDs are opaque.
* **Pagination** is keyset (cursor = `(created_at, id)`), never `OFFSET`.
* **Read/write split with read-your-writes:** writes return the commit LSN (`pg_current_wal_insert_lsn()`). The client
  echoes it in `X-Kessler-Min-LSN`. A read goes to the replica only if `pg_last_wal_replay_lsn() ≥ min_lsn`; otherwise
  it goes to the primary. The routing ratio and replica lag are exported as metrics.
* **Isolation:** READ COMMITTED by default. State machines use conditional updates (`WHERE status = $expected`) instead of
  `SERIALIZABLE`, which avoids retry loops on hot rows.

## 9. Concurrency model

| Where | Mechanism | Why |
|---|---|---|
| C++ engine | Single-threaded, deterministic event loop per sim; **thread pool** for sweeps in the CLI and benchmarks; atomics for progress and cancellation | Determinism per sim; throughput from parallelism across sims |
| sim-worker | asyncio orchestrator plus a `ThreadPoolExecutor`. ctypes releases the GIL, so N sims run in parallel; a semaphore caps concurrency at the core count | Python coordinates, C++ computes |
| api / gateway | asyncio; asyncpg pool; pipelined Redis | High I/O concurrency on few cores |
| Work distribution | Redis Streams consumer groups; pending-entry reclaim (`XAUTOCLAIM`) after idle time; dead-letter queue after 5 deliveries | At-least-once delivery, made effectively-once by idempotent writes |
| Outbox relay | `SELECT … FOR UPDATE SKIP LOCKED` | Multiple relays never double-publish |
| Leader election | `pg_try_advisory_lock` on a dedicated session | The lock dies with the connection, so there is no split brain |
| Live-run ownership | Redis lease (`SET NX PX`) plus a monotonic fencing token | Stale owners are rejected downstream |
| Spec edits | Optimistic concurrency (`If-Match` → `412`) | No locks held across user think-time |
| Fan-out | One Redis subscription per topic per gateway node (ref-counted); per-socket bounded queue with latest-wins conflation | O(topics) Redis load instead of O(sockets); one slow client can't stall the others |

## 10. Simulation engine (C++20)

**Discrete-event simulation.** A binary min-heap orders events by `(time, seq)`; the seq tie-break guarantees determinism.
Plain-struct events go to a switch dispatch (no virtual calls or `std::function` on the hot path), and jobs and calls live in
pooled objects with generation counters, so the engine skips stale timeout events (lazy cancellation).

**Models:**
* Open-loop Poisson arrivals with load patterns, plus client timeouts and retries.
* Multi-instance services with *c* worker slots, held across blocking downstream calls (the root of cascading
  failure), and bounded FIFO, LIFO or adaptive-LIFO queues with queue timeouts.
* Log-normal service times fitted from p50/p99, plus a contention knee for databases.
* Load balancers: round-robin, random, least-outstanding and power-of-two-choices.
* Per-call timeout and retry with backoff (jitter: none, full, equal or decorrelated); retry budgets as token buckets; circuit
  breakers (closed, open, half-open); deadline propagation and cancellation.
* Caches whose hit ratio warms with successful fills (so cold-cache metastability appears without special-casing).
* Autoscalers with a provisioning delay.
* Fault injection: latency, errors, crash, spike, cache flush.

**Outputs.** Per-window (1 s) metrics:
* goodput
* p50/p99 from log-linear histograms
* amplification (attempts ÷ new requests)
* wasted work
* queue depth and utilisation
* breaker states
* per-edge RPS and error rate

A run is classified as **stable**, **recovered** (with recovery time) or **metastable** (it did not recover within the
horizon after the trigger ended).

**Determinism.** The engine uses its own xoshiro256\*\* RNG and its own distribution samplers, because `std::*_distribution`
is implementation-defined. Results are bit-identical for a given engine build, so `engine_version` is part of the cache key.

**Validation.** Unit tests check the engine against queueing theory: the M/M/1 mean wait and the M/M/c Erlang-C waiting
probability must match within statistical tolerance. Behavioural tests assert that a known retry-storm spec goes
metastable and a well-tuned one does not.

**Interface.** A stable C ABI (`kessler.h`), usable from Python through ctypes (which drops the GIL) or from any other
language. A `kessler-sim` CLI provides runs, sweeps and benchmarks.

## 11. Scheduling, fairness and cost control
* **Priorities:** live runs are dispatched on a separate stream with reserved slots. Interactive analyses come before CI batches.
* **Deficit Round Robin** across tenants stops one user's 500-cell sweep from starving everyone else. The KPI is
  **Jain's fairness index** over each tenant's share of the dispatched work.
* **Adaptive boundary search:** within a magnitude row, metastability is monotone in load, so bisection finds the
  tipping point in O(log n) cells instead of n. Skipped cells are shown as *inferred*, hatched in the UI, with the
  state their neighbours imply. The KPI is sims avoided versus the full grid (58 % on the 45-service example, with
  0 of 60 cells disagreeing with the full grid).
* **Overload is a property of the load alone:** every row shares the same seeds and the trigger starts at the same
  time, so the pre-trigger minute is bit-identical across rows (checked on 48 column/seed groups). Once any row finds
  a column overloaded before the trigger, that column and every higher load are overloaded in all rows; the top
  column is probed in one row first, so the others skip it.
* **The trigger axis is not assumed monotone.** With 30 seeds per cell, a 3.8x database slowdown tipped the checkout
  example 40 % of the time and an 8x slowdown 3 %: slow but successful calls hold capacity upstream, fast failures
  free it.
* **Content-addressed result cache:** identical (spec, params, seed, engine) inputs are never simulated twice. The KPI is cache hit ratio.
* **Admission control:** the server estimates a spec's event budget up front and rejects "simulation bombs"
  (`422`). When the queue is too deep, new submissions get `503` plus `Retry-After` instead of queueing without bound.

## 12. Reliability: failure modes

| Failure | Behaviour |
|---|---|
| One api replica dies | nginx `max_fails` ejects it; requests retry on the next upstream (idempotent methods only) |
| An edge dies | The L4 LB health-checks it and routes to the other edge |
| A gateway dies | Clients reconnect with jittered backoff to another node and resume from the last stream ID |
| A worker dies mid-analysis | Any surviving worker reclaims pending tasks after 30 s idle; idempotent writes mean nothing is double-counted. Every long-lived loop is supervised and no log call can raise, so a recovery path cannot take the survivor down too (it once did, through a reserved logging key; found by the crash test) |
| A worker dies mid-live-run | Lease expiry, then reassignment, then deterministic replay continues with the same numbers |
| The scheduler leader dies | Its advisory lock is released with the session; the standby takes over within 2 s (`kessler_scheduler_leader` shows who leads). Measured: a worker and then the leader killed mid-sweep, the sweep finished with every simulation exactly once and results identical to a local recomputation |
| Redis is unavailable | The api keeps serving reads and writes from Postgres (the outbox buffers dispatch); the UI falls back to HTTP polling |
| Replica lag | Reads go to the replica only once it has replayed the client's last write (from a header or a short-lived cookie, so any client gets read-your-writes); a miss there is confirmed on the primary before a 404; snapshots that come with a stream position are fenced at the primary's WAL position. Lag is alerted on |
| Poison task | Moved to the dead-letter stream after 5 deliveries and the cell is marked `failed`, so the analysis still completes |

Kessler applies its own medicine: clients use jittered exponential backoff with a retry budget, servers propagate
deadlines, and every queue is bounded. `backend/kessler/spec/templates/kessler-self.yaml` models Kessler's own architecture, and Kessler analyses it.

## 13. Security
* **Credentials:** argon2id password hashing. Access JWTs last 15 minutes. Refresh tokens are opaque, rotated on every use, and
  **reuse detection** revokes the whole token family. Both live in `httpOnly; Secure; SameSite=Strict` cookies, with a
  double-submit CSRF token for cookie-authenticated writes.
* **Authorisation:** ownership checks on every resource; HMAC capability tokens for stream topics; hashed API keys with
  a visible prefix (`ks_…`) for CI.
* **Request hygiene:**
  * Rate limiting in two layers: nginx per IP, then Redis GCRA per user and route class, returning `429` with `Retry-After`.
  * Pydantic validation with hard limits on spec size and simulated-event budget.
  * Parameterised SQL everywhere.
* **Platform hardening:** CSP, HSTS and other security headers at the edge; containers run as non-root; secrets come from the environment only.

## 14. Observability and KPIs
* **Metrics:** Prometheus, using RED for every service, USE for workers and the database, and the business KPIs below. Grafana dashboards are provisioned as code.
* **Traces:** OpenTelemetry. W3C `traceparent` is propagated through nginx → api → Redis message → worker → gateway.
* **Logs:** structured JSON with `request_id` and `trace_id`.

**Platform KPIs:** API p99 by route; time-to-first-insight p95; analysis completion p95; engine events/s/core; sims/s;
cache hit ratio; adaptive savings; queue wait p95 by priority; Jain's fairness index; WS connections; fan-out
latency p99; conflated-frame ratio; live control round-trip p95; replica routing ratio and lag; error-budget burn rate.

**Product KPIs:** activation (signup → first analysis within the session); findings per analysis; fix-adoption rate (a fix applied
and the sweep re-run); median stable-region gain per applied fix; weekly returning users; CI runs per week.

## 15. Load balancing and reverse proxy
* **L4 tier:** nginx `stream` round-robins TCP across the two edges, with passive health checks (`max_fails` / `fail_timeout`).
* **L7 edges:**
  * `upstream api { least_conn; keepalive 64; }`, plus `proxy_next_upstream error timeout http_502 http_503` for idempotent methods only.
  * `upstream gateway { least_conn; }`. Fan-out goes through Redis, so any gateway can serve any client. That avoids
    sticky sessions and keeps the pool elastic.
  * `limit_req` zones: per-IP general traffic, plus a strict zone for `POST /analyses` and `/auth/*`.
  * A 2 s `proxy_cache` micro-cache for KPIs, and long-lived caching for immutable reports.
  * `Upgrade`/`Connection` headers for `/ws`, with `proxy_read_timeout` above the heartbeat interval.
  * Gzip, hashed assets served with `Cache-Control: immutable`, and `X-Request-ID` generation.
* **Inside the domain:** the engine *models* round-robin, random, least-outstanding and power-of-two-choices balancing, so users can
  measure how their balancer choice changes tail latency under failure.

## 16. Deployment
* **Local development:** native Windows/Linux processes managed by `scripts/devstack.py` (Postgres primary + replica, Redis, nginx L4 + 2× L7,
  3× api, 2× gateway, 2× scheduler, 2× worker).
* **Containers:** Docker Compose with the same topology plus PgBouncer, Prometheus, Grafana and Jaeger.
* **Production:** not deployed yet. The plan: managed Postgres and Redis, k8s HPA on queue depth and zero-downtime
  rolling deploys.

## 17. Verification

You can re-run the scripts that check the claims in this document:

* **Simulator correctness at scale** (`scripts/validate_engine.py`): on the 45-service example, flow
  conservation against the static model (median deviation 0.32 %), request accounting, determinism
  (sequential and parallel), the simulated overload onset against the predicted saturation point, and
  the adaptive planner against a full grid (0 of 60 cells disagree).
* **The running stack** (`scripts/e2e_check.py`): create, idempotency, a streamed sweep, recomputation of
  stored results (bit-identical), the result cache, read latency under saturated workers, live runs.
* **Failure drills** (`scripts/e2e_check.py --failover`): a worker and then the scheduler leader crash
  mid-sweep; then each WebSocket gateway and an api instance. Sweeps complete with every simulation
  exactly once, clients resume their streams without gaps, and nginx hides the dead api instance.
* **Real outages** (`scripts/replay_incidents.py`): AWS DynamoDB (2015) and Facebook (2010) rebuilt from
  their public postmortems. The replay sweeps unknown numbers over plausible grids instead of tuning them, and
  tests Kessler's predictions only on settings that reproduce each postmortem's stated facts (the risk flagged in
  advance, the recovery the engineers used, the fixes they planned).

| Outage and prediction | Result |
|---|---|
| **AWS DynamoDB, 2015 ([postmortem](https://aws.amazon.com/message/5467D2/))** | |
| Settings that reproduce the postmortem's facts | 35 of 64 |
| Kessler's static check flags the risk before simulating | 35/35 |
| Pausing requests, then resuming, ends the failure (what AWS did) | 35/35 |
| More metadata capacity prevents it | 26/35 |
| A lower renewal rate prevents it | 26/35 |
| A longer timeout alone prevents it | 1/35 |
| A longer timeout plus a queue that drops stale requests prevents it | 35/35 |
| Kessler's one-click fixes prevent it | 35/35 |
| **Facebook, 2010 ([post](https://engineering.fb.com/2010/09/23/uncategorized/more-details-on-today-s-outage/))** | |
| Settings that reproduce the post's facts | 18 of 24 |
| Kessler's static check flags the risk before simulating | 18/18 |
| Restoring all traffic at once collapses it again | 18/18 |
| Letting users back while the database copes recovers (what Facebook did) | 14/18 (median 4 min of simulated time) |
| A database sized for cold traffic prevents it | 16/18 |
| Kessler's one-click fixes prevent it | 16/18 |

What the misses mean:

- **A longer timeout alone (1/35).** The metadata queue holds more work than even a tripled timeout
  covers, so callers still time out and retry. Add a queue that drops requests older than the
  timeout and it works every time; Kessler's static check flags this case ("queue holds requests
  longer than callers wait").
- **Facebook's hardest corner.** The remaining misses all sit where a database that can serve only
  a third to a seventh of cold traffic sits behind a 200,000-key cache. The cache refills only as fast
  as that database answers misses, so recovery outlasts the 10-minute observation cap and a cautious
  ramp needs more than 30 simulated minutes, consistent with the 2.5-hour real outage.
- The models are reconstructions. They reproduce each postmortem's stated mechanism and order of
  events; neither company published its internal numbers.

Testing found real defects, each now fixed with a regression test: read-your-writes that only held for the
web client, a snapshot/stream ordering race, a reclaim path whose log call crashed the surviving worker,
lint output that did not scale to large systems, and verdicts that called slow but steady recoveries
permanent.

## 18. Alternatives considered
* **pybind11 vs a C ABI + ctypes:** chose the C ABI. It is stable across Python versions, works with any compiler, and can be called from
  Go or Node later. The marshalling cost is negligible because frames are produced as JSON in C++ and forwarded byte-for-byte.
* **Kafka vs Redis Streams:** Redis Streams give consumer groups, acknowledgements and claiming at our throughput (about 2 k ops/s) with
  far less operational weight. Kafka becomes worth it beyond about 100 k msg/s or when long retention is needed.
* **Postgres `LISTEN/NOTIFY` for fan-out:** rejected. It has an 8 KB payload cap, a single notification queue, and couples real-time load to the database.
* **Full grid vs adaptive search:** Kessler supports both. Adaptive is the default, with inferred cells marked.
* **CTMC (analytic) model instead of simulation:** faster, but cannot express breakers, caches and autoscaling. It may be used later as an
  instant first-pass estimate.

**Future work:**
* Infer a spec from OpenTelemetry traces (calibration).
* A GitHub App that comments the map diff on PRs.
* Multi-region topologies.
* An analytic CTMC fast path.
