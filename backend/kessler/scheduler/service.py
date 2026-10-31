from __future__ import annotations

import asyncio
import time
from typing import Any

import asyncpg
import orjson
import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from .. import keys
from ..analysis.planner import SweepParams
from ..config import Settings
from ..db import Database
from ..engine import get_engine
from ..heartbeat import cancel, heartbeat_loop, supervise
from ..logs import get_logger
from ..metrics import FAIRNESS, INFLIGHT_TASKS, QUEUE_DEPTH, QUEUE_WAIT, SCHEDULER_LEADER, TIME_TO_FIRST_INSIGHT
from ..redisio import create_redis
from .fairshare import WEIGHTS, DeficitRoundRobin, FairnessTracker
from .orchestrator import advance, enqueue, publish, tenant_of

log = get_logger("kessler.scheduler")

LEADER_LOCK = 4_242_001

MOVE_TASKS = """
local moved = {}
for i = 1, tonumber(ARGV[1]) do
  local v = redis.call('LPOP', KEYS[1])
  if not v then break end
  redis.call('XADD', KEYS[2], '*', 'task', v)
  moved[#moved + 1] = v
end
if redis.call('LLEN', KEYS[1]) == 0 then redis.call('ZREM', KEYS[3], ARGV[2]) end
return moved
"""


async def ensure_group(redis: aioredis.Redis, stream: str, group: str) -> None:
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


