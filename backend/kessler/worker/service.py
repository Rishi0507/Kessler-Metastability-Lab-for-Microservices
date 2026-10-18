from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import orjson
import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from .. import keys
from ..config import Settings
from ..db import Database
from ..engine import EngineError, SpecInvalid, get_engine
from ..events import compact_summary, emit_analysis
from ..heartbeat import cancel, heartbeat_loop
from ..logs import get_logger
from ..metrics import QUEUE_WAIT, SIM_EVENTS, SIM_SECONDS, SIMS, WORKER_BUSY
from ..redisio import create_redis
from ..scheduler.service import ensure_group
from ..spec import build_engine_spec
from ..tracing import span
from .live import LiveRun

log = get_logger("kessler.worker")


class Worker:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.slots = max(1, s.worker_threads)
        self.pool = ThreadPoolExecutor(max_workers=self.slots, thread_name_prefix="sim")
        self.sem = asyncio.Semaphore(self.slots)
        self.live_sem = asyncio.Semaphore(s.worker_live_slots)
        self.db = Database(s, f"kessler-worker-{s.instance}")
        self.redis: aioredis.Redis | None = None
        self.consumer = f"w-{s.instance}"
        self.engine = get_engine()
        self._specs: OrderedDict[tuple[str, int], dict[str, Any]] = OrderedDict()
        self._busy = 0
        self._events_window: list[tuple[float, int]] = []
        self._live: set[asyncio.Task[Any]] = set()
        self._inflight: set[asyncio.Task[Any]] = set()

    def stats(self) -> dict[str, Any]:
        now = time.monotonic()
        self._events_window = [(t, n) for t, n in self._events_window if now - t < 30]
        evs = sum(n for _, n in self._events_window) / 30.0
        return {
            "slots": self.slots,
            "busy": self._busy,
            "live_runs": len(self._live),
            "events_per_s": round(evs),
            "engine": self.engine.version,
        }

    async def run(self) -> None:
        await self.db.connect()
        self.redis = create_redis(self.s.redis_url)
        for stream, group in (
            (keys.STREAM_TASKS, keys.GROUP_WORKERS),
            (keys.STREAM_RESULTS, keys.GROUP_SCHEDULER),
            (keys.STREAM_LIVE, keys.GROUP_LIVE),
        ):
            await ensure_group(self.redis, stream, group)
        hb = asyncio.create_task(heartbeat_loop(self.redis, "worker", self.s.instance, self.stats))
        loops = [
            asyncio.create_task(self._task_loop()),
            asyncio.create_task(self._claim_loop()),
            asyncio.create_task(self._live_loop()),
        ]
        log.info("worker ready", extra={"slots": self.slots, "engine": self.engine.version})
        try:
            await asyncio.gather(*loops)
        finally:
            await cancel(hb, *loops)
            if self._inflight:
                await asyncio.wait(self._inflight, timeout=30)
            self.pool.shutdown(wait=False, cancel_futures=True)
            await self.redis.aclose()
            await self.db.close()

    async def _task_loop(self) -> None:
        assert self.redis is not None
        while True:
            await self.sem.acquire()
            try:
                resp = await self.redis.xreadgroup(
                    keys.GROUP_WORKERS, self.consumer, {keys.STREAM_TASKS: ">"}, count=1, block=2000
                )
            except Exception:
                self.sem.release()
                await asyncio.sleep(1.0)
                continue
            if not resp or not resp[0][1]:
                self.sem.release()
                continue
            msg_id, fields = resp[0][1][0]
            self._spawn(msg_id, fields)

    def _spawn(self, msg_id: bytes, fields: dict[bytes, bytes]) -> None:
        t = asyncio.create_task(self._process(msg_id, fields))
        self._inflight.add(t)

        def done(task: asyncio.Task[Any]) -> None:
            self._inflight.discard(task)
            self.sem.release()

        t.add_done_callback(done)

    async def _claim_loop(self) -> None:
        assert self.redis is not None
        idle_ms = int(self.s.task_claim_idle_s * 1000)
        while True:
            await asyncio.sleep(10.0)
            if self.sem.locked():
                continue
            try:
                _, claimed, _ = await self.redis.xautoclaim(
                    keys.STREAM_TASKS, keys.GROUP_WORKERS, self.consumer, min_idle_time=idle_ms, start_id="0-0", count=4
                )
            except ResponseError:
                continue
            for msg_id, fields in claimed:
                if not fields:
                    continue
                await self.sem.acquire()
                log.info("reclaimed orphaned task", extra={"msg": msg_id.decode()})
                self._spawn(msg_id, fields)

    async def _spec_json(self, system_id: str, version: int) -> dict[str, Any]:
        key = (system_id, version)
        if key in self._specs:
            self._specs.move_to_end(key)
            return self._specs[key]
        async with self.db.read() as conn:
            spec = await conn.fetchval(
                "SELECT spec_json FROM system_versions WHERE system_id = $1 AND version = $2",
                uuid.UUID(system_id),
                version,
            )
        if spec is None:
            raise SpecInvalid("system version not found")
        self._specs[key] = spec
        if len(self._specs) > 256:
            self._specs.popitem(last=False)
        return spec

    async def _process(self, msg_id: bytes, fields: dict[bytes, bytes]) -> None:
        assert self.redis is not None
        task = orjson.loads(fields.get(b"task", b"{}"))
        aid = task.get("a")
        if not aid or await self.redis.exists(f"{keys.PREFIX}:cancelled:{aid}"):
            await self.redis.xack(keys.STREAM_TASKS, keys.GROUP_WORKERS, msg_id)
            return
        QUEUE_WAIT.labels(f"{task.get('p', 1)}-start").observe(
            max(0.0, time.time() - float(task.get("q", time.time())))
        )
        self._busy += 1
        WORKER_BUSY.set(self._busy)
        try:
            with span("simulate", analysis=aid, cell=task["c"], seed=task["s"]):
                try:
                    spec = build_engine_spec(await self._spec_json(task["sys"], int(task["v"])), task["sc"])
                    t0 = time.perf_counter()
                    summary = await asyncio.get_running_loop().run_in_executor(self.pool, self.engine.run, spec)
                    wall = time.perf_counter() - t0
                except (SpecInvalid, EngineError) as exc:
                    await self._fail(task, msg_id, str(exc))
                    return
            SIMS.labels("executed").inc()
            SIM_SECONDS.observe(wall)
            SIM_EVENTS.inc(int(summary.get("events", 0)))
            self._events_window.append((time.monotonic(), int(summary.get("events", 0))))
            compact = compact_summary(summary)
            first = False
            ttfi = None
            async with self.db.write() as conn:
                await conn.execute(
                    """INSERT INTO sim_results (task_hash, engine_version, summary, events, wall_ms)
                       VALUES ($1, $2, $3, $4, $5) ON CONFLICT (task_hash) DO NOTHING""",
                    task["h"],
                    self.engine.version,
                    summary,
                    int(summary.get("events", 0)),
                    float(wall * 1000.0),
                )
                changed = await conn.fetchval(
                    """UPDATE analysis_tasks SET status = 'done', summary = $4, finished_at = now()
                       WHERE analysis_id = $1 AND cell = $2 AND seed_index = $3 AND status <> 'done' RETURNING 1""",
                    uuid.UUID(aid),
                    int(task["c"]),
                    int(task["s"]),
                    compact,
                )
                if changed:
                    row = await conn.fetchrow(
                        """UPDATE analyses SET sims_done = sims_done + 1,
                                  first_result_at = COALESCE(first_result_at, now())
                           WHERE id = $1 RETURNING created_at, first_result_at, first_result_at = now() AS first""",
                        uuid.UUID(aid),
                    )
                    if row and row["first"]:
                        first = True
                        ttfi = (row["first_result_at"] - row["created_at"]).total_seconds()
            if changed:
                await emit_analysis(
                    self.redis,
                    aid,
                    {"type": "task", "cell": task["c"], "seed": task["s"], "cached": False, "summary": compact},
                )
                fields_out = {b"a": aid.encode(), b"c": str(task["c"]).encode()}
                if first and ttfi is not None:
                    fields_out[b"first"] = b"1"
                    fields_out[b"ttfi"] = str(ttfi).encode()
                await self.redis.xadd(keys.STREAM_RESULTS, fields_out)
            await self.redis.xack(keys.STREAM_TASKS, keys.GROUP_WORKERS, msg_id)
        finally:
            self._busy -= 1
            WORKER_BUSY.set(self._busy)

    async def _fail(self, task: dict[str, Any], msg_id: bytes, error: str) -> None:
        assert self.redis is not None
        SIMS.labels("failed").inc()
        async with self.db.write() as conn:
            changed = await conn.fetchval(
                """UPDATE analysis_tasks SET status = 'failed', finished_at = now(), summary = $4
                   WHERE analysis_id = $1 AND cell = $2 AND seed_index = $3 AND status NOT IN ('done', 'failed')
                   RETURNING 1""",
                uuid.UUID(task["a"]),
                int(task["c"]),
                int(task["s"]),
                {"verdict": "failed", "error": error[:500]},
            )
        if changed:
            await self.redis.xadd(keys.STREAM_RESULTS, {b"a": task["a"].encode(), b"c": str(task["c"]).encode()})
        await self.redis.xack(keys.STREAM_TASKS, keys.GROUP_WORKERS, msg_id)
        log.warning("simulation failed", extra={"analysis": task["a"], "error": error})

    async def _live_loop(self) -> None:
        assert self.redis is not None
        while True:
            await self.live_sem.acquire()
            try:
                resp = await self.redis.xreadgroup(
                    keys.GROUP_LIVE, self.consumer, {keys.STREAM_LIVE: ">"}, count=1, block=2000
                )
            except Exception:
                self.live_sem.release()
                await asyncio.sleep(1.0)
                continue
            if not resp or not resp[0][1]:
                self.live_sem.release()
                continue
            msg_id, fields = resp[0][1][0]
            await self.redis.xack(keys.STREAM_LIVE, keys.GROUP_LIVE, msg_id)
            run = LiveRun(self.s, self.db, self.redis, fields[b"run"].decode(), fields.get(b"resume") == b"1")
            t = asyncio.create_task(self._live_guard(run))
            self._live.add(t)

            def done(task: asyncio.Task[Any]) -> None:
                self._live.discard(task)
                self.live_sem.release()

            t.add_done_callback(done)

    async def _live_guard(self, run: LiveRun) -> None:
        try:
            await run.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("live run crashed", extra={"run": run.run_id})
