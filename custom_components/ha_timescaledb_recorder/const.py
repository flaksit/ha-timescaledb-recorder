"""Constants for the TimescaleDB Recorder integration."""

DOMAIN = "ha_timescaledb_recorder"

# Defaults
DEFAULT_BATCH_SIZE = 200
DEFAULT_FLUSH_INTERVAL = 10  # seconds
DEFAULT_CHUNK_INTERVAL_DAYS = 7
# 2 hours keeps at most ~1 day of uncompressed data on disk (2-day chunk + 12h policy window).
DEFAULT_COMPRESS_AFTER_HOURS = 2

# Config keys
CONF_DSN = "dsn"
CONF_BATCH_SIZE = "batch_size"
CONF_FLUSH_INTERVAL = "flush_interval"
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
CREATE_HYPERTABLE_SQL = """
SELECT create_hypertable('ha_states', 'last_updated',
    chunk_time_interval => INTERVAL '{chunk_days} days',
    if_not_exists => TRUE);
"""

SET_COMPRESSION_SQL = f"""
ALTER TABLE {TABLE_NAME} SET (
    timescaledb.compress = TRUE,
    timescaledb.compress_segmentby = 'entity_id',
    timescaledb.compress_orderby = 'last_updated DESC');
"""

REMOVE_COMPRESSION_POLICY_SQL = """
SELECT remove_compression_policy('ha_states', if_exists => TRUE);
"""

# {compress_hours} and {schedule_hours} must be formatted before execution.
# schedule_hours = max(1, min(12, compress_hours // 2)) — runs at half the
# compression window, capped at 12 h to avoid excessive polling.
ADD_COMPRESSION_POLICY_SQL = """
SELECT add_compression_policy('ha_states',
    INTERVAL '{compress_hours} hours',
    schedule_interval => INTERVAL '{schedule_hours} hours');
"""

CREATE_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_entity_time
    ON {TABLE_NAME} (entity_id, last_updated DESC);
"""

INSERT_SQL = f"""
INSERT INTO {TABLE_NAME} (entity_id, state, attributes, last_updated, last_changed)
VALUES ($1, $2, $3, $4, $5)
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
# $1 = valid_to timestamp, $2 = entity_id.
SCD2_CLOSE_ENTITY_SQL = """
UPDATE dim_entities
SET valid_to = $1
WHERE entity_id = $2 AND valid_to IS NULL;
"""

SCD2_CLOSE_DEVICE_SQL = """
UPDATE dim_devices
SET valid_to = $1
WHERE device_id = $2 AND valid_to IS NULL;
"""

SCD2_CLOSE_AREA_SQL = """
UPDATE dim_areas
SET valid_to = $1
WHERE area_id = $2 AND valid_to IS NULL;
"""

SCD2_CLOSE_LABEL_SQL = """
UPDATE dim_labels
SET valid_to = $1
WHERE label_id = $2 AND valid_to IS NULL;
"""

# Idempotent snapshot inserts (Pitfall 3 mitigation).
# Uses WHERE NOT EXISTS so re-running on HA restart does not create duplicate
# open rows for entities already present in the dimension table.
# $1=entity_id, $2=ha_entity_uuid, $3=name, $4=domain, $5=platform,
# $6=device_id, $7=area_id, $8=labels, $9=device_class,
# $10=unit_of_measurement, $11=disabled_by, $12=valid_from, $13=extra
SCD2_SNAPSHOT_ENTITY_SQL = """
INSERT INTO dim_entities
    (entity_id, ha_entity_uuid, name, domain, platform, device_id, area_id,
     labels, device_class, unit_of_measurement, disabled_by, valid_from, valid_to, extra)
SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NULL, $13
WHERE NOT EXISTS (
    SELECT 1 FROM dim_entities WHERE entity_id = $1 AND valid_to IS NULL
);
"""

# $1=device_id, $2=name, $3=manufacturer, $4=model,
# $5=area_id, $6=labels, $7=valid_from, $8=extra
SCD2_SNAPSHOT_DEVICE_SQL = """
INSERT INTO dim_devices
    (device_id, name, manufacturer, model, area_id, labels, valid_from, valid_to, extra)
SELECT $1, $2, $3, $4, $5, $6, $7, NULL, $8
WHERE NOT EXISTS (
    SELECT 1 FROM dim_devices WHERE device_id = $1 AND valid_to IS NULL
);
"""

# $1=area_id, $2=name, $3=valid_from, $4=extra
SCD2_SNAPSHOT_AREA_SQL = """
INSERT INTO dim_areas
    (area_id, name, valid_from, valid_to, extra)
SELECT $1, $2, $3, NULL, $4
WHERE NOT EXISTS (
    SELECT 1 FROM dim_areas WHERE area_id = $1 AND valid_to IS NULL
);
"""

# $1=label_id, $2=name, $3=color, $4=valid_from, $5=extra
SCD2_SNAPSHOT_LABEL_SQL = """
INSERT INTO dim_labels
    (label_id, name, color, valid_from, valid_to, extra)
SELECT $1, $2, $3, $4, NULL, $5
WHERE NOT EXISTS (
    SELECT 1 FROM dim_labels WHERE label_id = $1 AND valid_to IS NULL
);
"""

# Plain inserts used for the new-row step of the SCD2 close-and-insert cycle
# (incremental updates after snapshot; the close step runs first).
# Parameters match snapshot SQL but without the WHERE NOT EXISTS guard.
SCD2_INSERT_ENTITY_SQL = """
INSERT INTO dim_entities
    (entity_id, ha_entity_uuid, name, domain, platform, device_id, area_id,
     labels, device_class, unit_of_measurement, disabled_by, valid_from, valid_to, extra)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NULL, $13);
"""

SCD2_INSERT_DEVICE_SQL = """
INSERT INTO dim_devices
    (device_id, name, manufacturer, model, area_id, labels, valid_from, valid_to, extra)
VALUES ($1, $2, $3, $4, $5, $6, $7, NULL, $8);
"""

SCD2_INSERT_AREA_SQL = """
INSERT INTO dim_areas
    (area_id, name, valid_from, valid_to, extra)
VALUES ($1, $2, $3, NULL, $4);
"""

SCD2_INSERT_LABEL_SQL = """
INSERT INTO dim_labels
    (label_id, name, color, valid_from, valid_to, extra)
VALUES ($1, $2, $3, $4, NULL, $5);
"""
