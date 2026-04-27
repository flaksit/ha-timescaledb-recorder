"""Constants for the TimescaleDB Recorder integration."""

DOMAIN = "ha_timescaledb_recorder"

# Defaults
DEFAULT_BATCH_SIZE = 200
DEFAULT_FLUSH_INTERVAL = 10  # seconds
DEFAULT_CHUNK_INTERVAL_DAYS = 7
# 2 hours keeps at most ~1 day of uncompressed data on disk (2-day chunk + 12h policy window).
DEFAULT_COMPRESS_AFTER_HOURS = 2

# Phase 2 ingestion tunables (D-04-e, D-06-a, D-01-a, D-01-b). These are
# internal to the states worker loop — they are NOT user-configurable via
# options flow. Intentionally distinct from DEFAULT_BATCH_SIZE /
# DEFAULT_FLUSH_INTERVAL above (which are the user-facing options-flow
# defaults from Phase 1).
BATCH_FLUSH_SIZE: int = 200       # D-04-e: flush when buffer reaches this size
INSERT_CHUNK_SIZE: int = 200      # D-06-a: sub-batch size per _insert_chunk call
FLUSH_INTERVAL: float = 5.0       # D-04-e seconds — adaptive get() timeout
LIVE_QUEUE_MAXSIZE: int = 10000   # D-01-a: OverflowQueue cap
BACKFILL_QUEUE_MAXSIZE: int = 2   # D-01-b: backpressure cap for backfill_queue

# Phase 3 observability tunables (D-03-d, D-05-c, D-11).
# STALL_THRESHOLD — after this many consecutive retry failures, the
#   notify_stall hook fires once and the worker_stalled repair issue is raised.
#   Matches Phase 2's previous retry._STALL_NOTIFY_THRESHOLD (which Plan 03
#   will remove in favour of importing from here).
# WATCHDOG_INTERVAL_S — polling cadence for watchdog_loop (seconds).
#   10s balances detection latency against event-loop overhead.
# DB_UNREACHABLE_THRESHOLD_SECONDS — cumulative fail duration at which the
#   db_unreachable repair issue is raised via retry decorator's
#   on_sustained_fail hook (D-11).
STALL_THRESHOLD: int = 5
WATCHDOG_INTERVAL_S: float = 10.0
DB_UNREACHABLE_THRESHOLD_SECONDS: float = 300.0

# Config keys
CONF_DSN = "dsn"
CONF_BATCH_SIZE = "write_batch_size_records"
CONF_FLUSH_INTERVAL = "flush_interval_seconds"
CONF_COMPRESS_AFTER = "compress_after_hours"
CONF_CHUNK_INTERVAL = "chunk_interval_days"

TABLE_NAME = "ha_states"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    last_updated  TIMESTAMPTZ NOT NULL,
    last_changed  TIMESTAMPTZ NOT NULL,
    entity_id     TEXT        NOT NULL,
    state         TEXT,
    attributes    JSONB
);
"""

# {chunk_days} must be formatted before execution
CREATE_HYPERTABLE_SQL = f"""
SELECT create_hypertable('{TABLE_NAME}', 'last_updated',
    chunk_time_interval => INTERVAL '{{chunk_days}} days',
    if_not_exists => TRUE);
"""

SET_COMPRESSION_SQL = f"""
ALTER TABLE {TABLE_NAME} SET (
    timescaledb.compress = TRUE,
    timescaledb.compress_segmentby = 'entity_id',
    timescaledb.compress_orderby = 'last_updated DESC');
"""

REMOVE_COMPRESSION_POLICY_SQL = f"""
SELECT remove_compression_policy('{TABLE_NAME}', if_exists => TRUE);
"""

# {compress_hours} and {schedule_hours} must be formatted before execution.
# schedule_hours = max(1, min(12, compress_hours // 2)) — runs at half the
# compression window, capped at 12 h to avoid excessive polling.
ADD_COMPRESSION_POLICY_SQL = f"""
SELECT add_compression_policy('{TABLE_NAME}',
    INTERVAL '{{compress_hours}} hours',
    schedule_interval => INTERVAL '{{schedule_hours}} hours');
"""

CREATE_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_entity_time
    ON {TABLE_NAME} (entity_id, last_updated DESC);
"""

# D-09-a: unique index — enables ON CONFLICT DO NOTHING dedup on every
# INSERT (D-06-d). TimescaleDB hypertables allow unique indexes as long as
# the partitioning column (last_updated) is included.
CREATE_UNIQUE_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS idx_{TABLE_NAME}_uniq
    ON {TABLE_NAME} (last_updated, entity_id);
