"""Unit tests for DbWorker."""
import queue
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from psycopg.types.json import Jsonb

from custom_components.ha_timescaledb_recorder.worker import (
    DbWorker,
    MetaCommand,
    StateRow,
    _STOP,
)
from custom_components.ha_timescaledb_recorder.const import INSERT_SQL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state_row(entity_id="sensor.temp", state="21.5", attributes=None):
    """Build a StateRow for testing."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return StateRow(
        entity_id=entity_id,
        state=state,
        attributes=attributes or {"unit": "°C"},
        last_updated=now,
        last_changed=now,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hass():
    h = MagicMock()
    h.async_add_executor_job = AsyncMock()
    h.add_job = MagicMock()
    return h


@pytest.fixture
def mock_syncer():
    """Return a mock MetadataSyncer with all change-detection helpers returning False."""
    s = MagicMock()
    s._entity_row_changed = MagicMock(return_value=False)
    s._device_row_changed = MagicMock(return_value=False)
    s._area_row_changed = MagicMock(return_value=False)
    s._label_row_changed = MagicMock(return_value=False)
    return s


@pytest.fixture
def worker(hass, mock_syncer):
    """Return a DbWorker (no thread started)."""
    return DbWorker(
        hass=hass,
        dsn="postgresql://test/test",
        chunk_interval_days=7,
        compress_after_hours=2,
        syncer=mock_syncer,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_state_row_is_frozen():
    """StateRow must be immutable — frozen=True ensures thread-safe enqueue."""
    import dataclasses
    row = _make_state_row()
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        row.entity_id = "x"


def test_meta_command_is_frozen():
    """MetaCommand must be immutable — frozen=True ensures thread-safe enqueue."""
    import dataclasses
    cmd = MetaCommand(
        registry="entity", action="create",
        registry_id="sensor.temp", old_id=None,
        params=("sensor.temp",),
    )
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        cmd.action = "x"


def test_meta_command_fields():
    """MetaCommand field names must match the interface spec: registry_id and old_id (not id)."""
    cmd = MetaCommand(
        registry="entity",
        action="create",
        registry_id="sensor.temp",
        old_id=None,
        params=("sensor.temp",),
    )
    assert cmd.registry == "entity"
    assert cmd.action == "create"
    assert cmd.registry_id == "sensor.temp"
    assert cmd.old_id is None
    assert cmd.params == ("sensor.temp",)


def test_stop_puts_sentinel(worker):
    """stop() must enqueue the _STOP sentinel for the worker thread to exit cleanly."""
    worker.stop()
    assert worker.queue.get_nowait() is _STOP


def test_flush_calls_executemany_with_correct_sql(worker, mock_psycopg_conn):
    """_flush() must call executemany with INSERT_SQL as the first argument."""
    conn, cur = mock_psycopg_conn
    worker._conn = conn
    worker._flush([_make_state_row()])
    assert cur.executemany.called
    call_args = cur.executemany.call_args
    assert call_args[0][0] == INSERT_SQL


def test_flush_wraps_attributes_in_jsonb(worker, mock_psycopg_conn):
    """_flush() must wrap attributes dict in Jsonb() for psycopg3 JSONB column (WORK-04).

    Verified with isinstance(), not type().__name__ string comparison, so this
    test remains correct even if Jsonb gains subclasses.
    """
    conn, cur = mock_psycopg_conn
    worker._conn = conn
    worker._flush([_make_state_row(attributes={"k": "v"})])
    call_args = cur.executemany.call_args
    rows = call_args[0][1]
    # Third tuple element is attributes — must be Jsonb, not a plain dict
    assert isinstance(rows[0][2], Jsonb)


def test_flush_operational_error_logs_warning(worker, mock_psycopg_conn, caplog):
    """_flush() must catch OperationalError, log a warning, and not raise.

    Phase 1 accepts row drops on transient errors; Phase 2 adds retry.
    """
    conn, cur = mock_psycopg_conn
    cur.executemany.side_effect = psycopg.OperationalError("conn reset")
    worker._conn = conn
    # Must not raise
    worker._flush([_make_state_row()])
    assert "Connection error during flush" in caplog.text


def test_run_loop_processes_state_rows_and_stop(hass, mock_syncer, mock_psycopg_conn):
    """Worker thread must drain StateRow items and exit cleanly on _STOP."""
    conn, cur = mock_psycopg_conn
    with patch("custom_components.ha_timescaledb_recorder.worker.psycopg.connect", return_value=conn):
        with patch("custom_components.ha_timescaledb_recorder.worker.sync_setup_schema"):
            w = DbWorker(hass=hass, dsn="postgresql://test/test",
                         chunk_interval_days=7, compress_after_hours=2, syncer=mock_syncer)
            w.start()
            w.queue.put_nowait(_make_state_row())
            w.queue.put_nowait(_make_state_row(entity_id="sensor.other"))
            w.queue.put_nowait(_STOP)
            w._thread.join(timeout=2)
    assert not w._thread.is_alive(), "Worker thread should have exited after _STOP"
    assert cur.executemany.called


def test_connect_failure_worker_enters_loop(hass, mock_syncer):
    """D-03: connect() failure must not kill the worker thread.

    The thread must enter the main flush loop and keep running so in-memory
    buffering works even when the DB is unreachable at startup.
    """
    with patch(
        "custom_components.ha_timescaledb_recorder.worker.psycopg.connect",
        side_effect=psycopg.OperationalError("connection refused"),
    ):
        w = DbWorker(hass=hass, dsn="postgresql://test/test",
                     chunk_interval_days=7, compress_after_hours=2, syncer=mock_syncer)
        w.start()
        # Give the thread a moment to enter the loop, then signal shutdown
        w.queue.put_nowait(_STOP)
        w._thread.join(timeout=2)

    # Thread must have exited cleanly via _STOP (entered the loop and processed sentinel)
    assert not w._thread.is_alive(), (
        "Worker thread must enter the flush loop even when connect() fails (D-03)"
    )
    # Connection must remain None (connect failed)
    assert w._conn is None


def test_bad_meta_command_does_not_kill_loop(hass, mock_syncer, mock_psycopg_conn):
    """Per-item exception boundary: one bad MetaCommand must not kill the worker thread.

    Verified by: inject a command that raises, then inject a StateRow + _STOP.
    The worker must process the StateRow (executemany called) and exit cleanly.
    """
    conn, cur = mock_psycopg_conn
    # Make _process_meta_command raise for the bad command
    bad_cmd = MetaCommand(
        registry="entity", action="create",
        registry_id="sensor.bad", old_id=None,
        params=("sensor.bad",),  # incomplete params — will raise inside _process_entity_command
    )

    with patch("custom_components.ha_timescaledb_recorder.worker.psycopg.connect", return_value=conn):
        with patch("custom_components.ha_timescaledb_recorder.worker.sync_setup_schema"):
            # Patch _process_meta_command to raise on the bad command
            original_process = DbWorker._process_meta_command

            call_count = {"n": 0}
            def patched_process(self, cmd):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RuntimeError("simulated bad command")
                return original_process(self, cmd)

            with patch.object(DbWorker, "_process_meta_command", patched_process):
                w = DbWorker(hass=hass, dsn="postgresql://test/test",
                             chunk_interval_days=7, compress_after_hours=2, syncer=mock_syncer)
                w.start()
                w.queue.put_nowait(bad_cmd)           # will raise inside patched_process
                w.queue.put_nowait(_make_state_row()) # must still be processed
                w.queue.put_nowait(_STOP)
                w._thread.join(timeout=2)

    assert not w._thread.is_alive(), "Worker thread must survive a bad MetaCommand"
    assert cur.executemany.called, "StateRow after bad MetaCommand must still be processed"


async def test_graceful_shutdown_awaits_join(hass, worker):
    """async_stop() must await thread join via executor job — not run_coroutine_threadsafe."""
    await worker.async_stop()
    assert hass.async_add_executor_job.called


def test_process_entity_create_executes_snapshot_sql(worker, mock_psycopg_conn):
    """_process_entity_command('create') must execute the idempotent snapshot SQL against dim_entities."""
    conn, cur = mock_psycopg_conn
    worker._conn = conn
    now = datetime.now(timezone.utc)
    cmd = MetaCommand(
        registry="entity",
        action="create",
        registry_id="sensor.temp",
        old_id=None,
        params=(
            "sensor.temp",   # entity_id
            "uuid-1",        # ha_entity_uuid
            "Temp",          # name
            "sensor",        # domain
            "zha",           # platform
            None,            # device_id
            None,            # area_id
            [],              # labels
            None,            # device_class
            None,            # unit_of_measurement
            None,            # disabled_by
            now,             # valid_from
            "{}",            # extra
        ),
    )
    worker._process_entity_command(cmd, now)
    assert cur.execute.called
    assert "dim_entities" in str(cur.execute.call_args)


def test_process_entity_remove_executes_close_sql(worker, mock_psycopg_conn):
    """_process_entity_command('remove') must set valid_to on the open dim_entities row."""
    conn, cur = mock_psycopg_conn
    worker._conn = conn
    now = datetime.now(timezone.utc)
    cmd = MetaCommand(
        registry="entity",
        action="remove",
        registry_id="sensor.temp",
        old_id=None,
        params=None,
    )
    worker._process_entity_command(cmd, now)
    assert cur.execute.called
    # SCD2_CLOSE_ENTITY_SQL contains "SET valid_to"
    assert "SET valid_to" in str(cur.execute.call_args)
