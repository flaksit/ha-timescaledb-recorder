"""Database-backed tests for sync_setup_schema (issue #15).

Installs from before the dim_ prefix was dropped carry the old index names,
some alongside identical copies under the new names. The mocks in test_schema.py
only see SQL text, not that the server ends up with one index per column set
under the new name.

DESTRUCTIVE: drops and recreates the dimension tables and `states`. Same setup
and guard as tests/test_scd2_invariant_db.py.
"""
import os
from collections.abc import Iterator

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402  pyright: ignore[reportMissingImports]

from custom_components.timescaledb_recorder import const  # noqa: E402
from custom_components.timescaledb_recorder.schema import sync_setup_schema  # noqa: E402
from tests.db_guard import assert_disposable  # noqa: E402

DSN = os.environ.get("SCD2_TEST_DSN")

pytestmark = [
    pytest.mark.skipif(not DSN, reason="SCD2_TEST_DSN not set"),
    pytest.mark.enable_socket,
]

LEGACY_INDEXES_SQL = (
    "CREATE INDEX idx_dim_entities_entity_time ON entities (entity_id, valid_from DESC)",
    "CREATE INDEX idx_dim_entities_current ON entities (entity_id) WHERE valid_to IS NULL",
    "CREATE INDEX idx_dim_devices_device_time ON devices (device_id, valid_from DESC)",
    "CREATE INDEX idx_dim_labels_label_time ON labels (label_id, valid_from DESC)",
)
NEW_INDEXES_SQL = (
    "CREATE INDEX idx_entities_entity_time ON entities (entity_id, valid_from DESC)",
    "CREATE INDEX idx_entities_current ON entities (entity_id) WHERE valid_to IS NULL",
    "CREATE INDEX idx_devices_device_time ON devices (device_id, valid_from DESC)",
    "CREATE INDEX idx_labels_label_time ON labels (label_id, valid_from DESC)",
)
EXPECTED = [
    "idx_areas_area_time", "idx_devices_device_time", "idx_entities_current",
    "idx_entities_entity_time", "idx_labels_label_time",
]


@pytest.fixture(params=[
    pytest.param(LEGACY_INDEXES_SQL, id="legacy-only"),
    pytest.param(LEGACY_INDEXES_SQL + NEW_INDEXES_SQL, id="legacy-and-new"),
    pytest.param((), id="fresh"),
])
def legacy_conn(request: pytest.FixtureRequest) -> Iterator[psycopg.Connection]:
    """Dimension tables carrying the given pre-existing indexes."""
    assert DSN  # pytestmark skips the module without it
    with psycopg.connect(DSN, autocommit=True) as c:
        assert_disposable(c)
        with c.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS entities, devices, areas, labels, states CASCADE")
            for ddl in (const.CREATE_ENTITIES_SQL, const.CREATE_DEVICES_SQL,
                        const.CREATE_AREAS_SQL, const.CREATE_LABELS_SQL):
                cur.execute(ddl)
            for ddl in request.param:
                cur.execute(ddl)
        yield c


def test_legacy_indexes_migrated_not_duplicated(legacy_conn: psycopg.Connection) -> None:
    sync_setup_schema(legacy_conn)
    sync_setup_schema(legacy_conn)  # runs on every startup, so must stay a no-op

    with legacy_conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes"
                    " WHERE tablename IN ('entities', 'devices', 'areas', 'labels')"
                    " AND indexname LIKE 'idx\\_%' ORDER BY indexname")
        assert [r[0] for r in cur.fetchall()] == EXPECTED
