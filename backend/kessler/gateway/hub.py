from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import orjson

from ..metrics import WS_CONFLATED, WS_FANOUT_LATENCY, WS_SLOW_DISCONNECTS

RELIABLE_LIMIT = 2_000


def parse_id(sid: str) -> tuple[int, int]:
    ms, _, seq = sid.partition("-")
    return int(ms or 0), int(seq or 0)


@dataclass
class Message:
    topic: str
    id: str
    raw: bytes
    kind: str
    ts: float | None = None
    token: int | None = None

    @staticmethod
    def from_envelope(raw: bytes) -> Message:
        env = orjson.loads(raw)
        ev = env.get("event") or {}
        tok = ev.get("token")
        return Message(
            env["topic"], env["id"], raw, str(ev.get("type", "")), ev.get("ts"), int(tok) if tok is not None else None
        )


@dataclass(eq=False)
class Subscriber:
    user_id: str
    reliable: deque[Message] = field(default_factory=deque)
    frames: dict[str, Message] = field(default_factory=dict)
    last_id: dict[str, tuple[int, int]] = field(default_factory=dict)
    backfilling: dict[str, list[Message]] = field(default_factory=dict)
    perms: dict[str, str] = field(default_factory=dict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False
    slow: bool = False

    def offer(self, m: Message) -> None:
        if self.closed:
            return
        if m.topic in self.backfilling:
            self.backfilling[m.topic].append(m)
            return
        pid = parse_id(m.id)
        if pid <= self.last_id.get(m.topic, (-1, -1)):
            return
        self.last_id[m.topic] = pid
        if m.kind == "frames":
            if m.topic in self.frames:
                WS_CONFLATED.inc()
            self.frames[m.topic] = m
        else:
            if len(self.reliable) >= RELIABLE_LIMIT:
                self.slow = True
                WS_SLOW_DISCONNECTS.inc()
                self.wake.set()
                return
            self.reliable.append(m)
        self.wake.set()

    def drain(self) -> list[Message]:
        out = list(self.reliable)
        self.reliable.clear()
        out.extend(self.frames.values())
        self.frames.clear()
        self.wake.clear()
        return out


class Hub:
    def __init__(self) -> None:
        self.topics: dict[str, set[Subscriber]] = {}
        self.max_token: dict[str, int] = {}
        self.latency_samples: deque[float] = deque(maxlen=2000)

    def add(self, sub: Subscriber, topic: str) -> bool:
        subs = self.topics.setdefault(topic, set())
        first = not subs
        subs.add(sub)
        return first

    def remove(self, sub: Subscriber, topic: str) -> bool:
        subs = self.topics.get(topic)
        if not subs:
            return False
        subs.discard(sub)
        if not subs:
            del self.topics[topic]
            self.max_token.pop(topic, None)
            return True
        return False

    def publish(self, m: Message) -> int:
        if m.token is not None:
            top = self.max_token.get(m.topic, 0)
            if m.token < top:
                return 0
            self.max_token[m.topic] = m.token
        subs = self.topics.get(m.topic)
        if not subs:
            return 0
        if m.ts:
            lat = max(0.0, time.time() - float(m.ts))
            WS_FANOUT_LATENCY.observe(lat)
            self.latency_samples.append(lat)
        for s in subs:
            s.offer(m)
        return len(subs)

    def fanout_p99_ms(self) -> float:
        if not self.latency_samples:
            return 0.0
        xs = sorted(self.latency_samples)
        return round(xs[min(len(xs) - 1, int(0.99 * len(xs)))] * 1000.0, 2)

    def stats(self) -> dict[str, Any]:
        return {"topics": len(self.topics), "subscriptions": sum(len(s) for s in self.topics.values())}
