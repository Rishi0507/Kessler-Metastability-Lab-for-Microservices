from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import orjson
import redis.asyncio as aioredis

from .. import keys
from ..config import Settings
from ..db import Database
from ..engine import LiveSim, SpecInvalid, get_engine
from ..logs import get_logger
from ..metrics import LIVE_CONTROL_RTT, LIVE_FAILOVERS, LIVE_RUNS, LIVE_TICK_LAG
from ..redisio import LEASE_RELEASE, LEASE_RENEW
from ..spec import build_engine_spec

log = get_logger("kessler.live")

LEASE_MS = 5_000
TICKS_MAXLEN = 600


class LiveRun:
    def __init__(self, s: Settings, db: Database, redis: aioredis.Redis, run_id: str, resume: bool) -> None:
        self.s = s
        self.db = db
        self.redis = redis
        self.run_id = run_id
        self.resume = resume
        self.topic = f"run:{run_id}"
        self.token = 0
        self.lease_value = b""
        self._renew = redis.register_script(LEASE_RENEW)
        self._release = redis.register_script(LEASE_RELEASE)

    async def _emit(self, event: dict[str, Any]) -> None:
        data = orjson.dumps({**event, "token": self.token, "ts": time.time()})
        await self._emit_raw(data)

    async def _emit_raw(self, data: bytes) -> None:
        sid = await self.redis.xadd(keys.run_ticks(self.run_id), {b"e": data}, maxlen=TICKS_MAXLEN, approximate=True)
        sid_s = sid.decode() if isinstance(sid, bytes) else str(sid)
        envelope = b'{"topic":"' + self.topic.encode() + b'","id":"' + sid_s.encode() + b'","event":' + data + b"}"
        await self.redis.publish(keys.channel(self.topic), envelope)

    async def run(self) -> None:
        self.token = int(await self.redis.incr(keys.run_fence(self.run_id)))
        self.lease_value = f"{self.s.instance}:{self.token}".encode()
        if not await self.redis.set(keys.run_lease(self.run_id), self.lease_value, nx=True, px=LEASE_MS):
            return
        try:
            await self._run_owned()
        finally:
            await self._release(keys=[keys.run_lease(self.run_id)], args=[self.lease_value])

    async def _run_owned(self) -> None:
        rid = uuid.UUID(self.run_id)
        async with self.db.primary_conn() as conn:
            run = await conn.fetchrow("SELECT * FROM runs WHERE id = $1", rid)
            if run is None or str(run["status"]) not in ("queued", "running"):
                return
            spec_json = await conn.fetchval(
                "SELECT spec_json FROM system_versions WHERE system_id = $1 AND version = $2",
                run["system_id"],
                run["system_version"],
            )
            commands = await conn.fetch(
                "SELECT seq, sim_time, command FROM run_commands WHERE run_id = $1 ORDER BY seq", rid
            )
            claimed = await conn.fetchval(
                """UPDATE runs SET status = 'running', worker = $2, lease_token = $3,
                          started_at = COALESCE(started_at, now())
                   WHERE id = $1 AND lease_token < $3 RETURNING 1""",
                rid,
                self.s.instance,
                self.token,
            )
        if not claimed:
            return
        p = run["params"]
        spec = build_engine_spec(
            spec_json,
            {"seed": p["seed"], "horizon_s": p["horizon_s"], "warmup_s": 0, "load": p["load"], "faults": []},
        )
        engine = get_engine()
        sim: LiveSim = engine.live(spec, emit_frames=True)
        LIVE_RUNS.inc()
        try:
            seq = 0
            if self.resume or commands:
                for c in commands:
                    await asyncio.to_thread(sim.advance, float(c["sim_time"]))
                    try:
                        sim.command(c["command"])
                    except SpecInvalid:
                        pass
                    seq = int(c["seq"])
                target = max(float(run["sim_time"] or 0.0), sim.now)
                await asyncio.to_thread(sim.advance, target)
                frames = sim.take_frames()
                if self.resume:
                    LIVE_FAILOVERS.inc()
                    log.info(
                        "live run resumed by replay",
                        extra={"run": self.run_id, "sim_time": target, "commands": len(commands)},
                    )
                if frames:
                    await self._emit({"type": "frames", "frames": frames[-5:], "replayed": True})
            await self._emit({"type": "started", "sim_time": sim.now, "resumed": self.resume, "speed": p["speed"]})
            await self._loop(sim, rid, float(p["speed"]), seq)
        finally:
            LIVE_RUNS.dec()
            sim.close()

    async def _loop(self, sim: LiveSim, rid: uuid.UUID, speed: float, seq: int) -> None:
        tick = self.s.live_tick_s
        ctl_key = keys.run_ctl(self.run_id)
        async with self.db.primary_conn() as conn:
            last = await conn.fetchval(
                "SELECT command->>'_ctl' FROM run_commands WHERE run_id = $1 ORDER BY seq DESC LIMIT 1", rid
            )
        ctl_last = last.encode() if last else b"0-0"
        persisted_at = time.monotonic()
        next_tick = time.monotonic()
        while True:
            started = time.monotonic()
            LIVE_TICK_LAG.observe(max(0.0, started - next_tick))
            next_tick = started + tick
            running = await asyncio.to_thread(sim.advance, sim.now + speed * tick)
            raw = sim.take_frames_raw()
            if raw != b"[]":
                await self._emit_raw(
                    b'{"type":"frames","token":'
                    + str(self.token).encode()
                    + b',"frames":'
                    + raw
                    + b',"ts":'
                    + str(time.time()).encode()
                    + b"}"
                )
            resp = await self.redis.xread({ctl_key: ctl_last}, count=50)
            stop = False
            for _, entries in resp or []:
                for msg_id, fields in entries:
                    ctl_last = msg_id
                    cmd = orjson.loads(fields.get(b"cmd", b"{}"))
                    req = fields.get(b"req", b"").decode()
                    sent = float(fields.get(b"ts", b"0") or 0)
                    if cmd.get("op") == "stop":
                        stop = True
                        continue
                    ok, err = True, None
                    try:
                        sim.command(cmd)
                    except SpecInvalid as exc:
                        ok, err = False, str(exc)
                    seq += 1
                    async with self.db.primary_conn() as conn:
                        await conn.execute(
                            """INSERT INTO run_commands (run_id, seq, sim_time, command, issued_by)
                               SELECT $1, $2, $3, $4, $5 WHERE EXISTS
                                 (SELECT 1 FROM runs WHERE id = $1 AND lease_token = $6)""",
                            rid,
                            seq,
                            sim.now,
                            {**cmd, "_ctl": msg_id.decode()},
                            uuid.UUID(fields[b"user"].decode()) if fields.get(b"user") else None,
                            self.token,
                        )
                    if sent:
                        LIVE_CONTROL_RTT.observe(max(0.0, time.time() - sent))
                    await self._emit(
                        {"type": "ack", "req": req, "ok": ok, "error": err, "sim_time": sim.now, "command": cmd}
                    )
            if not await self._renew(keys=[keys.run_lease(self.run_id)], args=[self.lease_value, LEASE_MS]):
                log.warning("lost live-run lease; stopping", extra={"run": self.run_id, "token": self.token})
                return
            if time.monotonic() - persisted_at > 2.0 or stop or not running:
                persisted_at = time.monotonic()
                async with self.db.primary_conn() as conn:
                    await conn.execute(
                        "UPDATE runs SET sim_time = $2 WHERE id = $1 AND lease_token = $3", rid, sim.now, self.token
                    )
            if stop or not running:
                summary = sim.summary(include_series=True)
                async with self.db.primary_conn() as conn:
                    await conn.execute(
                        """UPDATE runs SET status = 'done', summary = $2, ended_at = now(), sim_time = $3
                           WHERE id = $1 AND lease_token = $4""",
                        rid,
                        summary,
                        sim.now,
                        self.token,
                    )
                await self._emit({"type": "ended", "reason": "stopped" if stop else "horizon", "summary": summary})
                return
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