class Scheduler:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.db = Database(settings, "kessler-scheduler")
        self.redis: aioredis.Redis | None = None
        self.leader = False
        self._lock_conn: asyncpg.Connection | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self.engine_version = get_engine().version
        self.drr = DeficitRoundRobin()
        self.fairness = FairnessTracker()
        self._capacity = (0.0, 0)
        self.stats: dict[str, Any] = {"leader": 0, "dispatched": 0, "relayed": 0, "results": 0}

    async def _try_lead(self) -> bool:
        try:
            if self._lock_conn is None or self._lock_conn.is_closed():
                self._lock_conn = await asyncpg.connect(
                    self.s.database_url,
                    server_settings={"application_name": f"kessler-scheduler-lock:{self.s.instance}"[:63]},
                )
            return bool(await self._lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_LOCK))
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning("leader election failed", extra={"error": str(exc)})
            return False

    async def _still_leader(self) -> bool:
        try:
            return self._lock_conn is not None and await self._lock_conn.fetchval("SELECT 1") == 1
        except (OSError, asyncpg.PostgresError):
            return False

    async def run(self) -> None:
        await self.db.connect()
        self.redis = create_redis(self.s.redis_url)
        for stream, group in (
            (keys.STREAM_TASKS, keys.GROUP_WORKERS),
            (keys.STREAM_RESULTS, keys.GROUP_SCHEDULER),
            (keys.STREAM_LIVE, keys.GROUP_LIVE),
        ):
            await ensure_group(self.redis, stream, group)
        hb = asyncio.create_task(heartbeat_loop(self.redis, "scheduler", self.s.instance, lambda: self.stats))
        try:
            while True:
                if not self.leader:
                    if await self._try_lead():
                        self.leader = True
                        self.stats["leader"] = 1
                        SCHEDULER_LEADER.set(1)
                        log.info("became leader", extra={"instance": self.s.instance})
                        self._tasks = [
                            asyncio.create_task(self._guard(self._outbox_loop, "outbox")),
                            asyncio.create_task(self._guard(self._dispatch_loop, "dispatch")),
                            asyncio.create_task(self._guard(self._results_loop, "results")),
                            asyncio.create_task(self._guard(self._watchdog_loop, "watchdog")),
                            asyncio.create_task(self._guard(self._maintenance_loop, "maintenance")),
                        ]
                    else:
                        await asyncio.sleep(2.0)
                        continue
                await asyncio.sleep(1.0)
                if not await self._still_leader():
                    log.warning("lost leadership", extra={"instance": self.s.instance})
                    self.leader = False
                    self.stats["leader"] = 0
                    SCHEDULER_LEADER.set(0)
                    await cancel(*self._tasks)
                    self._tasks = []
                    if self._lock_conn is not None:
                        await self._lock_conn.close()
                        self._lock_conn = None
        finally:
            await cancel(hb, *self._tasks)
            if self._lock_conn is not None:
                await self._lock_conn.close()
            await self.redis.aclose()
            await self.db.close()

    async def _guard(self, fn: Any, name: str) -> None:
        await supervise(fn, name, log)

    async def _outbox_loop(self) -> None:
        assert self.redis is not None
        while True:
            to_publish: list[tuple[str, list[dict[str, Any]]]] = []
            async with self.db.primary_conn() as conn, conn.transaction():
                rows = await conn.fetch(
                    """SELECT id, topic, payload FROM outbox WHERE published_at IS NULL
                       ORDER BY id LIMIT 25 FOR UPDATE SKIP LOCKED"""
                )
                for r in rows:
                    to_publish.extend(await self._handle_outbox(conn, r["topic"], r["payload"]))
                if rows:
                    await conn.execute(
                        "UPDATE outbox SET published_at = now() WHERE id = ANY($1::bigint[])", [r["id"] for r in rows]
                    )
            for analysis_id, events in to_publish:
                await publish(self.redis, analysis_id, events)
            self.stats["relayed"] += len(rows)
            if not rows:
                await asyncio.sleep(0.2)

    async def _handle_outbox(
        self, conn: asyncpg.Connection, topic: str, payload: dict[str, Any]
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        assert self.redis is not None
        if topic == "analysis.created":
            async with conn.transaction():
                events = await advance(conn, self.redis, payload["id"], self.s, self.engine_version, start=True)
            return [(payload["id"], events)]
        if topic == "analysis.cancelled":
            return [(payload["id"], [{"type": "status", "status": "cancelled"}])]
        if topic == "run.created":
            await self.redis.xadd(keys.STREAM_LIVE, {b"run": payload["id"].encode(), b"resume": b"0"})
        return []

    async def _worker_capacity(self) -> int:
        assert self.redis is not None
        ts, cap = self._capacity
        if time.monotonic() - ts < 2.0:
            return cap
        slots = 0
        async for k in self.redis.scan_iter(match=keys.service_stats("worker", "*"), count=100):
            v = await self.redis.hget(k, "slots")
            slots += int(v or 0)
        cap = int(slots * self.s.dispatch_prefetch)
        self._capacity = (time.monotonic(), cap)
        return cap

    async def _inflight(self) -> int:
        assert self.redis is not None
        try:
            for g in await self.redis.xinfo_groups(keys.STREAM_TASKS):
                if g["name"] in (keys.GROUP_WORKERS, keys.GROUP_WORKERS.encode()):
                    return int(g.get("pending", 0) or 0) + int(g.get("lag", 0) or 0)
        except ResponseError:
            pass
        return 0

    async def _dispatch_loop(self) -> None:
        assert self.redis is not None
        mover = self.redis.register_script(MOVE_TASKS)
        while True:
            capacity = await self._worker_capacity()
            inflight = await self._inflight()
            INFLIGHT_TASKS.set(inflight)
            tenants = await self.redis.zrange(keys.TENANTS, 0, -1, withscores=True)
            if not tenants:
                QUEUE_DEPTH.labels("1").set(0)
                QUEUE_DEPTH.labels("2").set(0)
                await asyncio.sleep(0.1)
                continue
            names = [t.decode() for t, _ in tenants]
            prio = {t.decode(): int(p) for t, p in tenants}
            pipe = self.redis.pipeline(transaction=False)
            for t in names:
                pipe.llen(keys.tenant_queue(t))
            backlog = dict(zip(names, await pipe.execute(), strict=True))
            for p in (1, 2):
                QUEUE_DEPTH.labels(str(p)).set(sum(n for t, n in backlog.items() if prio[t] == p))
            free = capacity - inflight
            if free <= 0:
                await asyncio.sleep(0.05)
                continue
            active = {t: prio[t] for t in names if backlog[t] > 0}
            interactive = {t: p for t, p in active.items() if p == 1}
            order = self.drr.allocate(interactive or active, free, backlog)
            counts: dict[str, int] = {}
            for t in order:
                counts[t] = counts.get(t, 0) + 1
            now = time.time()
            moved_total = 0
            for t, n in counts.items():
                moved = await mover(keys=[keys.tenant_queue(t), keys.STREAM_TASKS, keys.TENANTS], args=[n, t])
                for raw in moved:
                    task = orjson.loads(raw)
                    QUEUE_WAIT.labels(str(task.get("p", 1))).observe(max(0.0, now - float(task.get("q", now))))
                    self.fairness.record(t, WEIGHTS.get(prio[t], 1.0))
                moved_total += len(moved)
            self.stats["dispatched"] += moved_total
            j = self.fairness.index(set(active))
            if j is not None:
                FAIRNESS.set(j)
                self.stats["fairness"] = round(j, 4)
            await asyncio.sleep(0.02 if moved_total else 0.1)

    async def _results_loop(self) -> None:
        assert self.redis is not None
        consumer = f"sched-{self.s.instance}"
        start_id: bytes | str = "0"
        while True:
            resp = await self.redis.xreadgroup(
                keys.GROUP_SCHEDULER, consumer, {keys.STREAM_RESULTS: start_id}, count=200, block=1000
            )
            entries = resp[0][1] if resp else []
            if start_id == "0" and not entries:
                start_id = ">"
                continue
            if not entries:
                continue
            analyses: dict[str, list[bytes]] = {}
            for msg_id, fields in entries:
                analyses.setdefault(fields[b"a"].decode(), []).append(msg_id)
                if fields.get(b"first") == b"1" and fields.get(b"ttfi"):
                    TIME_TO_FIRST_INSIGHT.observe(float(fields[b"ttfi"]))
            for aid, ids in analyses.items():
                async with self.db.primary_conn() as conn:
                    events = await advance(conn, self.redis, aid, self.s, self.engine_version)
                await publish(self.redis, aid, events)
                await self.redis.xack(keys.STREAM_RESULTS, keys.GROUP_SCHEDULER, *ids)
                self.stats["results"] += len(ids)

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            await self._dead_letters()
            await self._expired_leases()
            await self._stuck_analyses()

    async def _dead_letters(self) -> None:
        assert self.redis is not None
        try:
            pending = await self.redis.xpending_range(
                keys.STREAM_TASKS, keys.GROUP_WORKERS, min="-", max="+", count=100
            )
        except ResponseError:
            return
        for p in pending:
            if int(p["times_delivered"]) <= self.s.task_max_deliveries:
                continue
            msg_id = p["message_id"]
            claimed = await self.redis.xclaim(
                keys.STREAM_TASKS, keys.GROUP_WORKERS, f"sched-{self.s.instance}", min_idle_time=0, message_ids=[msg_id]
            )
            for cid, fields in claimed:
                raw = fields.get(b"task", b"{}")
                await self.redis.xadd(
                    keys.STREAM_DEAD, {b"task": raw, b"reason": b"max_deliveries"}, maxlen=10_000, approximate=True
                )
                task = orjson.loads(raw)
                async with self.db.write() as conn:
                    done = await conn.fetchval(
                        """UPDATE analysis_tasks SET status = 'failed', finished_at = now(),
                                  summary = '{"verdict": "failed", "error": "simulation crashed repeatedly"}'
                           WHERE analysis_id = $1 AND cell = $2 AND seed_index = $3 AND status NOT IN ('done', 'failed')
                           RETURNING 1""",
                        task["a"],
                        task["c"],
                        task["s"],
                    )
                if done:
                    await self.redis.xadd(
                        keys.STREAM_RESULTS, {b"a": task["a"].encode(), b"c": str(task["c"]).encode()}
                    )
                await self.redis.xack(keys.STREAM_TASKS, keys.GROUP_WORKERS, cid)
                log.warning("dead-lettered task", extra={"analysis": task.get("a"), "cell": task.get("c")})

    async def _expired_leases(self) -> None:
        assert self.redis is not None
        async with self.db.primary_conn() as conn:
            runs = await conn.fetch(
                "SELECT id FROM runs WHERE status = 'running' AND started_at < now() - interval '3 seconds'"
            )
        for r in runs:
            rid = str(r["id"])
            if await self.redis.exists(keys.run_lease(rid)):
                continue
            if await self.redis.set(f"{keys.PREFIX}:requeued:{rid}", b"1", nx=True, ex=10):
                await self.redis.xadd(keys.STREAM_LIVE, {b"run": rid.encode(), b"resume": b"1"})
                log.warning("live run lost its worker; resuming elsewhere", extra={"run": rid})

    async def _stuck_analyses(self) -> None:
        assert self.redis is not None
        async with self.db.primary_conn() as conn:
            stuck = await conn.fetch(
                """SELECT a.id FROM analyses a WHERE a.status = 'running'
                   AND coalesce((SELECT max(finished_at) FROM analysis_tasks t WHERE t.analysis_id = a.id),
                                a.started_at) < now() - interval '10 minutes'
                   LIMIT 20"""
            )
        for r in stuck:
            async with self.db.primary_conn() as conn, conn.transaction():
                a = await conn.fetchrow("SELECT * FROM analyses WHERE id = $1 FOR UPDATE", r["id"])
                tasks = await conn.fetch(
                    """SELECT cell, seed_index, task_hash FROM analysis_tasks
                       WHERE analysis_id = $1 AND status = 'pending'""",
                    r["id"],
                )
                params = SweepParams.from_dict(a["params"])
                payloads = [
                    orjson.dumps(
                        {
                            "a": str(a["id"]),
                            "c": t["cell"],
                            "s": t["seed_index"],
                            "h": t["task_hash"],
                            "sys": str(a["system_id"]),
                            "v": a["system_version"],
                            "sh": a["spec_hash"],
                            "sc": params.scenario(t["cell"], t["seed_index"])
                            | {"max_events": self.s.max_events_per_sim},
                            "p": int(a["priority"]),
                            "q": time.time(),
                            "o": str(a["owner_id"]),
                        }
                    )
                    for t in tasks
                ]
                await enqueue(
                    self.redis, tenant_of(int(a["priority"]), str(a["owner_id"])), int(a["priority"]), payloads
                )
                await conn.execute("UPDATE analyses SET started_at = now() WHERE id = $1", r["id"])
            log.warning("re-enqueued stuck analysis", extra={"analysis": str(r["id"]), "tasks": len(payloads)})

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                async with self.db.primary_conn() as conn:
                    await conn.execute("SELECT ensure_activity_partitions()")
                    await conn.execute("DELETE FROM idempotency_keys WHERE created_at < now() - interval '24 hours'")
                    await conn.execute("DELETE FROM refresh_tokens WHERE expires_at < now() - interval '1 day'")
                    await conn.execute("DELETE FROM users WHERE is_guest AND last_seen_at < now() - interval '7 days'")
                    await conn.execute(
                        """DELETE FROM sim_results WHERE task_hash IN (
                               SELECT task_hash FROM sim_results
                               WHERE last_hit_at < now() - interval '30 days' LIMIT 5000)"""
                    )
                    await conn.execute("DELETE FROM outbox WHERE published_at < now() - interval '3 days'")
            except Exception:
                log.exception("maintenance failed")
            await asyncio.sleep(600)
