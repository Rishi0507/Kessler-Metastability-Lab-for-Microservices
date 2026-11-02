# Progress

Kept current so work can resume from any point.

## Done

- [x] Idea validation (competitors: Rootly Academy/Uptime Labs, YouBrokeProd, Faultline, Breakscale, systemInPain; research: AWS "Analyzing Metastable Failures" HotOS '25, arXiv 2510.03551). Re-checked 2026-11-01: the closest work models one service at a time and lists multi-service topologies, recovery actions and a usable tool as open problems; small open-source explorers cover a service and one dependency.
- [x] System design ([DESIGN.md](DESIGN.md)) and a published System Design page
- [x] C++20 engine: DES core, resilience models, contention/congestive collapse, static analysis, C ABI, CLI, 31 tests (queueing-theory validated)
- [x] Python spec layer: YAML format, compiler, 11 lint rules with fix patches (consolidated per rule on large systems), format-preserving Apply fix, 8 templates including a 45-service system and two real outages
- [x] Adaptive sweep planner (stateless bisection, overload shared per load column) and findings
- [x] api, scheduler, worker, gateway, nginx L4 + 2x L7, devstack, docker compose, observability, CI
- [x] Web app: landing, systems, workspace, analysis (map, terrain, experiments), live run, status, shared reports
- [x] Full stack run locally; browser walkthrough of the main flows (about a dozen bugs found and fixed)
- [x] Engine validation at scale (`scripts/validate_engine.py`): 7/7 on the 45-service system
- [x] End-to-end and failover checks against the running stack (`scripts/e2e_check.py`): 7/7, and the sweep survives a worker crash plus a scheduler leader crash with identical results
- [x] Real-outage replays (`scripts/replay_incidents.py`): AWS DynamoDB 2015 and Facebook 2010

## Bugs found by testing (all fixed, with tests)

- Example sign-in raced the home redirect; hero trail spammed three.js warnings
- Apply fix rewrote the whole YAML file (now ruamel round-trip, only the changed lines)
- Inferred map cells mislabelled overload as metastable; median recovery ignored runs that never recovered
- Snapshot read before the stream position (a finished sweep could look stuck); replica snapshots could miss events
- Read-your-writes only worked for the web app (now a cookie for any client, and 404s are confirmed on the primary)
- A crashed worker's reclaimed tasks crashed the surviving worker (reserved logging key); loops are now supervised
- Linter produced 86 near-duplicate findings on a large system; score bottomed out at 0
- devstack: PID reuse could signal an unrelated process; `up -d` hung on an inherited pipe; graceful stop could not cross consoles

## Next

- [ ] Load test with many concurrent users (API latency, WebSocket fan-out, fair share across tenants)
- [ ] Browser walkthrough of every page in both themes, on a focused window (Chrome demotes unfocused tabs to idle priority, which made sweeps look frozen in automation)
- [ ] Deployment (to be discussed)
