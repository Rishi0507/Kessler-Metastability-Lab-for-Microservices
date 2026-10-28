# Progress

Kept current so work can resume from any point.

## Done

- [x] Idea validation (competitors: Rootly Academy/Uptime Labs, YouBrokeProd, Faultline, Breakscale, systemInPain; research: AWS "Analyzing Metastable Failures" 2025, arXiv 2510.03551)
- [x] System design ([DESIGN.md](DESIGN.md)) and a published System Design page
- [x] C++20 engine: DES core, resilience models, contention/congestive collapse, static analysis, C ABI, CLI, 31 tests (queueing-theory validated)
- [x] Python spec layer: YAML format, compiler, 11 lint rules with fix patches, 5 templates
- [x] Adaptive sweep planner (stateless bisection) and findings
- [x] api: auth (guest, rotation, reuse detection), systems (OCC), analyses (outbox, idempotency, quotas, admission control), experiments, reports, live runs, KPIs
- [x] scheduler: advisory-lock leader election, outbox relay, DRR fair share, results consumer, watchdog, maintenance
- [x] worker: GIL-free engine pool, idempotent writes, live runs with lease + fencing + replay failover
- [x] gateway: tickets, capability tokens, ref-counted subscriptions, backfill-then-flush, conflation, fencing
- [x] nginx L4 + 2x L7 configs; native devstack; docker compose; Prometheus SLO alerts; Grafana dashboard as code; CI
- [x] Web app: landing (three.js energy landscape), systems, workspace (live lint + fixes), analysis (map, 3D terrain, experiments, reproduce live), live run (3D topology, controls, charts), status, shared reports

## Next

- [ ] Run the full stack locally (needs permission to execute the portable Postgres/Redis/nginx builds in `.tools/`)
- [ ] Integration tests against the running stack (auth, idempotency, OCC, sweep end to end, failover drills)
- [ ] Load test: API latency, time to first insight, WebSocket fan-out; publish the numbers here and in the README
- [ ] Browser walkthrough of every page in both themes
- [ ] Deployment (to be discussed)
