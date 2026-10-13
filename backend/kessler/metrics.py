from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

LATENCY_BUCKETS = (0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
SLOW_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 20, 30, 60, 120, 300)

HTTP_REQUESTS = Counter("kessler_http_requests_total", "HTTP requests", ["route", "method", "status"])
HTTP_LATENCY = Histogram(
    "kessler_http_request_duration_seconds", "HTTP latency", ["route", "method"], buckets=LATENCY_BUCKETS
)
HTTP_INFLIGHT = Gauge("kessler_http_inflight_requests", "In-flight HTTP requests")
RATE_LIMITED = Counter("kessler_ratelimited_total", "Requests rejected by rate limiting", ["bucket"])
ADMISSION_REJECTED = Counter(
    "kessler_admission_rejected_total", "Submissions rejected by admission control", ["reason"]
)

DB_READS = Counter("kessler_db_reads_total", "Read routing decisions", ["target", "reason"])
REPLICA_LAG = Gauge("kessler_replica_lag_bytes", "Primary WAL position minus replica replay position")

ANALYSES_SUBMITTED = Counter("kessler_analyses_submitted_total", "Analyses accepted", ["mode"])
ANALYSES_FINISHED = Counter("kessler_analyses_finished_total", "Analyses finished", ["status"])
TIME_TO_FIRST_INSIGHT = Histogram(
    "kessler_time_to_first_insight_seconds", "Submit -> first cell result", buckets=SLOW_BUCKETS
)
ANALYSIS_DURATION = Histogram("kessler_analysis_duration_seconds", "Submit -> done", buckets=SLOW_BUCKETS)
SIMS = Counter("kessler_sims_total", "Simulations by outcome", ["result"])
SIM_SECONDS = Histogram("kessler_sim_duration_seconds", "Wall time per simulation", buckets=SLOW_BUCKETS)
SIM_EVENTS = Counter("kessler_sim_events_total", "Simulated events processed")
SIMS_AVOIDED = Counter("kessler_sims_avoided_total", "Simulations skipped by adaptive search vs the full grid")
QUEUE_WAIT = Histogram("kessler_queue_wait_seconds", "Task enqueue -> start", ["priority"], buckets=SLOW_BUCKETS)
QUEUE_DEPTH = Gauge("kessler_queue_depth", "Tasks waiting in tenant queues", ["priority"])
INFLIGHT_TASKS = Gauge("kessler_inflight_tasks", "Tasks dispatched to workers and not yet acknowledged")
FAIRNESS = Gauge("kessler_fairness_jain_index", "Jain's fairness index over active tenants' dispatch share")
WORKER_BUSY = Gauge("kessler_worker_busy_slots", "Busy simulation slots on this worker")

WS_CONNECTIONS = Gauge("kessler_ws_connections", "Open WebSocket connections")
WS_MESSAGES = Counter("kessler_ws_messages_total", "WebSocket messages", ["direction", "type"])
WS_FANOUT_LATENCY = Histogram(
    "kessler_ws_fanout_latency_seconds", "Producer publish -> socket send", buckets=LATENCY_BUCKETS
)
WS_CONFLATED = Counter("kessler_ws_conflated_total", "Live frames replaced before a slow client read them")
WS_SLOW_DISCONNECTS = Counter(
    "kessler_ws_slow_consumer_disconnects_total", "Clients dropped for falling too far behind"
)
LIVE_RUNS = Gauge("kessler_live_runs", "Live runs owned by this worker")
LIVE_TICK_LAG = Histogram(
    "kessler_live_tick_lag_seconds", "How late a live tick was published", buckets=LATENCY_BUCKETS
)
LIVE_CONTROL_RTT = Histogram("kessler_live_control_rtt_seconds", "Control sent -> applied", buckets=LATENCY_BUCKETS)
LIVE_FAILOVERS = Counter("kessler_live_failovers_total", "Live runs resumed by replay after a worker loss")


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
