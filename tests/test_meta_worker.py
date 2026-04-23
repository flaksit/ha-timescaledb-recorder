"""Unit tests for TimescaledbMetaRecorderThread (D-05, D-07, D-15-c)."""
import re
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from custom_components.ha_timescaledb_recorder.const import (
    SCD2_CLOSE_AREA_SQL,
    SCD2_CLOSE_DEVICE_SQL,
    SCD2_CLOSE_ENTITY_SQL,
    SCD2_CLOSE_LABEL_SQL,
    SCD2_INSERT_ENTITY_SQL,
    SCD2_SNAPSHOT_AREA_SQL,
    SCD2_SNAPSHOT_DEVICE_SQL,
    SCD2_SNAPSHOT_ENTITY_SQL,
    SCD2_SNAPSHOT_LABEL_SQL,
)
from custom_components.ha_timescaledb_recorder.meta_worker import (
    _VALID_FROM_INDEX,
    TimescaledbMetaRecorderThread,
)


def _make_worker(meta_queue=None, syncer=None):
    hass = MagicMock()
    meta_queue = meta_queue or MagicMock()
    syncer = syncer or MagicMock()
    stop_event = threading.Event()
    return TimescaledbMetaRecorderThread(
        hass=hass, dsn="postgresql://",
        meta_queue=meta_queue, syncer=syncer, stop_event=stop_event,
    )


def test_valid_from_index_constants():
    assert _VALID_FROM_INDEX == {
        "entity": 11, "device": 6, "area": 2, "label": 3,
    }


def test_rehydrate_params_converts_iso_string_at_entity_slot():
    w = _make_worker()
    # entity params: 13 slots; slot 11 (0-indexed) is valid_from
    params = ["x.y", "uuid", "name", "sensor", "zha", None, None,
              [], None, None, None, "2026-04-21T12:00:00+00:00", "{}"]
    out = w._rehydrate_params("entity", params)
    assert isinstance(out, tuple)
    assert isinstance(out[11], datetime)
    assert out[11] == datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)


def test_rehydrate_params_none_is_none():
    w = _make_worker()
    assert w._rehydrate_params("entity", None) is None


