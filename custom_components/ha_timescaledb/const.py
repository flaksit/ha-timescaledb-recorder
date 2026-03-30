"""Constants for the TimescaleDB Recorder integration."""

DOMAIN = "ha_timescaledb"

# Defaults
DEFAULT_BATCH_SIZE = 200
DEFAULT_FLUSH_INTERVAL = 10  # seconds
DEFAULT_COMPRESS_AFTER_DAYS = 7
DEFAULT_CHUNK_INTERVAL_DAYS = 7

# Config keys
CONF_DSN = "dsn"
CONF_BATCH_SIZE = "batch_size"
CONF_FLUSH_INTERVAL = "flush_interval"
CONF_COMPRESS_AFTER = "compress_after_days"
CONF_INGEST_UNAVAILABLE = "ingest_unavailable"

TABLE_NAME = "ha_states"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ha_states (
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

SET_COMPRESSION_SQL = """
ALTER TABLE ha_states SET (
    timescaledb.compress = TRUE,
    timescaledb.compress_segmentby = 'entity_id',
    timescaledb.compress_orderby = 'last_updated DESC');
"""

# {compress_days} must be formatted before execution
ADD_COMPRESSION_POLICY_SQL = """
SELECT add_compression_policy('ha_states',
    INTERVAL '{compress_days} days', if_not_exists => TRUE);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_ha_states_entity_time
    ON ha_states (entity_id, last_updated DESC);
"""

INSERT_SQL = """
INSERT INTO ha_states (entity_id, state, attributes, last_updated, last_changed)
VALUES ($1, $2, $3, $4, $5)
"""
