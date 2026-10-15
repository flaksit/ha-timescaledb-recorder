"""Database-backed tests for the states worker's poison-row path (issue #20).

The mocks in test_states_worker.py fake the failure; these check that the real
server rejects a NUL-bearing row with an error the classifier calls permanent,
and that the dead-letter table accepts the very row the states table refused.

DESTRUCTIVE: drops and recreates `states` and `states_deadletter`. Same setup
and guard as tests/test_scd2_invariant_db.py.
"""
import os
import queue
import threading
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402  pyright: ignore[reportMissingImports]

from custom_components.timescaledb_recorder.overflow_queue import OverflowQueue  # noqa: E402
from custom_components.timescaledb_recorder.retry import is_transient  # noqa: E402
from custom_components.timescaledb_recorder.schema import sync_setup_schema  # noqa: E402
from custom_components.timescaledb_recorder.states_worker import (  # noqa: E402
    TimescaledbStateRecorderThread,
)
from custom_components.timescaledb_recorder.worker import StateRow  # noqa: E402
from tests.db_guard import assert_disposable  # noqa: E402

DSN = os.environ.get("SCD2_TEST_DSN", "")

pytestmark = [
    pytest.mark.skipif(not DSN, reason="SCD2_TEST_DSN not set"),
    pytest.mark.enable_socket,
]

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        assert_disposable(c)
        with c.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS states, states_deadletter CASCADE")
        sync_setup_schema(c)
        yield c


def _row(eid: str, attributes: dict[str, Any], state: str = "on") -> StateRow:
    return StateRow(entity_id=eid, state=state, attributes=attributes,
                    last_updated=T0, last_changed=T0)


@pytest.mark.parametrize("bad", [
    _row("sensor.bad", {"note": "nul\x00here"}),  # jsonb rejects \u0000
    _row("sensor.bad", {}, state="nul\x00here"),  # text rejects NUL
])
def test_poison_row_is_dead_lettered_and_neighbours_land(conn, bad):
    stop = threading.Event()
    # Skip the real backoff sleeps; the decorator holds this same Event.
    stop.wait = MagicMock(return_value=False)
    worker = TimescaledbStateRecorderThread(
        hass=MagicMock(), dsn=DSN, live_queue=OverflowQueue(maxsize=10),
        backfill_queue=queue.Queue(), backfill_request=MagicMock(), stop_event=stop,
        chunk_interval_days=7, compress_after_hours=2,
    )

    with pytest.raises(psycopg.Error) as rejected:
        worker._insert_chunk_raw([bad])
    assert not is_transient(rejected.value)

    worker._flush([_row("sensor.a", {"k": 1}), bad, _row("sensor.b", {})])
    worker.reset_db_connection()

    with conn.cursor() as cur:
        cur.execute("SELECT entity_id FROM states ORDER BY entity_id")
        assert [r[0] for r in cur.fetchall()] == ["sensor.a", "sensor.b"]
        # row_data stays TEXT here: casting it to jsonb fails on the very \u0000
        # that made the row poison.
        cur.execute("SELECT entity_id, last_updated, row_data FROM states_deadletter")
        dead = cur.fetchall()
    assert [(e, t) for e, t, _ in dead] == [("sensor.bad", T0)]
    assert '"entity_id": "sensor.bad"' in dead[0][2]
    assert worker.rows_dropped == 1