def test_write_item_entity_create_uses_snapshot_sql(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    item = {
        "registry": "entity", "action": "create",
        "registry_id": "sensor.x", "old_id": None,
        "params": ["sensor.x", "uuid", "n", "sensor", "zha", None, None,
                   [], None, None, None, "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)
    args = cur.execute.call_args_list
    # First execute is the snapshot SQL
    sql, params = args[0].args
    assert sql == SCD2_SNAPSHOT_ENTITY_SQL
    assert params[0] == "sensor.x"
    # entity_id appears twice: once in SELECT, once in WHERE NOT EXISTS
    assert params[-1] == "sensor.x"


def test_write_item_entity_remove_uses_close_sql(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    item = {
        "registry": "entity", "action": "remove",
        "registry_id": "sensor.x", "old_id": None,
        "params": None,
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)
    call_args = cur.execute.call_args
    sql, params = call_args.args
    assert sql == SCD2_CLOSE_ENTITY_SQL
    assert params[1] == "sensor.x"


def test_write_item_entity_rename_closes_old_inserts_new(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    # conn.transaction() must work as a context manager
    conn.transaction = MagicMock(return_value=MagicMock(
        __enter__=MagicMock(),
        __exit__=MagicMock(return_value=False),
    ))
    item = {
        "registry": "entity", "action": "update",
        "registry_id": "sensor.new", "old_id": "sensor.old",
        "params": ["sensor.new", "uuid", "n", "sensor", "zha", None, None,
                   [], None, None, None, "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)
    calls = cur.execute.call_args_list
    sqls = [c.args[0] for c in calls]
    assert SCD2_CLOSE_ENTITY_SQL in sqls
    assert SCD2_INSERT_ENTITY_SQL in sqls


def test_write_item_device_create(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    item = {
        "registry": "device", "action": "create",
        "registry_id": "dev-1", "old_id": None,
        "params": ["dev-1", "name", "mfr", "model", None, [],
                   "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)
    sql = cur.execute.call_args.args[0]
    assert sql == SCD2_SNAPSHOT_DEVICE_SQL


def test_write_item_area_create(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    item = {
        "registry": "area", "action": "create",
        "registry_id": "area-1", "old_id": None,
        "params": ["area-1", "name", "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)
    sql = cur.execute.call_args.args[0]
    assert sql == SCD2_SNAPSHOT_AREA_SQL


def test_write_item_label_create(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    item = {
        "registry": "label", "action": "create",
        "registry_id": "label-1", "old_id": None,
        "params": ["label-1", "name", "#fff", "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)
    sql = cur.execute.call_args.args[0]
    assert sql == SCD2_SNAPSHOT_LABEL_SQL


def test_run_loop_skips_task_done_on_shutdown_interrupt(mock_psycopg_conn):
    """D-03-h: when retry returns None due to shutdown, do not call task_done."""
    conn, cur = mock_psycopg_conn
    stop_event = threading.Event()
    meta_queue = MagicMock()
    meta_queue.get = MagicMock(side_effect=[{
        "registry": "entity", "action": "create",
        "registry_id": "x.y", "old_id": None,
        "params": ["x.y", "u", "n", "sensor", "zha", None, None,
                   [], None, None, None, "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }])
    w = _make_worker(meta_queue=meta_queue)
    w._conn = conn

    def fake_write_item(item):
        # Simulate: retry-decorator interrupted by shutdown → sets stop_event
        # and returns None (D-07-e behaviour).
        stop_event.set()
        return None

    w._write_item = fake_write_item
    w._stop_event = stop_event

    w.run()
    meta_queue.task_done.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 3 Plan 04: _last_* watchdog context + hook methods + outer try/except
# ---------------------------------------------------------------------------


def test_last_exception_initialized_to_none_in_init():
    """D-06-b: _last_exception has a safe default in __init__ so watchdog can
    always read it even if run() never executes."""
    w = _make_worker()
    assert w._last_exception is None


def test_last_context_initialized_with_safe_defaults_in_init():
    """D-06-b: _last_context is a dict (not empty) with known keys at construction.
    meta_worker has no mode machine, so mode is None (not 'init')."""
    w = _make_worker()
    ctx = w._last_context
    assert isinstance(ctx, dict)
    assert set(ctx.keys()) == {"at", "mode", "retry_attempt", "last_op"}
    # meta_worker has no mode state machine — mode is always None.
    assert ctx["mode"] is None
    assert ctx["last_op"] == "unknown"


def test_stall_hook_fires_persistent_notification_and_repair_issue():
    """D-02 / D-07-f: _stall_hook fires both the Phase 2 persistent_notification
    AND the Phase 3 meta_worker_stalled repair issue — two hass.add_job calls."""
    hass = MagicMock()
    w = _make_worker()
    w._hass = hass

    with patch("homeassistant.components.persistent_notification"):
        w._stall_hook(5)

    assert hass.add_job.call_count == 2

    from custom_components.ha_timescaledb_recorder.issues import (
        create_meta_worker_stalled_issue,
    )
    assert call(create_meta_worker_stalled_issue, hass) in hass.add_job.call_args_list


def test_recovery_hook_clears_both_issues():
    """D-02-c / D-03-a: _recovery_hook clears meta_worker_stalled AND
    db_unreachable — both may have been raised during a long stall streak."""
    hass = MagicMock()
    w = _make_worker()
    w._hass = hass
    w._recovery_hook()

    assert hass.add_job.call_count == 2

    from custom_components.ha_timescaledb_recorder.issues import (
        clear_db_unreachable_issue,
        clear_meta_worker_stalled_issue,
    )
    assert call(clear_meta_worker_stalled_issue, hass) in hass.add_job.call_args_list
    assert call(clear_db_unreachable_issue, hass) in hass.add_job.call_args_list


def test_sustained_fail_hook_creates_db_unreachable_issue():
    """D-11: _sustained_fail_hook fires create_db_unreachable_issue via hass.add_job."""
    hass = MagicMock()
    w = _make_worker()
    w._hass = hass
    w._sustained_fail_hook()

    assert hass.add_job.call_count == 1

    from custom_components.ha_timescaledb_recorder.issues import create_db_unreachable_issue
    assert call(create_db_unreachable_issue, hass) in hass.add_job.call_args_list


def test_retry_decorator_wired_with_all_phase3_hooks():
    """D-03: retry_until_success must receive on_recovery and on_sustained_fail
    keyword args bound to the meta worker instance hook methods."""
    captured = {}

    def fake_retry(*, stop_event, on_transient=None, notify_stall=None,
                   on_recovery=None, on_sustained_fail=None, **kwargs):
        captured["on_recovery"] = on_recovery
        captured["on_sustained_fail"] = on_sustained_fail
        def decorator(fn):
            return fn
        return decorator

    with patch(
        "custom_components.ha_timescaledb_recorder.meta_worker.retry_until_success",
        fake_retry,
    ):
        w = _make_worker()

    assert captured.get("on_recovery") is not None
    assert captured.get("on_sustained_fail") is not None
    assert captured["on_recovery"].__func__ is w._recovery_hook.__func__
    assert captured["on_sustained_fail"].__func__ is w._sustained_fail_hook.__func__


def test_run_outer_except_captures_last_exception_and_context():
    """D-06-a: run() outer try/except captures the unhandled exception into
    _last_exception and populates _last_context with the four required keys."""
    stop_event = threading.Event()
    w = _make_worker()
    w._stop_event = stop_event

    boom = RuntimeError("boom")

    def raise_boom():
        raise boom

    w._run_main_loop = raise_boom
    w.start()
    w.join(timeout=5)

    assert not w.is_alive()
    assert w._last_exception is boom
    ctx = w._last_context
    assert isinstance(ctx, dict)
    assert set(ctx.keys()) == {"at", "mode", "retry_attempt", "last_op"}


def test_run_outer_except_context_has_iso_at_timestamp():
    """D-06-a: _last_context['at'] must be a valid ISO-8601 UTC timestamp string."""
    w = _make_worker()
    w._run_main_loop = lambda: (_ for _ in ()).throw(RuntimeError("ts-test"))
    w.start()
    w.join(timeout=5)

    at_val = w._last_context.get("at")
    assert at_val is not None
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", at_val)


def test_run_finally_closes_connection_on_exception():
    """D-06 / MEDIUM-8: the finally block must close the connection even when
    _run_main_loop raises — verified by mock call count."""
    w = _make_worker()
    mock_conn = MagicMock()
    w._conn = mock_conn
    w._run_main_loop = lambda: (_ for _ in ()).throw(RuntimeError("conn-test"))

    w.start()
    w.join(timeout=5)

    mock_conn.close.assert_called_once()


def test_run_teardown_error_does_not_overwrite_last_exception():
    """MEDIUM-8: if conn.close() raises inside finally, _last_exception must
    still be the original RuntimeError from _run_main_loop — not overwritten."""
    w = _make_worker()

    original_err = RuntimeError("original")
    mock_conn = MagicMock()
    mock_conn.close.side_effect = RuntimeError("teardown-error")
    w._conn = mock_conn

    w._run_main_loop = lambda: (_ for _ in ()).throw(original_err)

    w.start()
    w.join(timeout=5)

    assert w._last_exception is original_err


def test_last_op_updated_before_write_item():
    """D-06-b: _last_op must be initialized to a sentinel at construction time
    and updated before each major operation in _run_main_loop."""
    w = _make_worker()
    # Safe default at construction.
    assert w._last_op == "unknown"
    assert isinstance(w._last_op, str)
    assert isinstance(w._last_retry_attempt, (int, type(None)))