"""

INSERT_SQL = f"""
INSERT INTO {TABLE_NAME} (entity_id, state, attributes, last_updated, last_changed)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (last_updated, entity_id) DO NOTHING
"""

# Live-capture insert overwrites existing rows — live state_changed events carry
# the full HA state-machine attributes, which are always more complete than what
# the HA SQLite recorder stores (it filters certain attributes, e.g. automation
# id/mode/current/last_triggered). Backfill still uses DO NOTHING so it never
# overwrites a live-captured row.
INSERT_LIVE_SQL = f"""
INSERT INTO {TABLE_NAME} (entity_id, state, attributes, last_updated, last_changed)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (last_updated, entity_id) DO UPDATE
    SET state      = EXCLUDED.state,
        attributes = EXCLUDED.attributes
"""

# Dimension table DDL — SCD2 temporal tracking for HA registry metadata.
# All tables are idempotent (CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS)
# so they can safely execute on every integration startup (D-11).

CREATE_DIM_ENTITIES_SQL = """
CREATE TABLE IF NOT EXISTS dim_entities (
    entity_id           TEXT        NOT NULL,
    ha_entity_uuid      TEXT        NOT NULL,
    name                TEXT,
    domain              TEXT        NOT NULL,
    platform            TEXT,
    device_id           TEXT,
    area_id             TEXT,
    labels              TEXT[],
    device_class        TEXT,
    unit_of_measurement TEXT,
    disabled_by         TEXT,
    valid_from          TIMESTAMPTZ NOT NULL,
    valid_to            TIMESTAMPTZ,
    extra               JSONB
);
"""

CREATE_DIM_DEVICES_SQL = """
CREATE TABLE IF NOT EXISTS dim_devices (
    device_id   TEXT        NOT NULL,
    name        TEXT,
    manufacturer TEXT,
    model       TEXT,
    area_id     TEXT,
    labels      TEXT[],
    valid_from  TIMESTAMPTZ NOT NULL,
    valid_to    TIMESTAMPTZ,
    extra       JSONB
);
"""

CREATE_DIM_AREAS_SQL = """
CREATE TABLE IF NOT EXISTS dim_areas (
    area_id    TEXT        NOT NULL,
    name       TEXT,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to   TIMESTAMPTZ,
    extra      JSONB
);
"""

CREATE_DIM_LABELS_SQL = """
CREATE TABLE IF NOT EXISTS dim_labels (
    label_id   TEXT        NOT NULL,
    name       TEXT,
    color      TEXT,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to   TIMESTAMPTZ,
    extra      JSONB
);
"""

# Indexes for dimension tables.
# Compound index on (id, valid_from DESC) supports history range scans.
# Partial index WHERE valid_to IS NULL supports fast current-row lookups
# (the primary access pattern for Grafana joins in Phase 7).
CREATE_DIM_ENTITIES_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_dim_entities_entity_time
    ON dim_entities (entity_id, valid_from DESC);
"""

# Partial index — avoids scanning historical rows when only current state is needed.
CREATE_DIM_ENTITIES_CURRENT_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_dim_entities_current
    ON dim_entities (entity_id)
    WHERE valid_to IS NULL;
"""

CREATE_DIM_DEVICES_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_dim_devices_device_time
    ON dim_devices (device_id, valid_from DESC);
"""

CREATE_DIM_AREAS_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_dim_areas_area_time
    ON dim_areas (area_id, valid_from DESC);
"""

CREATE_DIM_LABELS_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_dim_labels_label_time
    ON dim_labels (label_id, valid_from DESC);
"""

# SCD2 close-and-insert SQL.
# Convention: separate constants per table (not a .format() template) to keep
# SQL strings explicit, grep-able, and safe from accidental table injection.

# Close (expire) the currently-open row for an entity.
# %s = valid_to timestamp, %s = entity_id.
SCD2_CLOSE_ENTITY_SQL = """
UPDATE dim_entities
SET valid_to = %s
WHERE entity_id = %s AND valid_to IS NULL;
"""

SCD2_CLOSE_DEVICE_SQL = """
UPDATE dim_devices
SET valid_to = %s
WHERE device_id = %s AND valid_to IS NULL;
"""

SCD2_CLOSE_AREA_SQL = """
UPDATE dim_areas
SET valid_to = %s
WHERE area_id = %s AND valid_to IS NULL;
"""

SCD2_CLOSE_LABEL_SQL = """
UPDATE dim_labels
SET valid_to = %s
WHERE label_id = %s AND valid_to IS NULL;
"""

# Idempotent snapshot inserts (Pitfall 3 mitigation).
# Uses WHERE NOT EXISTS so re-running on HA restart does not create duplicate
# open rows for entities already present in the dimension table.
# %s=entity_id, %s=ha_entity_uuid, %s=name, %s=domain, %s=platform,
# %s=device_id, %s=area_id, %s=labels, %s=device_class,
# %s=unit_of_measurement, %s=disabled_by, %s=valid_from, %s=extra
# NOTE: first positional param (%s for entity_id) appears TWICE — once in SELECT, once in WHERE NOT EXISTS subquery.
SCD2_SNAPSHOT_ENTITY_SQL = """
INSERT INTO dim_entities
    (entity_id, ha_entity_uuid, name, domain, platform, device_id, area_id,
     labels, device_class, unit_of_measurement, disabled_by, valid_from, valid_to, extra)
SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s
WHERE NOT EXISTS (
    SELECT 1 FROM dim_entities WHERE entity_id = %s AND valid_to IS NULL
);
"""

