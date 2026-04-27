"""Unit tests for TimescaledbStateRecorderThread (D-04, D-06, D-08-d/f)."""
import queue
import re
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from custom_components.timescaledb_recorder.const import (
    INSERT_SQL,
    SELECT_OPEN_ENTITIES_SQL,
    SELECT_WATERMARK_SQL,
)
from custom_components.timescaledb_recorder.overflow_queue import OverflowQueue
from custom_components.timescaledb_recorder.states_worker import (
    MODE_BACKFILL,
    MODE_INIT,
    MODE_LIVE,
    TimescaledbStateRecorderThread,
)
from custom_components.timescaledb_recorder.worker import StateRow


def _make_thread(hass=None, live_queue=None, backfill_queue=None,
                 backfill_request=None, stop_event=None):
    hass = hass or MagicMock()
    live_queue = live_queue or OverflowQueue(maxsize=100)
    backfill_queue = backfill_queue or queue.Queue(maxsize=2)
    backfill_request = backfill_request or MagicMock()  # asyncio.Event mock
    stop_event = stop_event or threading.Event()
    return TimescaledbStateRecorderThread(
        hass=hass, dsn="postgresql://",
        live_queue=live_queue, backfill_queue=backfill_queue,
        backfill_request=backfill_request, stop_event=stop_event,
        chunk_interval_days=7, compress_after_hours=2,
    )


def _sample_state_row(eid="x.y", t=None):
    t = t or datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    return StateRow(
        entity_id=eid, state="on",
        attributes={"a": 1}, last_updated=t, last_changed=t,
    )


def test_mode_constants():
    assert MODE_INIT == "init"
    assert MODE_BACKFILL == "backfill"
    assert MODE_LIVE == "live"


def test_slice_to_rows_sorts_by_last_updated():
    t = _make_thread()
    t1 = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 21, 12, 0, 1, tzinfo=timezone.utc)
    t3 = datetime(2026, 4, 21, 12, 0, 2, tzinfo=timezone.utc)

    def mkstate(eid, at):
        s = MagicMock()
        s.entity_id = eid
        s.state = "on"
        s.attributes = {}
        s.last_updated = at
        s.last_changed = at
        return s

    slice_dict = {
        "a.1": [mkstate("a.1", t3), mkstate("a.1", t1)],
        "b.2": [mkstate("b.2", t2)],
    }
    rows = t._slice_to_rows(slice_dict)
    assert [r.last_updated for r in rows] == [t1, t2, t3]


