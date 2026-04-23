"""Tests for Phase 3 constants in const.py (D-03-d, D-05-c, D-11)."""


def test_phase3_constants_importable():
    from custom_components.ha_timescaledb_recorder.const import (
        STALL_THRESHOLD,
        WATCHDOG_INTERVAL_S,
        DB_UNREACHABLE_THRESHOLD_SECONDS,
    )
    assert STALL_THRESHOLD == 5
    assert isinstance(STALL_THRESHOLD, int)
    assert WATCHDOG_INTERVAL_S == 10.0
    assert isinstance(WATCHDOG_INTERVAL_S, float)
    assert DB_UNREACHABLE_THRESHOLD_SECONDS == 300.0
    assert isinstance(DB_UNREACHABLE_THRESHOLD_SECONDS, float)


def test_phase2_constants_unchanged():
    """Regression guard — Phase 2 constants must retain their exact values."""
    from custom_components.ha_timescaledb_recorder.const import (
        BATCH_FLUSH_SIZE,
        INSERT_CHUNK_SIZE,
        FLUSH_INTERVAL,
        LIVE_QUEUE_MAXSIZE,
        BACKFILL_QUEUE_MAXSIZE,
    )
    assert BATCH_FLUSH_SIZE == 200
    assert INSERT_CHUNK_SIZE == 200
    assert FLUSH_INTERVAL == 5.0
    assert LIVE_QUEUE_MAXSIZE == 10000
    assert BACKFILL_QUEUE_MAXSIZE == 2