# %s=device_id, %s=name, %s=manufacturer, %s=model,
# %s=area_id, %s=labels, %s=valid_from, %s=extra
# NOTE: first positional param (%s for device_id) appears TWICE — once in SELECT, once in WHERE NOT EXISTS subquery.
SCD2_SNAPSHOT_DEVICE_SQL = """
INSERT INTO dim_devices
    (device_id, name, manufacturer, model, area_id, labels, valid_from, valid_to, extra)
SELECT %s, %s, %s, %s, %s, %s, %s, NULL, %s
WHERE NOT EXISTS (
    SELECT 1 FROM dim_devices WHERE device_id = %s AND valid_to IS NULL
);
"""

# %s=area_id, %s=name, %s=valid_from, %s=extra
# NOTE: first positional param (%s for area_id) appears TWICE — once in SELECT, once in WHERE NOT EXISTS subquery.
SCD2_SNAPSHOT_AREA_SQL = """
INSERT INTO dim_areas
    (area_id, name, valid_from, valid_to, extra)
SELECT %s, %s, %s, NULL, %s
WHERE NOT EXISTS (
    SELECT 1 FROM dim_areas WHERE area_id = %s AND valid_to IS NULL
);
"""

# %s=label_id, %s=name, %s=color, %s=valid_from, %s=extra
# NOTE: first positional param (%s for label_id) appears TWICE — once in SELECT, once in WHERE NOT EXISTS subquery.
SCD2_SNAPSHOT_LABEL_SQL = """
INSERT INTO dim_labels
    (label_id, name, color, valid_from, valid_to, extra)
SELECT %s, %s, %s, %s, NULL, %s
WHERE NOT EXISTS (
    SELECT 1 FROM dim_labels WHERE label_id = %s AND valid_to IS NULL
);
"""

# Plain inserts used for the new-row step of the SCD2 close-and-insert cycle
# (incremental updates after snapshot; the close step runs first).
# Parameters match snapshot SQL but without the WHERE NOT EXISTS guard.
SCD2_INSERT_ENTITY_SQL = """
INSERT INTO dim_entities
    (entity_id, ha_entity_uuid, name, domain, platform, device_id, area_id,
     labels, device_class, unit_of_measurement, disabled_by, valid_from, valid_to, extra)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s);
"""

SCD2_INSERT_DEVICE_SQL = """
INSERT INTO dim_devices
    (device_id, name, manufacturer, model, area_id, labels, valid_from, valid_to, extra)
VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s);
"""

SCD2_INSERT_AREA_SQL = """
INSERT INTO dim_areas
    (area_id, name, valid_from, valid_to, extra)
VALUES (%s, %s, %s, NULL, %s);
"""

SCD2_INSERT_LABEL_SQL = """
INSERT INTO dim_labels
    (label_id, name, color, valid_from, valid_to, extra)
VALUES (%s, %s, %s, %s, NULL, %s);
"""

# D-08-d step 4: watermark read (orchestrator → states worker connection).
SELECT_WATERMARK_SQL = f"SELECT MAX(last_updated) FROM {TABLE_NAME}"

# D-08-f: all-known entities reader — dim_entities (all rows, incl. removed)
# unioned with all entity_ids ever written to ha_states.
# GROUP BY on the ha_states branch forces the planner to use the
# idx_ha_states_entity_time index before the UNION deduplication step.
# Plain UNION without pre-grouping causes a full hypertable scan (~27 s).
SELECT_ALL_KNOWN_ENTITIES_SQL = (
    "SELECT entity_id FROM dim_entities"
    " UNION"
    f" SELECT DISTINCT entity_id FROM {TABLE_NAME}"
)

# Change-detection SELECT constants — read the current open row for each registry type.
# Moved from inline strings in syncer.py per project convention (all SQL in const.py).
# %s = the registry ID (entity_id / device_id / area_id / label_id).
SELECT_ENTITY_CURRENT_SQL = (
    "SELECT name, platform, device_id, area_id, labels, device_class,"
    " unit_of_measurement, disabled_by, extra"
    " FROM dim_entities WHERE entity_id = %s AND valid_to IS NULL"
)

SELECT_DEVICE_CURRENT_SQL = (
    "SELECT name, manufacturer, model, area_id, labels, extra"
    " FROM dim_devices WHERE device_id = %s AND valid_to IS NULL"
)

SELECT_AREA_CURRENT_SQL = (
    "SELECT name, extra FROM dim_areas WHERE area_id = %s AND valid_to IS NULL"
)

SELECT_LABEL_CURRENT_SQL = (
    "SELECT name, color, extra FROM dim_labels WHERE label_id = %s AND valid_to IS NULL"
)