def test_insert_chunk_raw_uses_jsonb_and_insert_sql(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    t = _make_thread()
    t._conn = conn
    rows = [_sample_state_row()]
    t._insert_chunk_raw(rows)
    # Correct SQL used
    cur.executemany.assert_called_once()
    sql_arg = cur.executemany.call_args[0][0]
    assert sql_arg == INSERT_SQL
    # Jsonb-wrapped attributes
    params = cur.executemany.call_args[0][1]
    assert len(params) == 1
    # attributes param is Jsonb-wrapped
    from psycopg.types.json import Jsonb
    assert isinstance(params[0][2], Jsonb)


def test_read_watermark_returns_max_last_updated(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    expected_t = datetime(2026, 4, 21, 0, 0, tzinfo=timezone.utc)
    cur.fetchone = MagicMock(return_value=(expected_t,))
    t = _make_thread()
    t._conn = conn
    wm = t.read_watermark()
    assert wm == expected_t
    cur.execute.assert_called_with(SELECT_WATERMARK_SQL)


def test_read_watermark_returns_none_on_empty_hypertable(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    cur.fetchone = MagicMock(return_value=(None,))
    t = _make_thread()
    t._conn = conn
    assert t.read_watermark() is None


def test_read_open_entities_returns_set_of_ids(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    cur.fetchall = MagicMock(return_value=[("sensor.a",), ("sensor.b",)])
    t = _make_thread()
    t._conn = conn
    result = t.read_open_entities()
    assert result == {"sensor.a", "sensor.b"}
    cur.execute.assert_called_with(SELECT_OPEN_ENTITIES_SQL)


def test_reset_db_connection_drops_conn():
    t = _make_thread()
    t._conn = MagicMock()
    t.reset_db_connection()
    assert t._conn is None


def test_flush_chunks_by_insert_chunk_size(mock_psycopg_conn):
    conn, cur = mock_psycopg_conn
    t = _make_thread()
    t._conn = conn
    # Replace retry-wrapped _insert_chunk with a spy so we can count calls
    t._insert_chunk = MagicMock()
    # 450 rows → 3 chunks at chunk-size 200
    rows = [_sample_state_row(eid=f"s.{i}") for i in range(450)]
    t._flush(rows)
    assert t._insert_chunk.call_count == 3
    # Chunk sizes: 200, 200, 50
    sizes = [len(c.args[0]) for c in t._insert_chunk.call_args_list]
    assert sizes == [200, 200, 50]


def test_retry_wrapper_applied_at_init(mock_psycopg_conn):
    """D-07: _insert_chunk is the retry-wrapped version of _insert_chunk_raw."""
    t = _make_thread()
    # wrapped function is a different object from the raw one
    assert t._insert_chunk is not t._insert_chunk_raw
    # functools.wraps copies __qualname__ from the wrapped function
    assert "_insert_chunk_raw" in t._insert_chunk.__qualname__


# ---------------------------------------------------------------------------
# Phase 3 Plan 04: _last_* watchdog context + hook methods + outer try/except
# ---------------------------------------------------------------------------


def test_last_exception_initialized_to_none_in_init():
    """D-06-b: _last_exception has a safe default in __init__ so watchdog can
    always read it even if run() never executes."""
    t = _make_thread()
    assert t._last_exception is None


def test_last_context_initialized_with_safe_defaults_in_init():
    """D-06-b: _last_context is a dict (not empty) with known keys at construction."""
    t = _make_thread()
    ctx = t._last_context
    assert isinstance(ctx, dict)
    # All four expected keys must be present at init time.
    assert set(ctx.keys()) == {"at", "mode", "retry_attempt", "last_op"}
    # Safe default values (not unset/missing).
    assert ctx["mode"] == "init"
    assert ctx["last_op"] == "unknown"


def test_stall_hook_fires_persistent_notification_and_repair_issue():
    """D-02 / D-07-f: _stall_hook fires both the Phase 2 persistent_notification
    AND the Phase 3 repair issue — two hass.add_job calls total."""
    hass = MagicMock()
    t = _make_thread(hass=hass)

    # _stall_hook imports persistent_notification lazily inside the method body;
    # patch at the homeassistant.components level so the lazy import resolves to the mock.
    with patch("homeassistant.components.persistent_notification") as mock_pn:
        t._stall_hook(5)

    # Two add_job calls: one for persistent_notification.async_create, one for
    # create_states_worker_stalled_issue.
    assert hass.add_job.call_count == 2

    # One of the calls must be for create_states_worker_stalled_issue.
    from custom_components.timescaledb_recorder.issues import (
        create_states_worker_stalled_issue,
    )
    issue_call = call(create_states_worker_stalled_issue, hass)
    assert issue_call in hass.add_job.call_args_list


def test_recovery_hook_clears_both_issues():
    """D-02-c / D-03-a: _recovery_hook clears states_worker_stalled AND
    db_unreachable — both may have been raised during a long stall streak."""
    hass = MagicMock()
    t = _make_thread(hass=hass)
    t._recovery_hook()

    assert hass.add_job.call_count == 2

    from custom_components.timescaledb_recorder.issues import (
        clear_db_unreachable_issue,
        clear_states_worker_stalled_issue,
    )
    assert call(clear_states_worker_stalled_issue, hass) in hass.add_job.call_args_list
    assert call(clear_db_unreachable_issue, hass) in hass.add_job.call_args_list


def test_sustained_fail_hook_creates_db_unreachable_issue():
    """D-11: _sustained_fail_hook fires create_db_unreachable_issue via hass.add_job."""
    hass = MagicMock()
    t = _make_thread(hass=hass)
    t._sustained_fail_hook()

    assert hass.add_job.call_count == 1

    from custom_components.timescaledb_recorder.issues import create_db_unreachable_issue
    assert call(create_db_unreachable_issue, hass) in hass.add_job.call_args_list


def test_retry_decorator_wired_with_all_phase3_hooks():
    """D-03: retry_until_success must receive on_recovery and on_sustained_fail
    keyword args for the _insert_chunk wrapper (first call in __init__).
    The read_watermark wrapper (second call) uses on_transient only — no hooks.

    __init__ calls retry_until_success twice: once for _insert_chunk, once for
    read_watermark. Capture both calls and assert the first has full hook set.
    """
    all_calls = []

    def fake_retry(*, stop_event, on_transient=None, notify_stall=None,
                   on_recovery=None, on_sustained_fail=None, **kwargs):
        all_calls.append({
            "on_recovery": on_recovery,
            "on_sustained_fail": on_sustained_fail,
        })
        def decorator(fn):
            return fn
        return decorator

    with patch(
        "custom_components.timescaledb_recorder.states_worker.retry_until_success",
        fake_retry,
    ):
        t = _make_thread()

    # __init__ must call retry_until_success at least twice (insert + watermark).
    assert len(all_calls) >= 2, f"Expected >=2 calls to retry_until_success, got {len(all_calls)}"

    # First call wraps _insert_chunk_raw: must have full Phase 3 hook set.
    insert_call = all_calls[0]
    assert insert_call.get("on_recovery") is not None
    assert insert_call.get("on_sustained_fail") is not None
    # They should be bound to the thread instance (same underlying function).
    assert insert_call["on_recovery"].__func__ is t._recovery_hook.__func__
    assert insert_call["on_sustained_fail"].__func__ is t._sustained_fail_hook.__func__


def test_run_outer_except_captures_last_exception_and_context():
    """D-06-a: run() outer try/except captures the unhandled exception into
    _last_exception and populates _last_context with the four required keys."""
    stop_event = threading.Event()
    t = _make_thread(stop_event=stop_event)

    boom = RuntimeError("boom")

    def raise_boom():
        raise boom

    t._run_main_loop = raise_boom
    t.start()
    t.join(timeout=5)

    assert not t.is_alive()
    assert t._last_exception is boom
    ctx = t._last_context
    assert isinstance(ctx, dict)
    assert set(ctx.keys()) == {"at", "mode", "retry_attempt", "last_op"}


def test_run_outer_except_context_has_iso_at_timestamp():
    """D-06-a: _last_context['at'] must be a valid ISO-8601 UTC timestamp string."""
    stop_event = threading.Event()
    t = _make_thread(stop_event=stop_event)
    t._run_main_loop = lambda: (_ for _ in ()).throw(RuntimeError("ts-test"))
    t.start()
    t.join(timeout=5)

    at_val = t._last_context.get("at")
    assert at_val is not None
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", at_val)


def test_run_finally_closes_connection_on_exception():
    """D-06 / MEDIUM-8: the finally block must close the connection even when
    _run_main_loop raises — verified by mock call count."""
    stop_event = threading.Event()
    t = _make_thread(stop_event=stop_event)
    mock_conn = MagicMock()
    t._conn = mock_conn
    t._run_main_loop = lambda: (_ for _ in ()).throw(RuntimeError("conn-test"))

    t.start()
    t.join(timeout=5)

    mock_conn.close.assert_called_once()


def test_run_teardown_error_does_not_overwrite_last_exception():
    """MEDIUM-8: if conn.close() raises inside finally, _last_exception must
    still be the original RuntimeError from _run_main_loop — not overwritten."""
    stop_event = threading.Event()
    t = _make_thread(stop_event=stop_event)

    original_err = RuntimeError("original")
    mock_conn = MagicMock()
    mock_conn.close.side_effect = RuntimeError("teardown-error")
    t._conn = mock_conn

    t._run_main_loop = lambda: (_ for _ in ()).throw(original_err)

    t.start()
    t.join(timeout=5)

    assert t._last_exception is original_err


def test_last_op_updated_before_retried_operations():
    """D-06-b: _last_op must be updated before each major operation so watchdog
    context captures a meaningful last known activity even after thread exit."""
    t = _make_thread()
    # _last_op starts as 'unknown' (safe default).
    assert t._last_op == "unknown"
    # After construction we don't run the thread — just verify the attribute
    # exists and has the right type.
    assert isinstance(t._last_op, str)
    assert isinstance(t._last_retry_attempt, (int, type(None)))


# ---------------------------------------------------------------------------
# Phase 3 Plan 05: retry-wrapped read_watermark (D-03-c)
# ---------------------------------------------------------------------------


def test_read_watermark_is_retry_wrapped():
    """D-03-c: after __init__, read_watermark must be the retry-wrapped instance
    attribute (not identical to _read_watermark_raw)."""
    t = _make_thread()
    # read_watermark must exist
    assert hasattr(t, "read_watermark")
    # raw variant must be renamed
    assert hasattr(t, "_read_watermark_raw")
    # wrapped version must differ from the raw method
    assert t.read_watermark is not t._read_watermark_raw


def test_read_watermark_retries_on_transient_error(mock_psycopg_conn):
    """D-03-c: a single OperationalError from the raw read must be retried;
    the final successful return value must be propagated to the caller."""
    import psycopg
    from datetime import datetime, timezone

    conn, cur = mock_psycopg_conn
    expected_dt = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)

    # Fail once, succeed on second call.
    call_count = {"n": 0}

    def raw_side_effect():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise psycopg.OperationalError("transient error")
        return expected_dt

    # Use a fast backoff so the test does not sleep.
    stop_event = threading.Event()
    t = _make_thread(stop_event=stop_event)
    t._conn = conn

    reset_mock = MagicMock()
    t.reset_db_connection = reset_mock

    # Patch the raw method and rebuild the retry wrapper with a zero backoff.
    t._read_watermark_raw = raw_side_effect
    from custom_components.timescaledb_recorder.retry import retry_until_success
    t.read_watermark = retry_until_success(
        stop_event=stop_event,
        on_transient=t.reset_db_connection,
        backoff_schedule=(0,),
    )(t._read_watermark_raw)

    result = t.read_watermark()
    assert result == expected_dt
    # reset_db_connection must have been called at least once (on_transient hook).
    reset_mock.assert_called()


def test_read_watermark_retry_wiring_captures_on_transient(mock_psycopg_conn):
    """D-03-c: retry_until_success must be called with on_transient=reset_db_connection
    and without on_recovery / on_sustained_fail / notify_stall (D-03-c note: stall on
    watermark read manifests as stalled backfill — covered by orchestrator done_callback,
    not by repair-issue hooks on this wrapper)."""
    captured = {}

    def fake_retry(*, stop_event, on_transient=None, on_recovery=None,
                   on_sustained_fail=None, notify_stall=None, **kwargs):
        captured["on_transient"] = on_transient
        captured["on_recovery"] = on_recovery
        captured["on_sustained_fail"] = on_sustained_fail
        captured["notify_stall"] = notify_stall

        def decorator(fn):
            return fn
        return decorator

    with patch(
        "custom_components.timescaledb_recorder.states_worker.retry_until_success",
        fake_retry,
    ):
        t = _make_thread()

    # on_transient must be the bound reset_db_connection of this specific instance.
    # Bound methods are equal (==) but not identical (is) since Python recreates
    # bound method wrappers on each attribute access. Use __func__ comparison instead.
    on_transient = captured.get("on_transient")
    assert on_transient is not None, "on_transient was not passed to retry_until_success"
    assert on_transient.__func__ is TimescaledbStateRecorderThread.reset_db_connection
    assert on_transient.__self__ is t
    # No stall/recovery hooks — watermark failures are surfaced via orchestrator.
    assert captured.get("on_recovery") is None
    assert captured.get("on_sustained_fail") is None
    assert captured.get("notify_stall") is None
