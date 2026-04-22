"""Unit tests for PersistentQueue (D-03)."""
import asyncio
import threading
import time

import pytest

from custom_components.ha_timescaledb_recorder.persistent_queue import PersistentQueue


def test_put_get_task_done_roundtrip(tmp_path):
    q = PersistentQueue(tmp_path / "q.jsonl")
    q.put({"k": 1})
    q.put({"k": 2})
    assert q.get() == {"k": 1}
    q.task_done()
    assert q.get() == {"k": 2}
    q.task_done()


def test_crash_replay_survives_new_instance(tmp_path):
    """D-03-h: item persists on disk until task_done()."""
    path = tmp_path / "q.jsonl"
    q1 = PersistentQueue(path)
    q1.put({"id": "one"})
    q1.put({"id": "two"})
    item = q1.get()
    assert item == {"id": "one"}
    # simulate crash: do NOT call task_done; drop q1
    del q1
    q2 = PersistentQueue(path)
    # Same item appears because disk still carries it
    assert q2.get() == {"id": "one"}
    q2.task_done()
    assert q2.get() == {"id": "two"}
    q2.task_done()


def test_task_done_atomically_removes_front_line(tmp_path):
    path = tmp_path / "q.jsonl"
    q = PersistentQueue(path)
    q.put({"a": 1})
    q.put({"a": 2})
    q.put({"a": 3})
    q.get()       # returns {"a": 1}
    q.task_done()
    # File now has exactly 2 lines (items 2 and 3)
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2
    assert '"a": 2' in lines[0]
    assert '"a": 3' in lines[1]


def test_wake_consumer_returns_none_on_empty(tmp_path):
    path = tmp_path / "q.jsonl"
    q = PersistentQueue(path)
    result = {}

    def consumer():
        result["got"] = q.get()  # blocks until wake_consumer

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.1)  # ensure consumer enters wait
    q.wake_consumer()
    t.join(timeout=2)
    assert not t.is_alive()
    assert result["got"] is None


async def test_put_async_and_join(tmp_path):
    path = tmp_path / "q.jsonl"
    q = PersistentQueue(path)
    await q.put_async({"x": 42})
    # Consumer drains
    item = q.get()
    assert item == {"x": 42}
    q.task_done()
    # join blocks until empty + no in-flight
    await asyncio.wait_for(q.join(), timeout=2)


def test_datetime_serialization_via_default_str(tmp_path):
    """datetime values become ISO strings via json.dumps(default=str).

    Consumers (meta_worker._rehydrate_params) reverse via fromisoformat.
    """
    from datetime import datetime, timezone
    path = tmp_path / "q.jsonl"
    q = PersistentQueue(path)
    dt = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    # put() uses json.dumps(..., default=str); datetime → str(datetime)
    q.put({"when": dt})
    text = path.read_text()
    assert "2026-04-21" in text
