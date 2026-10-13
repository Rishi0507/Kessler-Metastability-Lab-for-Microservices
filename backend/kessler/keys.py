from __future__ import annotations

PREFIX = "ks"

STREAM_TASKS = f"{PREFIX}:stream:tasks"
STREAM_RESULTS = f"{PREFIX}:stream:results"
STREAM_LIVE = f"{PREFIX}:stream:live"
STREAM_DEAD = f"{PREFIX}:stream:dead"
GROUP_WORKERS = "workers"
GROUP_SCHEDULER = "scheduler"
GROUP_LIVE = "live"

TENANTS = f"{PREFIX}:tenants"


def tenant_queue(tenant: str) -> str:
    return f"{PREFIX}:q:{tenant}"


def ticket(token: str) -> str:
    return f"{PREFIX}:ticket:{token}"


def ratelimit(bucket: str, subject: str) -> str:
    return f"{PREFIX}:rl:{bucket}:{subject}"


def progress_stream(analysis_id: str) -> str:
    return f"{PREFIX}:progress:{analysis_id}"


def run_ticks(run_id: str) -> str:
    return f"{PREFIX}:run:{run_id}:ticks"


def run_ctl(run_id: str) -> str:
    return f"{PREFIX}:run:{run_id}:ctl"


def run_lease(run_id: str) -> str:
    return f"{PREFIX}:lease:run:{run_id}"


def run_fence(run_id: str) -> str:
    return f"{PREFIX}:fence:run:{run_id}"


def channel(topic: str) -> str:
    return f"{PREFIX}:ch:{topic}"


def service_stats(kind: str, instance: str) -> str:
    return f"{PREFIX}:svc:{kind}:{instance}"


SERVICE_STATS_PATTERN = f"{PREFIX}:svc:*"
