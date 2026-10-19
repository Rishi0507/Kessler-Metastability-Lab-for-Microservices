from __future__ import annotations

import time

import orjson
import pytest

from kessler.gateway.hub import RELIABLE_LIMIT, Hub, Message, Subscriber, parse_id
from kessler.scheduler.fairshare import DeficitRoundRobin, FairnessTracker, jain_index


def env(topic: str, sid: str, event: dict) -> Message:
    raw = orjson.dumps({"topic": topic, "id": sid, "event": event})
    return Message.from_envelope(raw)


def test_stream_ids_compare_numerically():
    assert parse_id("1700000000000-10") > parse_id("1700000000000-9")
    assert parse_id("1700000000001-0") > parse_id("1700000000000-99")


def test_hub_refcounts_redis_subscriptions():
    hub, a, b = Hub(), Subscriber("u1"), Subscriber("u2")
    assert hub.add(a, "analysis:1") is True
    assert hub.add(b, "analysis:1") is False
    assert hub.remove(a, "analysis:1") is False
    assert hub.remove(b, "analysis:1") is True


def test_reliable_events_are_delivered_once_in_order():
    hub, s = Hub(), Subscriber("u")
    hub.add(s, "analysis:1")
    for i in range(1, 4):
        hub.publish(env("analysis:1", f"100-{i}", {"type": "task", "cell": i}))
    hub.publish(env("analysis:1", "100-2", {"type": "task", "cell": 2}))
    got = [orjson.loads(m.raw)["event"]["cell"] for m in s.drain()]
    assert got == [1, 2, 3]


def test_live_frames_are_conflated_for_slow_clients():
    hub, s = Hub(), Subscriber("u")
    hub.add(s, "run:9")
    for i in range(1, 6):
        hub.publish(env("run:9", f"200-{i}", {"type": "frames", "token": 1, "n": i}))
    hub.publish(env("run:9", "200-6", {"type": "ack", "token": 1, "req": "x"}))
    msgs = s.drain()
    kinds = [m.kind for m in msgs]
    assert kinds.count("frames") == 1
    assert orjson.loads(next(m.raw for m in msgs if m.kind == "frames"))["event"]["n"] == 5
    assert "ack" in kinds


def test_fencing_drops_frames_from_a_stale_owner():
    hub, s = Hub(), Subscriber("u")
    hub.add(s, "run:9")
    hub.publish(env("run:9", "300-1", {"type": "frames", "token": 2, "n": 1}))
    assert hub.publish(env("run:9", "300-2", {"type": "frames", "token": 1, "n": 2})) == 0
    assert orjson.loads(s.drain()[0].raw)["event"]["n"] == 1


def test_backfill_then_buffered_live_messages_without_gaps_or_duplicates():
    hub, s = Hub(), Subscriber("u")
    s.backfilling["analysis:1"] = []
    hub.add(s, "analysis:1")
    hub.publish(env("analysis:1", "500-3", {"type": "task", "cell": 3}))
    backfill = [env("analysis:1", f"500-{i}", {"type": "task", "cell": i}) for i in (1, 2, 3)]
    buffered = s.backfilling.pop("analysis:1")
    for m in backfill:
        s.offer(m)
    for m in buffered:
        s.offer(m)
    assert [orjson.loads(m.raw)["event"]["cell"] for m in s.drain()] == [1, 2, 3]


def test_slow_consumer_is_flagged_instead_of_buffering_forever():
    hub, s = Hub(), Subscriber("u")
    hub.add(s, "analysis:1")
    for i in range(RELIABLE_LIMIT + 5):
        hub.publish(env("analysis:1", f"600-{i}", {"type": "task", "cell": i}))
    assert s.slow is True
    assert len(s.reliable) == RELIABLE_LIMIT


def test_fanout_latency_is_measured_from_producer_timestamp():
    hub, s = Hub(), Subscriber("u")
    hub.add(s, "analysis:1")
    hub.publish(env("analysis:1", "700-1", {"type": "task", "ts": time.time() - 0.05}))
    assert 40 <= hub.fanout_p99_ms() < 1000


def test_jain_index_bounds():
    assert jain_index([1, 1, 1, 1]) == pytest.approx(1.0)
    assert jain_index([1, 0, 0, 0]) == pytest.approx(0.25)
    assert jain_index([]) is None


def test_drr_shares_slots_evenly_regardless_of_backlog_size():
    drr = DeficitRoundRobin()
    active = {"1:alice": 1, "1:bob": 1}
    backlog = {"1:alice": 500, "1:bob": 3}
    order = drr.allocate(active, slots=6, backlog=backlog)
    assert order.count("1:bob") == 3
    assert order.count("1:alice") == 3


def test_drr_weights_interactive_over_batch():
    drr = DeficitRoundRobin()
    active = {"1:human": 1, "2:ci": 2}
    backlog = {"1:human": 100, "2:ci": 100}
    order = drr.allocate(active, slots=50, backlog=backlog)
    assert order.count("1:human") == pytest.approx(40, abs=4)
    assert order.count("2:ci") == pytest.approx(10, abs=4)


def test_drr_does_not_bank_credit_for_idle_tenants():
    drr = DeficitRoundRobin()
    drr.allocate({"1:a": 1}, slots=1, backlog={"1:a": 1})
    order = drr.allocate({"1:a": 1, "1:b": 1}, slots=4, backlog={"1:a": 10, "1:b": 10})
    assert abs(order.count("1:a") - order.count("1:b")) <= 1


def test_fairness_tracker_scores_weighted_shares():
    ft = FairnessTracker()
    for _ in range(40):
        ft.record("1:human", 4.0, now=100.0)
    for _ in range(10):
        ft.record("2:ci", 1.0, now=100.0)
    assert ft.index({"1:human", "2:ci"}, now=101.0) == pytest.approx(1.0)
