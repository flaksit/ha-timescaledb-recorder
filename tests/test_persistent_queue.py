"""Unit tests for PersistentQueue (D-03)."""
import asyncio
import logging
import threading
import time
from unittest.mock import patch

import pytest

from custom_components.timescaledb_recorder.persistent_queue import PersistentQueue


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


# ---------------------------------------------------------------------------
# put_many / put_many_async — single-fsync batched writes (issue #11)
# ---------------------------------------------------------------------------

def test_put_many_writes_all_items_and_preserves_order(tmp_path):
    """put_many writes every item as a JSONL line, in input order."""
    path = tmp_path / "q.jsonl"
    q = PersistentQueue(path)
    q.put_many([{"a": 1}, {"a": 2}, {"a": 3}])
    # FIFO drain confirms order
    assert q.get() == {"a": 1}
    q.task_done()
    assert q.get() == {"a": 2}
    q.task_done()
    assert q.get() == {"a": 3}
    q.task_done()


def test_put_many_empty_is_noop(tmp_path):
    """put_many([]) opens nothing, fsyncs nothing, notifies nothing."""
    path = tmp_path / "q.jsonl"
    q = PersistentQueue(path)
    # Patch os.fsync to ensure it is never called for an empty payload.
    with patch(
        "custom_components.timescaledb_recorder.persistent_queue.os.fsync"
    ) as mock_fsync:
        q.put_many([])
    assert mock_fsync.call_count == 0
    # File never created for empty input — no append happened.
    assert not path.exists()


def test_put_many_single_fsync(tmp_path):
    """put_many of N items must call os.fsync exactly once (issue #11)."""
    path = tmp_path / "q.jsonl"
    q = PersistentQueue(path)
    items = [{"i": i} for i in range(5)]
    with patch(
        "custom_components.timescaledb_recorder.persistent_queue.os.fsync"
    ) as mock_fsync:
        q.put_many(items)
    assert mock_fsync.call_count == 1, (
        f"put_many({len(items)}) must fsync once, got {mock_fsync.call_count}"
    )
    # Sanity: all items landed.
    text = path.read_text()
    assert text.count("\n") == 5


async def test_put_many_async_drains_via_get(tmp_path):
    """put_many_async runs put_many in the executor and items drain in order."""
    path = tmp_path / "q.jsonl"
    q = PersistentQueue(path)
    await q.put_many_async([{"k": "a"}, {"k": "b"}, {"k": "c"}])
    # Drain — order preserved.
    for expected in ("a", "b", "c"):
        assert q.get() == {"k": expected}
        q.task_done()
    # join must return promptly when empty.
    await asyncio.wait_for(q.join(), timeout=2)


async def test_put_many_async_empty_is_noop(tmp_path):
    """put_many_async([]) returns without scheduling executor work."""
    path = tmp_path / "q.jsonl"
    q = PersistentQueue(path)
    # No fsync, no file — completes synchronously.
    with patch(
        "custom_components.timescaledb_recorder.persistent_queue.os.fsync"
    ) as mock_fsync:
        await q.put_many_async([])
    assert mock_fsync.call_count == 0
    assert not path.exists()


# ---------------------------------------------------------------------------
# Tolerant get(): malformed lines are skipped + warned (issue #11)
# ---------------------------------------------------------------------------

def test_get_skips_malformed_front_line(tmp_path, caplog):
    """A bogus first line is dropped from disk with a WARNING; get() returns the next valid item."""
    path = tmp_path / "q.jsonl"
    # Manually craft a file: malformed line then a valid line.
    path.write_text("not-json\n" + '{"k": "ok"}\n', encoding="utf-8")
    q = PersistentQueue(path)
    with caplog.at_level(logging.WARNING, logger="custom_components.timescaledb_recorder.persistent_queue"):
        item = q.get()
    assert item == {"k": "ok"}
    assert any("malformed" in rec.message.lower() or "invalid" in rec.message.lower() or "skip" in rec.message.lower()
               for rec in caplog.records), (
        f"Expected a WARNING about the malformed line, got: {[r.message for r in caplog.records]}"
    )
    q.task_done()
    # After draining the only good line, file is empty.
    assert path.read_text() == ""


def test_get_recovers_from_partial_trailing_line(tmp_path, caplog):
    """A torn write (partial trailing JSON) does not crash get(); the partial line is skipped."""
    path = tmp_path / "q.jsonl"
    q = PersistentQueue(path)
    q.put({"good": True})
    # Simulate a power-loss tail: append an incomplete JSON fragment after the good line.
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"k":')
    # Drain the good item normally — it is the front.
    assert q.get() == {"good": True}
    q.task_done()
    # The partial fragment is now at the front. get() must not raise; it skips
    # the malformed line, logs WARNING, and returns None (no more good items).
    with caplog.at_level(logging.WARNING, logger="custom_components.timescaledb_recorder.persistent_queue"):
        # Wake from a separate thread so get() does not hang on empty-file wait.
        def _waker():
            time.sleep(0.1)
            q.wake_consumer()
        threading.Thread(target=_waker, daemon=True).start()
        result = q.get()
    assert result is None
    # WARNING was emitted for the malformed line.
    assert any("malformed" in rec.message.lower() or "invalid" in rec.message.lower() or "skip" in rec.message.lower()
               for rec in caplog.records), (
        f"Expected a WARNING about the malformed line, got: {[r.message for r in caplog.records]}"
    )
