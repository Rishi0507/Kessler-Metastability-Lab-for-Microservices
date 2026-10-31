from __future__ import annotations

import asyncio
import json
import logging

import pytest

from kessler.heartbeat import supervise
from kessler.logs import JsonFormatter, get_logger


def test_a_log_call_with_reserved_extra_keys_never_raises():
    log = get_logger("kessler.test.safe")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("reclaimed orphaned task", extra={"msg": "1-0", "name": "x", "ok": 1})
    finally:
        log.removeHandler(handler)
    doc = json.loads(JsonFormatter().format(records[0]))
    assert doc["msg"] == "reclaimed orphaned task"
    assert doc["extra_msg"] == "1-0" and doc["extra_name"] == "x" and doc["ok"] == 1
    assert type(log).__name__ == "SafeLogger"


async def test_supervised_loops_restart_after_errors():
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("boom")

    await asyncio.wait_for(supervise(flaky, "flaky", get_logger("kessler.test"), first_delay_s=0.01), 5)
    assert calls == 3


async def test_supervision_does_not_swallow_cancellation():
    async def forever() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(supervise(forever, "forever", get_logger("kessler.test")))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
