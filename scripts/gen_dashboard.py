#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "deploy" / "grafana" / "dashboards" / "kessler.json"

PANELS: list[tuple[str, str, list[tuple[str, str]], str]] = [
    (
        "API requests per second by route",
        "reqps",
        [("sum by (route) (rate(kessler_http_requests_total[1m]))", "{{route}}")],
        "RED: rate",
    ),
    (
        "API error ratio vs SLO (0.1%)",
        "percentunit",
        [("kessler:api_error_ratio:rate5m", "5m"), ("kessler:api_error_ratio:rate1h", "1h"), ("vector(0.001)", "SLO")],
        "RED: errors",
    ),
    (
        "API latency p50 / p99",
        "s",
        [
            ("histogram_quantile(0.5, sum by (le) (rate(kessler_http_request_duration_seconds_bucket[5m])))", "p50"),
            ("histogram_quantile(0.99, sum by (le) (rate(kessler_http_request_duration_seconds_bucket[5m])))", "p99"),
        ],
        "RED: duration",
    ),
    (
        "Time to first insight p50 / p95",
        "s",
        [
            ("histogram_quantile(0.5, sum by (le) (rate(kessler_time_to_first_insight_seconds_bucket[30m])))", "p50"),
            ("histogram_quantile(0.95, sum by (le) (rate(kessler_time_to_first_insight_seconds_bucket[30m])))", "p95"),
        ],
        "Submit -> first cell result",
    ),
    (
        "Simulations per second",
        "ops",
        [("sum by (result) (rate(kessler_sims_total[1m]))", "{{result}}")],
        "Executed vs served from the content-addressed cache",
    ),
    (
        "Simulated events per second",
        "short",
        [("sum(rate(kessler_sim_events_total[1m]))", "events/s")],
        "Engine throughput across all workers",
    ),
    (
        "Simulation backlog by priority",
        "short",
        [("kessler_queue_depth", "priority {{priority}}"), ("kessler_inflight_tasks", "in flight")],
        "Tenant queues and dispatched tasks",
    ),
    (
        "Queue wait p95",
        "s",
        [
            (
                "histogram_quantile(0.95, sum by (le, priority) (rate(kessler_queue_wait_seconds_bucket[5m])))",
                "{{priority}}",
            )
        ],
        "Enqueue -> dispatch/start",
    ),
    (
        "Fairness (Jain's index)",
        "none",
        [("kessler_fairness_jain_index", "index")],
        "1.0 = every backlogged tenant gets its weighted share",
    ),
    ("Worker slots busy", "short", [("sum(kessler_worker_busy_slots)", "busy")], "USE: utilisation"),
    (
        "WebSocket connections",
        "short",
        [("sum(kessler_ws_connections)", "connections"), ("sum(kessler_live_runs)", "live runs")],
        "Realtime load",
    ),
    (
        "Live update fan-out p99",
        "s",
        [
            (
                "histogram_quantile(0.99, sum by (le) (rate(kessler_ws_fanout_latency_seconds_bucket[5m])))",
                "publish -> send",
            )
        ],
        "Producer timestamp to socket write",
    ),
    (
        "Live frames conflated / slow consumers dropped",
        "ops",
        [
            ("sum(rate(kessler_ws_conflated_total[1m]))", "conflated"),
            ("sum(rate(kessler_ws_slow_consumer_disconnects_total[1m]))", "dropped"),
        ],
        "Backpressure at work",
    ),
    (
        "Read routing",
        "ops",
        [("sum by (target, reason) (rate(kessler_db_reads_total[1m]))", "{{target}} ({{reason}})")],
        "LSN-gated read-your-writes",
    ),
    ("Replica lag", "bytes", [("max(kessler_replica_lag_bytes)", "lag")], "WAL bytes the replica is behind"),
    (
        "Rate limited and shed requests",
        "ops",
        [
            ("sum by (bucket) (rate(kessler_ratelimited_total[1m]))", "{{bucket}}"),
            ("sum by (reason) (rate(kessler_admission_rejected_total[1m]))", "admission: {{reason}}"),
        ],
        "429s and 503s by cause",
    ),
]


def panel(i: int, title: str, unit: str, targets: list[tuple[str, str]], desc: str) -> dict:
    return {
        "id": i + 1,
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "gridPos": {"h": 8, "w": 12, "x": (i % 2) * 12, "y": (i // 2) * 8},
        "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 8}}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
            {
                "refId": chr(65 + j),
                "expr": expr,
                "legendFormat": legend,
                "datasource": {"type": "prometheus", "uid": "prometheus"},
            }
            for j, (expr, legend) in enumerate(targets)
        ],
    }


def main() -> None:
    dash = {
        "uid": "kessler-platform",
        "title": "Kessler platform",
        "tags": ["kessler"],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "10s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": [panel(i, *p) for i, p in enumerate(PANELS)],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dash, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(PANELS)} panels)")


if __name__ == "__main__":
    main()
