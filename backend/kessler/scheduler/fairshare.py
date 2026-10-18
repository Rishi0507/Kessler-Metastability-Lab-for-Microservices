from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

WEIGHTS = {1: 4.0, 2: 1.0}


@dataclass
class DeficitRoundRobin:
    quantum: float = 1.0
    deficits: dict[str, float] = field(default_factory=dict)
    _ring: deque[str] = field(default_factory=deque)

    def sync(self, active: dict[str, int]) -> None:
        for t in list(self.deficits):
            if t not in active:
                del self.deficits[t]
        self._ring = deque(t for t in self._ring if t in active)
        for t in active:
            if t not in self.deficits:
                self.deficits[t] = 0.0
                self._ring.append(t)

    def allocate(self, active: dict[str, int], slots: int, backlog: dict[str, int]) -> list[str]:
        self.sync(active)
        out: list[str] = []
        remaining = dict(backlog)
        top = max((WEIGHTS.get(p, 1.0) for p in active.values()), default=1.0)
        guard = 0
        while slots > 0 and self._ring and guard < 10_000:
            guard += 1
            t = self._ring[0]
            self._ring.rotate(-1)
            if remaining.get(t, 0) <= 0:
                continue
            self.deficits[t] += self.quantum * WEIGHTS.get(active[t], 1.0) / top
            while self.deficits[t] >= 1.0 and slots > 0 and remaining.get(t, 0) > 0:
                out.append(t)
                self.deficits[t] -= 1.0
                remaining[t] -= 1
                slots -= 1
            if remaining.get(t, 0) <= 0:
                self.deficits[t] = 0.0
            if not any(remaining.get(x, 0) > 0 for x in self._ring):
                break
        return out


def jain_index(shares: list[float]) -> float | None:
    xs = [x for x in shares if x >= 0]
    if not xs or sum(xs) == 0:
        return None
    return (sum(xs) ** 2) / (len(xs) * sum(x * x for x in xs))


@dataclass
class FairnessTracker:
    window_s: float = 60.0
    _events: deque[tuple[float, str, float]] = field(default_factory=deque)

    def record(self, tenant: str, weight: float, now: float | None = None) -> None:
        self._events.append((now or time.monotonic(), tenant, weight))

    def index(self, backlogged: set[str], now: float | None = None) -> float | None:
        now = now or time.monotonic()
        while self._events and now - self._events[0][0] > self.window_s:
            self._events.popleft()
        if len(backlogged) < 2:
            return None
        got: dict[str, float] = {t: 0.0 for t in backlogged}
        for _, t, w in self._events:
            if t in got:
                got[t] += 1.0 / w
        return jain_index(list(got.values()))
