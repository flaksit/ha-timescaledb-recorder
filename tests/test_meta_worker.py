"""Unit tests for TimescaledbMetaRecorderThread (D-05, D-07, D-15-c)."""
import re
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from custom_components.timescaledb_recorder.const import (
    SCD2_CLOSE_AREA_SQL,
    SCD2_CLOSE_DEVICE_SQL,
    SCD2_CLOSE_ENTITY_SQL,
    SCD2_ANY_VERSION_SQL,
    SCD2_CLOSE_LABEL_SQL,
    SCD2_SNAPSHOT_AREA_SQL,
    SCD2_SNAPSHOT_DEVICE_SQL,
    SCD2_SNAPSHOT_ENTITY_SQL,
    SCD2_SNAPSHOT_LABEL_SQL,
)
from custom_components.timescaledb_recorder.meta_worker import (
    _VALID_FROM_INDEX,
    TimescaledbMetaRecorderThread,
)


def _make_worker(meta_queue=None, registry_listener=None):
    hass = MagicMock()
    meta_queue = meta_queue or MagicMock()
    registry_listener = registry_listener or MagicMock()
    stop_event = threading.Event()
    return TimescaledbMetaRecorderThread(
        hass=hass, dsn="postgresql://",
        meta_queue=meta_queue, registry_listener=registry_listener, stop_event=stop_event,
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
    # Reconcile: close whatever differs, then the guarded insert.
    assert [a.args[0] for a in args] == [SCD2_CLOSE_ENTITY_SQL, SCD2_SNAPSHOT_ENTITY_SQL]
    sql, params = args[1].args
    assert params[0] == "sensor.x"
    # entity_id appears twice: once in SELECT, once in WHERE NOT EXISTS
    assert params[-1] == "sensor.x"


# ---------------------------------------------------------------------------
# Reconcile path and first-version start (issue #19)
# ---------------------------------------------------------------------------

EVENT_TS = "2026-04-21T12:00:00+00:00"
CREATED = "2026-04-21 11:59:30.500000+00:00"  # str(datetime), as _build_extra writes it


def _entity(action="snapshot", extra='{"created_at": "%s"}' % CREATED, old_id=None,
            entity_id="sensor.x"):
    return {
        "registry": "entity", "action": action,
        "registry_id": entity_id, "old_id": old_id,
        "params": [entity_id, "uuid-x", "n", "sensor", "zha", None, None,
                   [], None, None, None, EVENT_TS, extra],
        "enqueued_at": EVENT_TS,
    }


def _first_version_worker(mock_psycopg_conn, *, closed: int = 0, history=None, changed=True):
    """Worker whose close touched `closed` rows and whose history probe returns `history`."""
    conn, cur = mock_psycopg_conn
    rl = MagicMock()
    for name in ("_entity_row_changed", "_device_row_changed",
                 "_area_row_changed", "_label_row_changed"):
        getattr(rl, name).return_value = changed
    w = _make_worker(registry_listener=rl)
    w._conn = conn
    rowcounts = iter([closed, 1])  # close, then insert
    type(cur).rowcount = property(lambda _self: next(rowcounts, 1))
    cur.fetchone.return_value = history
    return w, cur


def _inserted_valid_from(cur, sql=SCD2_SNAPSHOT_ENTITY_SQL, idx=11):
    params = next(c.args[1] for c in cur.execute.call_args_list if c.args[0] == sql)
    return params[idx]


@pytest.mark.parametrize("action", ["create", "update", "snapshot"])
def test_reconcile_actions_write_nothing_when_the_open_row_matches(mock_psycopg_conn, action):
    """Replaying a snapshot, or any already-applied change, must be a no-op."""
    w, cur = _first_version_worker(mock_psycopg_conn, changed=False)
    w._write_item_raw(_entity(action))
    cur.execute.assert_not_called()
    assert w.out_of_order_skips == 0


def test_snapshot_closes_and_inserts_on_drift(mock_psycopg_conn):
    """A registry that drifted while HA was down is stamped at snapshot time."""
    w, cur = _first_version_worker(mock_psycopg_conn, closed=1)
    w._write_item_raw(_entity())
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert sqls == [SCD2_CLOSE_ENTITY_SQL, SCD2_SNAPSHOT_ENTITY_SQL]
    assert _inserted_valid_from(cur) == datetime.fromisoformat(EVENT_TS)


def test_first_version_starts_at_created_at(mock_psycopg_conn):
    w, cur = _first_version_worker(mock_psycopg_conn, history=None)
    w._write_item_raw(_entity())
    assert _inserted_valid_from(cur) == datetime.fromisoformat(CREATED)
    probe = next(c for c in cur.execute.call_args_list
                 if c.args[0] == SCD2_ANY_VERSION_SQL["entity"])
    # The uuid is checked too: a rename while HA was down keeps created_at.
    assert probe.args[1] == ("sensor.x", "uuid-x")


def test_id_with_history_keeps_event_time(mock_psycopg_conn):
    """A gap after a removal is real; created_at must not paper over it."""
    w, cur = _first_version_worker(mock_psycopg_conn, history=(1,))
    w._write_item_raw(_entity("create"))
    assert _inserted_valid_from(cur) == datetime.fromisoformat(EVENT_TS)


@pytest.mark.parametrize("extra", [
    "{}",                                                    # persisted by an older version
    '{"created_at": "1970-01-01 00:00:00+00:00"}',           # HA's migration sentinel
    '{"created_at": "2026-04-21 12:00:01+00:00"}',           # after the event: LEAST
    '{"created_at": "garbage"}',
])
def test_unusable_created_at_keeps_event_time_without_probing(mock_psycopg_conn, extra):
    w, cur = _first_version_worker(mock_psycopg_conn)
    w._write_item_raw(_entity(extra=extra))
    assert _inserted_valid_from(cur) == datetime.fromisoformat(EVENT_TS)
    assert SCD2_ANY_VERSION_SQL["entity"] not in [c.args[0] for c in cur.execute.call_args_list]


def test_rename_never_uses_created_at(mock_psycopg_conn):
    """The registry entry predates the new entity_id."""
    w, cur = _first_version_worker(mock_psycopg_conn, history=None)
    w._write_item_raw(_entity("update", old_id="sensor.old", entity_id="sensor.new"))
    assert _inserted_valid_from(cur) == datetime.fromisoformat(EVENT_TS)
    assert SCD2_ANY_VERSION_SQL["entity"] not in [c.args[0] for c in cur.execute.call_args_list]


def test_device_first_version_starts_at_created_at(mock_psycopg_conn):
    w, cur = _first_version_worker(mock_psycopg_conn, history=None)
    w._write_item_raw({
        "registry": "device", "action": "snapshot", "registry_id": "dev-1", "old_id": None,
        "params": ["dev-1", "n", "m", "mo", None, [], EVENT_TS,
                   '{"created_at": "%s"}' % CREATED],
        "enqueued_at": EVENT_TS,
    })
    assert _inserted_valid_from(cur, SCD2_SNAPSHOT_DEVICE_SQL, 6) == datetime.fromisoformat(CREATED)
    probe = next(c for c in cur.execute.call_args_list
                 if c.args[0] == SCD2_ANY_VERSION_SQL["device"])
    assert probe.args[1] == ("dev-1",)


def test_unknown_action_is_ignored(mock_psycopg_conn):
    w, cur = _first_version_worker(mock_psycopg_conn)
    w._write_item_raw(_entity("frobnicate"))
    cur.execute.assert_not_called()


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
    # A removal closes at the time the event fired, not the time the worker got
    # round to it — see test_remove_closes_at_event_time_not_dequeue_time.
    assert params[0] == datetime.fromisoformat("2026-04-21T12:00:00+00:00")
    assert params[0] == params[2]


def test_remove_closes_at_event_time_not_dequeue_time(mock_psycopg_conn):
    """A delayed queue must not push a removal's close time forward.

    With the database unreachable for hours, dequeue time is hours after the
    entity actually vanished. Closing at dequeue time can then overlap a
    re-creation that happened in between, and the constraint rejects it — leaving
    the entity permanently marked removed.
    """
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    removed_at = "2026-04-21T10:00:00+00:00"
    item = {
        "registry": "entity", "action": "remove",
        "registry_id": "sensor.x", "old_id": None, "params": None,
        "enqueued_at": removed_at,
    }
    w._write_item_raw(item)

    params = cur.execute.call_args.args[1]
    assert params[0] == datetime.fromisoformat(removed_at)
    assert params[2] == datetime.fromisoformat(removed_at)


def test_remove_falls_back_to_clock_without_usable_event_time(mock_psycopg_conn):
    """An item with no parseable enqueued_at must still close, not crash."""
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    item = {
        "registry": "entity", "action": "remove",
        "registry_id": "sensor.x", "old_id": None, "params": None,
        "enqueued_at": "not-a-timestamp",
    }
    w._write_item_raw(item)

    params = cur.execute.call_args.args[1]
    assert isinstance(params[0], datetime)
    assert params[0] == params[2]


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
    # The guarded snapshot statement, not a bare INSERT: a replayed rename must
    # not be able to add a second open row (issue #17).
    assert SCD2_SNAPSHOT_ENTITY_SQL in sqls


def test_close_timestamp_equals_incoming_valid_from(mock_psycopg_conn):
    """Issue #17: close and insert of one change must share one timestamp.

    A separate clock read for the close puts it after the successor's start,
    producing an overlapping interval on every single metadata change.
    """
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    conn.transaction = MagicMock(return_value=MagicMock(
        __enter__=MagicMock(), __exit__=MagicMock(return_value=False),
    ))
    w._registry_listener._entity_row_changed = MagicMock(return_value=True)
    valid_from = "2026-04-21T12:00:00+00:00"
    item = {
        "registry": "entity", "action": "update",
        "registry_id": "sensor.x", "old_id": None,
        "params": ["sensor.x", "uuid", "n", "sensor", "zha", None, None,
                   [], None, None, None, valid_from, "{}"],
        "enqueued_at": valid_from,
    }
    w._write_item_raw(item)

    close = next(c for c in cur.execute.call_args_list
                 if c.args[0] == SCD2_CLOSE_ENTITY_SQL)
    insert = next(c for c in cur.execute.call_args_list
                  if c.args[0] == SCD2_SNAPSHOT_ENTITY_SQL)
    expected = datetime.fromisoformat(valid_from)
    assert close.args[1][0] == expected, "close must use the incoming valid_from"
    assert close.args[1][2] == expected, "ordering guard must use the same value"
    assert insert.args[1][_VALID_FROM_INDEX["entity"]] == expected


@pytest.mark.parametrize("registry,params,close_sql", [
    ("device", ["dev-1", "n", "m", "mo", None, [], "2026-04-21T12:00:00+00:00", "{}"],
     SCD2_CLOSE_DEVICE_SQL),
    ("area", ["area-1", "n", "2026-04-21T12:00:00+00:00", "{}"], SCD2_CLOSE_AREA_SQL),
    ("label", ["label-1", "n", "#fff", "2026-04-21T12:00:00+00:00", "{}"],
     SCD2_CLOSE_LABEL_SQL),
])
def test_close_timestamp_for_other_registries(mock_psycopg_conn, registry, params, close_sql):
    """The single-timestamp rule applies to every dimension, not just entities."""
    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    conn.transaction = MagicMock(return_value=MagicMock(
        __enter__=MagicMock(), __exit__=MagicMock(return_value=False),
    ))
    getattr(w._registry_listener, f"_{registry}_row_changed").return_value = True
    item = {
        "registry": registry, "action": "update",
        "registry_id": params[0], "old_id": None,
        "params": params, "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)

    close = next(c for c in cur.execute.call_args_list if c.args[0] == close_sql)
    expected = datetime.fromisoformat("2026-04-21T12:00:00+00:00")
    assert close.args[1][0] == expected
    assert close.args[1][2] == expected


def test_integrity_violation_drops_item_instead_of_wedging_queue(mock_psycopg_conn):
    """Issue #17: retry_until_success never gives up, so an unretryable
    constraint violation must not reach it — one bad item would otherwise stall
    every later metadata write forever."""
    import psycopg

    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    cur.execute.side_effect = psycopg.errors.ExclusionViolation("overlaps")
    item = {
        "registry": "entity", "action": "create",
        "registry_id": "sensor.x", "old_id": None,
        "params": ["sensor.x", "uuid", "n", "sensor", "zha", None, None,
                   [], None, None, None, "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    w._write_item_raw(item)  # must not raise
    assert w.integrity_drops == 1


def test_transient_errors_still_propagate_to_retry(mock_psycopg_conn):
    """Only integrity violations are swallowed — a dropped connection must still
    reach retry_until_success or the item would be lost."""
    import psycopg

    conn, cur = mock_psycopg_conn
    w = _make_worker()
    w._conn = conn
    cur.execute.side_effect = psycopg.OperationalError("connection lost")
    item = {
        "registry": "entity", "action": "create",
        "registry_id": "sensor.x", "old_id": None,
        "params": ["sensor.x", "uuid", "n", "sensor", "zha", None, None,
                   [], None, None, None, "2026-04-21T12:00:00+00:00", "{}"],
        "enqueued_at": "2026-04-21T12:00:00+00:00",
    }
    with pytest.raises(psycopg.OperationalError):
        w._write_item_raw(item)
    assert w.integrity_drops == 0


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

    from custom_components.timescaledb_recorder.issues import (
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

    from custom_components.timescaledb_recorder.issues import (
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

    from custom_components.timescaledb_recorder.issues import create_db_unreachable_issue
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
        "custom_components.timescaledb_recorder.meta_worker.retry_until_success",
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


# ---------------------------------------------------------------------------
# Issue #20: poison items are dead-lettered and task_done()d
# ---------------------------------------------------------------------------


def test_poison_item_is_dropped_and_queue_moves_on(mock_psycopg_conn):
    """An item that keeps failing non-transiently must not wedge the queue:
    it is dead-lettered, counted, surfaced, and task_done()d so the next item
    is written."""
    from custom_components.timescaledb_recorder.const import (
        DEADLETTER_REASON_POISON,
        METADATA_DEADLETTER_INSERT_SQL,
        POISON_MAX_ATTEMPTS,
    )
    from custom_components.timescaledb_recorder.issues import create_metadata_dropped_issue

    conn, cur = mock_psycopg_conn
    bad = {"registry": "entity", "action": "create", "registry_id": "bad.one"}
    good = {"registry": "entity", "action": "create", "registry_id": "good.one"}
    meta_queue = MagicMock()
    meta_queue.get = MagicMock(side_effect=[bad, good, None])
    w = _make_worker(meta_queue=meta_queue)
    # Skip the real backoff sleeps; the decorator holds this same Event.
    w._stop_event.wait = MagicMock(return_value=False)
    attempts = []

    def dispatch(item):
        attempts.append(item["registry_id"])
        if item is bad:
            raise ValueError("cannot serialise")

    w._dispatch_item = dispatch
    with patch(
        "custom_components.timescaledb_recorder.meta_worker.psycopg.connect",
        return_value=conn,
    ):
        w._run_main_loop()

    assert attempts == ["bad.one"] * POISON_MAX_ATTEMPTS + ["good.one"]
    assert meta_queue.task_done.call_count == 2
    assert w.poison_drops == 1
    dl = [c for c in cur.execute.call_args_list if c.args[0] == METADATA_DEADLETTER_INSERT_SQL]
    assert len(dl) == 1
    reason, registry, registry_id, error, _item = dl[0].args[1]
    assert (reason, registry, registry_id) == (DEADLETTER_REASON_POISON, "entity", "bad.one")
    assert "cannot serialise" in error
    assert call(create_metadata_dropped_issue, w._hass) in w._hass.add_job.call_args_list


def test_transient_failure_is_not_dropped(mock_psycopg_conn):
    """A transient error keeps the item: retried until it succeeds."""
    import psycopg

    from custom_components.timescaledb_recorder.const import POISON_MAX_ATTEMPTS

    conn, _cur = mock_psycopg_conn
    item = {"registry": "entity", "action": "create", "registry_id": "x.y"}
    meta_queue = MagicMock()
    meta_queue.get = MagicMock(side_effect=[item, None])
    w = _make_worker(meta_queue=meta_queue)
    w._stop_event.wait = MagicMock(return_value=False)
    errors = [psycopg.OperationalError("down")] * (POISON_MAX_ATTEMPTS + 2)

    def dispatch(_item):
        if errors:
            raise errors.pop(0)

    w._dispatch_item = dispatch
    with patch(
        "custom_components.timescaledb_recorder.meta_worker.psycopg.connect",
        return_value=conn,
    ):
        w._run_main_loop()

    assert w.poison_drops == 0
    meta_queue.task_done.assert_called_once()
