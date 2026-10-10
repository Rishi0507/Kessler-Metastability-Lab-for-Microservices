from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(us|µs|ms|s|m|h)?\s*$")
_UNITS_MS = {"us": 0.001, "µs": 0.001, "ms": 1.0, "s": 1000.0, "m": 60_000.0, "h": 3_600_000.0}
SERVICE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")


def _duration_ms(v: Any) -> float:
    if isinstance(v, bool):
        raise ValueError("expected a duration like 250ms, 1.5s or 2m")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = _DURATION.match(v)
        if m:
            return float(m.group(1)) * _UNITS_MS[m.group(2) or "ms"]
    raise ValueError("expected a duration like 250ms, 1.5s or 2m")


def _fraction(v: Any) -> float:
    if isinstance(v, bool):
        raise ValueError("expected a ratio like 0.1 or 10%")
    if isinstance(v, str):
        s = v.strip()
        try:
            return float(s[:-1]) / 100.0 if s.endswith("%") else float(s)
        except ValueError:
            raise ValueError("expected a ratio like 0.1 or 10%") from None
    return float(v)


Duration = Annotated[float, BeforeValidator(_duration_ms), Field(ge=0, le=86_400_000)]
Fraction = Annotated[float, BeforeValidator(_fraction), Field(ge=0, le=1)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Latency(Strict):
    p50: Duration | None = None
    p99: Duration | None = None
    mean: Duration | None = None
    constant: Duration | None = None
    min: Duration | None = None
    max: Duration | None = None

    @model_validator(mode="after")
    def _one_form(self) -> Latency:
        forms = [
            self.p50 is not None or self.p99 is not None,
            self.mean is not None,
            self.constant is not None,
            self.min is not None or self.max is not None,
        ]
        if sum(forms) != 1:
            raise ValueError("give exactly one of: p50/p99, mean, constant, or min/max")
        if self.p99 is not None and self.p50 is None:
            raise ValueError("p99 needs p50 as well")
        if self.p50 is not None:
            if self.p50 <= 0:
                raise ValueError("p50 must be > 0")
            if self.p99 is None:
                self.p99 = self.p50 * 4
            if self.p99 < self.p50:
                raise ValueError("p99 must be >= p50")
        if (self.min is None) != (self.max is None):
            raise ValueError("min and max go together")
        if self.min is not None and self.max is not None and self.max < self.min:
            raise ValueError("max must be >= min")
        for v in (self.mean, self.constant):
            if v is not None and v <= 0:
                raise ValueError("latency must be > 0")
        return self

    def to_engine(self) -> dict[str, Any]:
        if self.p50 is not None:
            return {"dist": "lognormal", "p50_ms": self.p50, "p99_ms": self.p99}
        if self.mean is not None:
            return {"dist": "exponential", "mean_ms": self.mean}
        if self.constant is not None:
            return {"dist": "constant", "ms": self.constant}
        return {"dist": "uniform", "min_ms": self.min, "max_ms": self.max}

    def mean_ms(self) -> float:
        import math

        if self.p50 is not None and self.p99 is not None:
            sigma = (math.log(self.p99) - math.log(self.p50)) / 2.3263478740408408
            return self.p50 * math.exp(0.5 * sigma * sigma)
        if self.mean is not None:
            return self.mean
        if self.constant is not None:
            return self.constant
        return ((self.min or 0) + (self.max or 0)) / 2


class Retry(Strict):
    attempts: int = Field(1, ge=1, le=10)
    base: Duration = 50
    cap: Duration = 1000
    jitter: Literal["none", "full", "equal", "decorrelated"] = "full"
    on_timeout: bool = True
    on_error: bool = True

    def to_engine(self) -> dict[str, Any]:
        return {
            "max_attempts": self.attempts,
            "base_ms": self.base,
            "cap_ms": max(self.cap, self.base),
            "jitter": self.jitter,
            "on_timeout": self.on_timeout,
            "on_error": self.on_error,
        }


class Budget(Strict):
    ratio: Fraction = 0.1
    min_per_s: float = Field(10, ge=0, le=1e6)

    def to_engine(self) -> dict[str, Any]:
        return {"ratio": self.ratio, "min_per_s": self.min_per_s}


def _budget_shorthand(v: Any) -> Any:
    if v is None or isinstance(v, (dict, Budget)):
        return v
    return {"ratio": v}


class Breaker(Strict):
    failure_rate: Fraction = 0.5
    window: Duration = 10_000
    min_requests: int = Field(20, ge=1, le=1_000_000)
    open_for: Duration = 5_000
    half_open_probes: int = Field(3, ge=1, le=1000)

    def to_engine(self) -> dict[str, Any]:
        return {
            "failure_rate": max(self.failure_rate, 0.01),
            "window_s": max(self.window / 1000.0, 1.0),
            "min_requests": self.min_requests,
            "open_s": max(self.open_for / 1000.0, 0.1),
            "half_open_probes": self.half_open_probes,
        }


class Call(Strict):
    to: str
    probability: Fraction = 1.0
    timeout: Duration = 1000
    retry: Retry | None = None
    budget: Annotated[Budget | None, BeforeValidator(_budget_shorthand)] = None
    breaker: Breaker | None = None

    @field_validator("timeout")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("timeout must be > 0")
        return v


class Queue(Strict):
    capacity: int = Field(1000, ge=0, le=10_000_000)
    discipline: Literal["fifo", "lifo", "adaptive_lifo"] = "fifo"
    timeout: Duration = 0


class Cache(Strict):
    hit_ratio: Fraction = 0.9
    key_space: int = Field(10_000, ge=1, le=1_000_000_000)
    miss_target: str


class Autoscale(Strict):
    min: int = Field(ge=1, le=1000)
    max: int = Field(ge=1, le=1000)
    target_utilization: Fraction = 0.6
    interval: Duration = 15_000
    provision: Duration = 60_000
    cooldown: Duration = 120_000

    @model_validator(mode="after")
    def _bounds(self) -> Autoscale:
        if self.max < self.min:
            raise ValueError("max must be >= min")
        if self.target_utilization < 0.05:
            raise ValueError("target_utilization must be at least 5%")
        return self


class Contention(Strict):
    knee: Fraction = 0.7
    factor: float = Field(2.0, ge=0, le=100)


class Service(Strict):
    description: str | None = Field(None, max_length=500)
    instances: int = Field(1, ge=1, le=1000)
    concurrency: int = Field(16, ge=1, le=100_000)
    latency: Latency
    queue: Queue = Field(default_factory=Queue)
    error_rate: Fraction = 0
    lb: Literal["round_robin", "random", "least_outstanding", "p2c"] = "round_robin"
    calls: list[Call] = Field(default_factory=list, max_length=32)
    cache: Cache | None = None
    autoscale: Autoscale | None = None
    contention: Contention | None = None


class Client(Strict):
    timeout: Duration = 2000
    slo: Duration | None = None
    retry: Retry | None = None
    budget: Annotated[Budget | None, BeforeValidator(_budget_shorthand)] = None


class Pattern(Strict):
    kind: Literal["constant", "step", "diurnal"] = "constant"
    at: Duration = 0
    factor: float = Field(1.0, ge=0, le=100)
    amplitude: Fraction = 0.3
    period: Duration = 300_000


class Traffic(Strict):
    rps: float = Field(gt=0, le=50_000)
    entry: str | None = None
    pattern: Pattern | None = None
    client: Client = Field(default_factory=Client)


class Resilience(Strict):
    deadline_propagation: bool = False
    cancel_on_timeout: bool = False


class Network(Strict):
    latency: Duration = 0.5
    jitter: Duration = 0.2


class SystemSpec(Strict):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(None, max_length=2000)
    traffic: Traffic
    services: dict[str, Service] = Field(min_length=1, max_length=64)
    resilience: Resilience = Field(default_factory=Resilience)
    network: Network = Field(default_factory=Network)

    @model_validator(mode="after")
    def _references(self) -> SystemSpec:
        names = list(self.services)
        for n in names:
            if not SERVICE_NAME.match(n) or n == "client":
                raise ValueError(
                    f"service name '{n}' must start with a letter and use lowercase letters, digits, '-' or '_' "
                    "(and 'client' is reserved)"
                )
        entry = self.traffic.entry or names[0]
        if entry not in self.services:
            raise ValueError(f"traffic.entry: unknown service '{entry}'")
        self.traffic.entry = entry
        for name, svc in self.services.items():
            for i, call in enumerate(svc.calls):
                if call.to not in self.services:
                    raise ValueError(f"services.{name}.calls[{i}].to: unknown service '{call.to}'")
                if call.to == name:
                    raise ValueError(f"services.{name}.calls[{i}].to: a service cannot call itself")
            if svc.cache and svc.cache.miss_target not in {c.to for c in svc.calls}:
                raise ValueError(f"services.{name}.cache.miss_target must be one of this service's calls")
            if svc.autoscale and not (svc.autoscale.min <= svc.instances <= svc.autoscale.max):
                raise ValueError(f"services.{name}.instances must be within autoscale min..max")
        cycle = _find_cycle({n: [c.to for c in s.calls] for n, s in self.services.items()})
        if cycle:
            raise ValueError("call graph has a cycle: " + " → ".join(cycle))
        return self


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def dfs(u: str) -> list[str] | None:
        state[u] = 1
        stack.append(u)
        for v in graph.get(u, []):
            if state.get(v) == 1:
                return stack[stack.index(v) :] + [v]
            if v not in state:
                found = dfs(v)
                if found:
                    return found
        stack.pop()
        state[u] = 2
        return None

    for node in graph:
        if node not in state:
            found = dfs(node)
            if found:
                return found
    return None
