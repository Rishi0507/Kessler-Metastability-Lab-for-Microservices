from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    description: str
    tags: tuple[str, ...]
    trigger: dict[str, Any]
    yaml: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "trigger": self.trigger,
            "yaml": self.yaml,
        }


_META: dict[str, dict[str, Any]] = {
    "checkout-retry-storm": {
        "tags": ("retry storm", "wasted work"),
        "trigger": {"kind": "latency", "target": "db", "duration_s": 20},
    },
    "catalog-cold-cache": {
        "tags": ("cold cache", "congestive collapse"),
        "trigger": {"kind": "cache_flush", "target": "cache", "duration_s": 0},
    },
    "bursty-autoscaling": {
        "tags": ("traffic spike", "autoscaling lag"),
        "trigger": {"kind": "spike", "duration_s": 30},
    },
    "checkout-hardened": {
        "tags": ("budgets", "deadlines", "after the fix"),
        "trigger": {"kind": "latency", "target": "db", "duration_s": 20},
    },
    "kessler-self": {
        "tags": ("dogfooding", "connection pool"),
        "trigger": {"kind": "latency", "target": "postgres", "duration_s": 20},
    },
    "marketplace-at-scale": {
        "tags": ("45 services", "shared databases", "fan-out"),
        "trigger": {"kind": "latency", "target": "inventory-db", "duration_s": 20},
    },
    "aws-dynamodb-2015": {
        "tags": ("real outage", "retry storm", "timeouts"),
        "trigger": {"kind": "latency", "target": "metadata", "duration_s": 10},
    },
    "facebook-2010": {
        "tags": ("real outage", "cold cache", "feedback loop"),
        "trigger": {"kind": "cache_flush", "target": "config-cache", "duration_s": 0},
    },
}


@lru_cache(maxsize=1)
def all_templates() -> tuple[Template, ...]:
    import yaml

    out = []
    for tid, meta in _META.items():
        text = (_DIR / f"{tid}.yaml").read_text(encoding="utf-8")
        doc = yaml.safe_load(text)
        out.append(
            Template(
                id=tid,
                name=doc["name"],
                description=" ".join(str(doc.get("description", "")).split()),
                tags=meta["tags"],
                trigger=meta["trigger"],
                yaml=text,
            )
        )
    return tuple(out)


def get_template(tid: str) -> Template | None:
    return next((t for t in all_templates() if t.id == tid), None)
