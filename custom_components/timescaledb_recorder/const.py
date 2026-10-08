"""Constants for the TimescaleDB Recorder integration."""

DOMAIN = "timescaledb_recorder"

# Platform list — forwarded in async_setup_entry and unloaded in async_unload_entry.
PLATFORMS: list[str] = ["sensor", "binary_sensor"]

# HA dispatcher signals for push-updated sensor/binary_sensor entities.
# Sent whenever the signalled state transitions (both directions) so that
# subscribing entities can refresh without polling.
SIGNAL_OVERFLOW_CHANGE = f"{DOMAIN}_overflow_change"
SIGNAL_WORKER_STATE_CHANGE = f"{DOMAIN}_worker_state_change"
# Sent by every create_*/clear_* issue helper so health/db_status sensors
# update immediately rather than waiting for their poll interval.
SIGNAL_HEALTH_CHANGE = f"{DOMAIN}_health_change"

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

TABLE_NAME = "states"

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
CREATE TABLE IF NOT EXISTS entities (
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
CREATE TABLE IF NOT EXISTS devices (
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
CREATE TABLE IF NOT EXISTS areas (
    area_id    TEXT        NOT NULL,
    name       TEXT,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to   TIMESTAMPTZ,
    extra      JSONB
);
"""

CREATE_DIM_LABELS_SQL = """
CREATE TABLE IF NOT EXISTS labels (
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
    ON entities (entity_id, valid_from DESC);
"""

# Partial index — avoids scanning historical rows when only current state is needed.
CREATE_DIM_ENTITIES_CURRENT_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_dim_entities_current
    ON entities (entity_id)
    WHERE valid_to IS NULL;
"""

CREATE_DIM_DEVICES_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_dim_devices_device_time
    ON devices (device_id, valid_from DESC);
"""

CREATE_DIM_AREAS_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_areas_area_time
    ON areas (area_id, valid_from DESC);
"""

CREATE_DIM_LABELS_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_dim_labels_label_time
    ON labels (label_id, valid_from DESC);
"""

# Convenience views for query tools.
#
# `states.state` is TEXT because HA states are untyped strings — the same column
# holds "23.5", "on", and "unavailable". Every numeric query therefore needs a
# guarded cast, which SQL query builders (Grafana's included) cannot express:
# they emit `AVG(state)` from the column list and fail with
# "function avg(text) does not exist". These views do the cast once so the
# builders see a real numeric column and point-and-click exploration works.
#
# The regex accepts a leading minus (negative power = grid export, sub-zero
# temperatures) and optional exponent. Omitting the minus silently drops those
# rows instead of erroring, which is why the guard lives here rather than being
# retyped per query.
#
# CASE (not a WHERE filter) keeps non-numeric rows visible with value = NULL, so
# the views stay usable for text entities too. Aggregates ignore NULLs, so
# AVG/SUM/MIN/MAX over mixed entities still return the numeric answer.
#
# No GRANT here: init-db.sh in the ha-timescaledb app sets
# ALTER DEFAULT PRIVILEGES FOR ROLE homeassistant ... GRANT SELECT ON TABLES,
# and Postgres default privileges treat views as TABLES, so read-only roles
# inherit access automatically. An explicit GRANT would break deployments that
# have no such role.
NUMERIC_STATE_REGEX = r'^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$'

CREATE_VIEW_STATES_NUMERIC_SQL = f"""
CREATE OR REPLACE VIEW states_numeric AS
SELECT
    entity_id,
    last_updated,
    last_changed,
    state,
    CASE WHEN state ~ '{NUMERIC_STATE_REGEX}'
         THEN state::numeric
    END AS value,
    attributes
FROM states;
"""

# Point-in-time SCD2 join: every state row is labelled with the metadata that was
# current when the state was recorded.
#
# This is a straight temporal join on the recorded interval —
# [valid_from, valid_to), with an open version running to infinity. It trusts
# what the dimension says rather than reinterpreting it.
#
# Earlier versions deliberately ignored `valid_to` and synthesised gap-free eras
# from consecutive `valid_from` values, because the dimension held overlapping
# and duplicate-open rows and a literal join multiplied fact rows. That masked
# the corruption instead of showing it. With the exclusion constraint in place
# overlaps cannot recur, so the honest join is also the safe one, and anything
# the dimension fails to cover now shows up as NULL metadata instead of being
# silently papered over.
#
# Consequences, all intended:
#   1. A state recorded while no version covers it gets NULL metadata. That is a
#      visible symptom of missing history, not a cosmetic defect — investigate it
#      with `repair_scd2.py --verify-only`.
#   2. An entity removed from HA keeps metadata for the period it existed, and
#      has none afterwards, which is what the data actually records.
#   3. Renames stay historically correct: each version carries the name of its
#      time. GROUP BY entity_id, not entity_name, to keep a series together.
#
# Requires the dimension to satisfy the invariant. On a database that has not yet
# been repaired, overlapping versions will duplicate fact rows here exactly as
# they do in any other literal join — run repair_scd2.py first.
#
# LEFT JOIN throughout — an entity absent from the registry still has states, and
# dropping that history would make the view lie about totals.
_SCD2_ERA_JOIN = (
    "{alias}.{key} = {src} AND {ts} >= {alias}.valid_from"
    " AND {ts} < COALESCE({alias}.valid_to, 'infinity'::timestamptz)"
)

CREATE_VIEW_STATES_FLAT_SQL = f"""
CREATE OR REPLACE VIEW states_flat AS
SELECT
    s.entity_id,
    s.last_updated,
    s.last_changed,
    s.state,
    CASE WHEN s.state ~ '{NUMERIC_STATE_REGEX}'
         THEN s.state::numeric
    END AS value,
    e.name                AS entity_name,
    -- Derived from entity_id, not taken from the dimension. Identical by
    -- construction (registry_listener stores exactly this), but it also holds
    -- for entities HA never put in its entity registry — sun.sun, zone.home,
    -- conversation.*, YAML automations and helpers. Those have no dimension row
    -- and never will, so reading domain from `e` would leave a `WHERE domain =
    -- 'sensor'` filter silently dropping them.
    split_part(s.entity_id, '.', 1) AS domain,
    e.platform,
    e.device_class,
    e.unit_of_measurement,
    e.labels,
    e.area_id,
    a.name                AS area_name,
    e.device_id,
    d.name                AS device_name,
    d.manufacturer,
    d.model,
    s.attributes
FROM states s
LEFT JOIN entities e ON {_SCD2_ERA_JOIN.format(
    alias="e", key="entity_id", src="s.entity_id", ts="s.last_updated")}
LEFT JOIN areas a ON {_SCD2_ERA_JOIN.format(
    alias="a", key="area_id", src="e.area_id", ts="s.last_updated")}
LEFT JOIN devices d ON {_SCD2_ERA_JOIN.format(
    alias="d", key="device_id", src="e.device_id", ts="s.last_updated")};
"""

# SCD2 close-and-insert SQL.
# Convention: separate constants per table (not a .format() template) to keep
# SQL strings explicit, grep-able, and safe from accidental table injection.

# Close (expire) the currently-open row.
#
# The close timestamp MUST be the replacement row's valid_from, so the two
# intervals abut exactly: [old_vf, new_vf) then [new_vf, infinity). Using a
# separately-read worker clock instead — which is what issue #17 found — makes
# the closed interval overrun its successor's start, producing one overlap per
# metadata change. "remove" has no replacement row to take a valid_from from, so
# it closes at the item's `enqueued_at` instead — still event time. A clock read
# at dequeue would close the version whenever the queue happened to drain, which
# overlaps any re-creation that arrived in between; see
# meta_worker._close_timestamp.
#
# `valid_from < %s` is an ordering guard: an item applied out of order can then
# never close a version that begins at or after it. Without it, a late-arriving
# event writes valid_to < valid_from, and an inverted range makes tstzrange()
# raise — which would break the exclusion constraint and every later insert.
#
# %s = valid_to timestamp, %s = id, %s = the same timestamp again (guard).
SCD2_CLOSE_ENTITY_SQL = """
UPDATE entities
SET valid_to = %s
WHERE entity_id = %s AND valid_to IS NULL AND valid_from < %s;
"""

SCD2_CLOSE_DEVICE_SQL = """
UPDATE devices
SET valid_to = %s
WHERE device_id = %s AND valid_to IS NULL AND valid_from < %s;
"""

SCD2_CLOSE_AREA_SQL = """
UPDATE areas
SET valid_to = %s
WHERE area_id = %s AND valid_to IS NULL AND valid_from < %s;
"""

SCD2_CLOSE_LABEL_SQL = """
UPDATE labels
SET valid_to = %s
WHERE label_id = %s AND valid_to IS NULL AND valid_from < %s;
"""

# Idempotent guarded inserts — the ONLY insert path for all four dimensions.
#
# WHERE NOT EXISTS(open row) serves two purposes. On the "create" path it means
# re-running the startup snapshot does not duplicate rows for entities already
# present. On the "update" paths it makes the close+insert pair replay-safe:
# task_done() runs only after the write (meta_worker), so a crash between commit
# and task_done replays the item. On replay the close matches nothing (its
# valid_from < guard excludes the row just inserted) and this insert sees that
# open row and does nothing. Issue #17: the rename path previously used an
# unguarded INSERT, so a replay added a second open row.
# %s=entity_id, %s=ha_entity_uuid, %s=name, %s=domain, %s=platform,
# %s=device_id, %s=area_id, %s=labels, %s=device_class,
# %s=unit_of_measurement, %s=disabled_by, %s=valid_from, %s=extra
# NOTE: first positional param (%s for entity_id) appears TWICE — once in SELECT, once in WHERE NOT EXISTS subquery.
SCD2_SNAPSHOT_ENTITY_SQL = """
INSERT INTO entities
    (entity_id, ha_entity_uuid, name, domain, platform, device_id, area_id,
     labels, device_class, unit_of_measurement, disabled_by, valid_from, valid_to, extra)
SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s
WHERE NOT EXISTS (
    SELECT 1 FROM entities WHERE entity_id = %s AND valid_to IS NULL
);
"""

# %s=device_id, %s=name, %s=manufacturer, %s=model,
# %s=area_id, %s=labels, %s=valid_from, %s=extra
# NOTE: first positional param (%s for device_id) appears TWICE — once in SELECT, once in WHERE NOT EXISTS subquery.
SCD2_SNAPSHOT_DEVICE_SQL = """
INSERT INTO devices
    (device_id, name, manufacturer, model, area_id, labels, valid_from, valid_to, extra)
SELECT %s, %s, %s, %s, %s, %s, %s, NULL, %s
WHERE NOT EXISTS (
    SELECT 1 FROM devices WHERE device_id = %s AND valid_to IS NULL
);
"""

# %s=area_id, %s=name, %s=valid_from, %s=extra
# NOTE: first positional param (%s for area_id) appears TWICE — once in SELECT, once in WHERE NOT EXISTS subquery.
SCD2_SNAPSHOT_AREA_SQL = """
INSERT INTO areas
    (area_id, name, valid_from, valid_to, extra)
SELECT %s, %s, %s, NULL, %s
WHERE NOT EXISTS (
    SELECT 1 FROM areas WHERE area_id = %s AND valid_to IS NULL
);
"""

# %s=label_id, %s=name, %s=color, %s=valid_from, %s=extra
# NOTE: first positional param (%s for label_id) appears TWICE — once in SELECT, once in WHERE NOT EXISTS subquery.
SCD2_SNAPSHOT_LABEL_SQL = """
INSERT INTO labels
    (label_id, name, color, valid_from, valid_to, extra)
SELECT %s, %s, %s, %s, NULL, %s
WHERE NOT EXISTS (
    SELECT 1 FROM labels WHERE label_id = %s AND valid_to IS NULL
);
"""

# The former SCD2_INSERT_* constants (unguarded VALUES inserts) are deliberately
# gone. They were the new-row step of the close-and-insert cycle and had no
# replay protection; the SCD2_SNAPSHOT_* statements above now serve every insert.

# Replay probe. A guarded close+insert that writes nothing has two possible
# causes, and they mean opposite things: the item was already applied and is
# being replayed (nothing lost), or a newer version landed first and this change
# is now unrepresentable (a real loss). An open row whose valid_from equals the
# incoming one IS the row this item would have inserted, which distinguishes
# them. Without the probe every replay — routine after any shutdown that lands
# mid-item — was counted and logged as a lost registry change.
# Keyed by the item's `registry` field, not by table name.
# %s = id, %s = the incoming valid_from.
SCD2_OPEN_VERSION_AT_SQL = {
    "entity": "SELECT 1 FROM entities WHERE entity_id = %s AND valid_to IS NULL AND valid_from = %s;",
    "device": "SELECT 1 FROM devices WHERE device_id = %s AND valid_to IS NULL AND valid_from = %s;",
    "area": "SELECT 1 FROM areas WHERE area_id = %s AND valid_to IS NULL AND valid_from = %s;",
    "label": "SELECT 1 FROM labels WHERE label_id = %s AND valid_to IS NULL AND valid_from = %s;",
}

# Is there an open version at all, whatever it starts at? Tells a "remove" whose
# close matched nothing which case it is in: no open row means the removal was
# already applied (a replay), an open row means the ordering guard refused to
# close it and the removal was lost.
SCD2_OPEN_VERSION_SQL = {
    "entity": "SELECT valid_from FROM entities WHERE entity_id = %s AND valid_to IS NULL;",
    "device": "SELECT valid_from FROM devices WHERE device_id = %s AND valid_to IS NULL;",
    "area": "SELECT valid_from FROM areas WHERE area_id = %s AND valid_to IS NULL;",
    "label": "SELECT valid_from FROM labels WHERE label_id = %s AND valid_to IS NULL;",
}

# D-08-d step 4: watermark read (orchestrator → states worker connection).
SELECT_WATERMARK_SQL = f"SELECT MAX(last_updated) FROM {TABLE_NAME}"

# D-08-f: all-known entities reader — entities (all rows, incl. removed)
# unioned with all entity_ids ever written to states.
# GROUP BY on the states branch forces the planner to use the
# idx_states_entity_time index before the UNION deduplication step.
# Plain UNION without pre-grouping causes a full hypertable scan (~27 s).
SELECT_ALL_KNOWN_ENTITIES_SQL = (
    "SELECT entity_id FROM entities"
    " UNION"
    f" SELECT DISTINCT entity_id FROM {TABLE_NAME}"
)

# Change-detection SELECT constants — read the current open row for each registry type.
# Moved from inline strings in syncer.py per project convention (all SQL in const.py).
#
# ORDER BY valid_from DESC LIMIT 1 is required, not cosmetic. These are consumed
# with fetchone(); on a table that already holds several open rows for one id, an
# unordered read picks an arbitrary one, so the gate compares against a random
# predecessor and the damage can never heal. Pinning it to the newest open row
# makes the comparison deterministic and lets a corrupt table converge (issue #17).
#
# %s = the registry ID (entity_id / device_id / area_id / label_id).
SELECT_ENTITY_CURRENT_SQL = (
    "SELECT name, platform, device_id, area_id, labels, device_class,"
    " unit_of_measurement, disabled_by, extra"
    " FROM entities WHERE entity_id = %s AND valid_to IS NULL"
    " ORDER BY valid_from DESC LIMIT 1"
)

SELECT_DEVICE_CURRENT_SQL = (
    "SELECT name, manufacturer, model, area_id, labels, extra"
    " FROM devices WHERE device_id = %s AND valid_to IS NULL"
    " ORDER BY valid_from DESC LIMIT 1"
)

SELECT_AREA_CURRENT_SQL = (
    "SELECT name, extra FROM areas WHERE area_id = %s AND valid_to IS NULL"
    " ORDER BY valid_from DESC LIMIT 1"
)

SELECT_LABEL_CURRENT_SQL = (
    "SELECT name, color, extra FROM labels WHERE label_id = %s AND valid_to IS NULL"
    " ORDER BY valid_from DESC LIMIT 1"
)


# ----------------------------------------------------------------------------
# SCD2 invariant enforcement (issue #17)
# ----------------------------------------------------------------------------
#
# The invariant: for a given id, version intervals never overlap, and at most one
# version is open. Nothing enforced this before, which is why the corruption ran
# silently for months — row counts stayed plausible while every join on
# `valid_to IS NULL` doubled the fact rows for affected ids.
#
# An exclusion constraint is used rather than a partial unique index on open rows.
# Two open rows are both [valid_from, infinity) and therefore always overlap, so
# the exclusion constraint strictly subsumes the unique index, and it additionally
# catches overlaps between *closed* versions — which is the bulk of the observed
# damage (322 of 327 cases) and which a unique index cannot see.
#
# '[)' bounds are load-bearing: a version handing over to its successor produces
# [old_vf, new_vf) and [new_vf, infinity), which touch but do not overlap. With
# '[]' every legitimate handover would violate the constraint.
#
# COALESCE(valid_to, 'infinity') maps the open row into the range. 'infinity' is
# an immutable literal, so it is legal in an index expression (unlike 'now').
SCD2_BTREE_GIST_SQL = "CREATE EXTENSION IF NOT EXISTS btree_gist;"

# ADD CONSTRAINT takes ACCESS EXCLUSIVE, which queues behind any open reader and
# then blocks every later access to the table. At startup that reader is a
# dashboard query against states_flat, and the queue behind the ALTER is schema
# setup itself — so states ingestion and metadata writes would both stall for as
# long as the query runs. Failing fast is correct here: the constraint is
# best-effort and the next startup retries it.
# Plain SET, not SET LOCAL: schema setup runs in autocommit, where SET LOCAL has
# no transaction to be local to and would silently do nothing. Hence the reset.
SCD2_DDL_LOCK_TIMEOUT_SQL = "SET lock_timeout = '5s';"
SCD2_DDL_LOCK_TIMEOUT_RESET_SQL = "RESET lock_timeout;"

# ALTER TABLE ... ADD CONSTRAINT has no IF NOT EXISTS, so guard on pg_constraint
# to keep startup DDL idempotent. {table}/{key} are formatted from the hard-coded
# SCD2_DIMENSIONS tuple below, never from user input.
_SCD2_EXCLUDE_CONSTRAINT_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'excl_{table}_period'
          AND conrelid = '{table}'::regclass
    ) THEN
        ALTER TABLE {table} ADD CONSTRAINT excl_{table}_period
            EXCLUDE USING gist (
                {key} WITH =,
                tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz), '[)') WITH &&
            );
    END IF;
END $$;
"""

# Fallback when btree_gist cannot be installed (no CREATE privilege on the
# database). Catches only the multiple-open-rows half of the invariant; the
# verification queries remain the safety net for overlapping closed versions.
_SCD2_OPEN_UNIQUE_IDX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_{table}_open
    ON {table} ({key}) WHERE valid_to IS NULL;
"""

# (table, id column) for every SCD2 dimension. Single source of truth for schema
# setup, the repair script, and the verification queries.
SCD2_DIMENSIONS = (
    ("entities", "entity_id"),
    ("devices", "device_id"),
    ("areas", "area_id"),
    ("labels", "label_id"),
)

SCD2_EXCLUDE_CONSTRAINT_SQL = {
    table: _SCD2_EXCLUDE_CONSTRAINT_SQL.format(table=table, key=key)
    for table, key in SCD2_DIMENSIONS
}

SCD2_OPEN_UNIQUE_IDX_SQL = {
    table: _SCD2_OPEN_UNIQUE_IDX_SQL.format(table=table, key=key)
    for table, key in SCD2_DIMENSIONS
}


# ----------------------------------------------------------------------------
# SCD2 repair SQL (issue #17) — consumed by repair_scd2.py
# ----------------------------------------------------------------------------
#
# Anchor principle: TRUST valid_from, NEVER WRITE IT.
#
# valid_from is stamped in the event loop at event time and is the one field both
# defects leave intact — the close-timestamp bug corrupts only valid_to, and the
# enqueue-ordering bug corrupts only arrival order, not the timestamp already
# baked into the queued payload. So the repair reconstructs every interval from
# valid_from ordering alone and writes valid_to only. It never invents, shifts,
# or nudges a timestamp, and by default it never deletes a row.
#
# Why that converges, which matters because this runs once against real history:
# after the rebuild, row i's valid_to is <= row i+1's valid_from (it is either
# already earlier, or it is set to exactly that value), so consecutive ranges
# cannot overlap. Rows sharing a valid_from collapse to empty ranges, and an
# empty range overlaps nothing. The final row per id is never touched, so an open
# version stays open and a version closed by a "remove" keeps its recorded close
# time. Inverted rows are then clamped to empty, which is required because
# tstzrange() raises on an inverted range and one such row would make the
# exclusion constraint uncreatable.

# Rebuild plan. The window tiebreak is load-bearing: within one valid_from,
# closed rows sort before the open one ((valid_to IS NULL) is FALSE < TRUE), so
# the open row is last and inherits the real successor interval while its twins
# collapse. ctid makes the order total. ctid is a safe row identity here because
# plan and UPDATE are one statement over one snapshot under a table lock.
_SCD2_REPAIR_PLAN_SQL = """
    SELECT ctid AS rid,
           lead(valid_from) OVER (
               PARTITION BY {key}
               ORDER BY valid_from, (valid_to IS NULL), valid_to, ctid
           ) AS next_from
    FROM {table}
"""

# Shrink-only: `valid_to > next_from` never *extends* a close time. Strict
# equality would erase genuine remove-then-recreate gaps, where a version was
# legitimately closed long before the entity reappeared.
_SCD2_REPAIR_REBUILD_SQL = """
WITH plan AS (
""" + _SCD2_REPAIR_PLAN_SQL + """
)
UPDATE {table} t
   SET valid_to = p.next_from
  FROM plan p
 WHERE t.ctid = p.rid
   AND p.next_from IS NOT NULL
   AND (t.valid_to IS NULL OR t.valid_to > p.next_from);
"""

# Dry-run counterpart: how many rows the rebuild would touch, changing nothing.
_SCD2_REPAIR_REBUILD_PREVIEW_SQL = """
WITH plan AS (
""" + _SCD2_REPAIR_PLAN_SQL + """
)
SELECT count(*)
  FROM plan p
  JOIN {table} t ON t.ctid = p.rid
 WHERE p.next_from IS NOT NULL
   AND (t.valid_to IS NULL OR t.valid_to > p.next_from);
"""

# Clamp inverted intervals to empty. Applies to every row, not just the last:
# a non-final row whose valid_to already precedes its valid_from is left alone by
# the shrink-only rebuild and would still poison the constraint.
_SCD2_REPAIR_CLAMP_SQL = """
UPDATE {table}
   SET valid_to = valid_from
 WHERE valid_to IS NOT NULL AND valid_to < valid_from;
"""

_SCD2_REPAIR_CLAMP_PREVIEW_SQL = """
SELECT count(*) FROM {table}
 WHERE valid_to IS NOT NULL AND valid_to < valid_from;
"""

# Full row snapshots of everything the clamp will rewrite — the recorded close
# time is discarded, so it is captured before the fact rather than lost.
_SCD2_REPAIR_CLAMP_ROWS_SQL = """
SELECT to_jsonb(t) AS row_data FROM {table} t
 WHERE t.valid_to IS NOT NULL AND t.valid_to < t.valid_from;
"""

# Backup of the whole table before any mutation. {backup} is built in Python from
# a fixed prefix plus a UTC timestamp, never from user input.
SCD2_REPAIR_BACKUP_SQL = "CREATE TABLE {backup} AS SELECT * FROM {table};"

SCD2_REGCLASS_EXISTS_SQL = "SELECT to_regclass(%s) IS NOT NULL;"

# Table lock for the repair transaction. meta_worker writes on its own connection
# and a concurrent UPDATE would move a row's ctid out from under the plan.
# SHARE ROW EXCLUSIVE blocks writers but not readers.
SCD2_REPAIR_LOCK_SQL = "LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE;"
SCD2_REPAIR_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '10s';"

# ---- Ambiguity reporting -----------------------------------------------------
#
# Rows sharing (id, valid_from) with DIFFERENT payloads are genuinely ambiguous:
# that is the enqueue-reordering defect's signature, and nothing in the data says
# which version owned the era. The repair keeps every row and lets all but one
# collapse to an empty interval, so no payload is destroyed — but the collapsed
# versions label no state rows, so they are reported rather than passed over.
_SCD2_REPAIR_AMBIGUOUS_SQL = """
SELECT {key}::text AS id_value, valid_from, count(*) AS versions
  FROM {table}
 GROUP BY 1, 2
HAVING count(*) > 1
 ORDER BY 1, 2;
"""

# Twins at the same (id, valid_from) carrying the same payload — artefacts, not
# history. Reported always; deleted only under --collapse-duplicates.
#
# "Same payload" excludes valid_to, so twins that differ only in their recorded
# close time qualify. That is deliberate: the close-timestamp defect is what
# produced the differing valid_to in the first place, so demanding equality
# there would make the flag inert against the exact damage it exists for. It
# costs nothing, because the rebuild collapses every one of these rows to an
# empty interval anyway — within a valid_from, closed rows sort before the open
# one and inherit next_from, which equals their own valid_from. So the rows this
# deletes could never label a state row, and their close times survive in both
# the quarantine and the pre-repair backup.
_SCD2_REPAIR_IDENTICAL_DUPES_SQL = """
WITH grouped AS (
    SELECT {key}::text AS id_value, valid_from,
           count(*) AS versions,
           count(DISTINCT (to_jsonb(t) - 'valid_from' - 'valid_to')::text) AS payloads
      FROM {table} t
     GROUP BY 1, 2
)
SELECT id_value, valid_from, versions FROM grouped
 WHERE versions > 1 AND payloads = 1
 ORDER BY 1, 2;
"""

# Opt-in duplicate collapse. Keeps the row the states_flat view would pick
# (DISTINCT ON ... ORDER BY valid_to DESC NULLS FIRST -> the open twin), archives
# the losers with their full payload, and deletes only rows sharing the
# survivor's id, valid_from and payload (valid_to excluded — see above).
# "All payloads in this group are equal" is expressed as min = max over the
# partition, not count(DISTINCT ...) OVER (...): PostgreSQL rejects DISTINCT in a
# window function ("DISTINCT is not implemented for window functions").
_SCD2_REPAIR_COLLAPSE_SQL = """
WITH ranked AS (
    SELECT t.ctid AS rid, t.{key}::text AS id_value, t.valid_from,
           to_jsonb(t) AS row_data,
           row_number() OVER w AS rn,
           count(*) OVER w AS versions,
           min((to_jsonb(t) - 'valid_from' - 'valid_to')::text) OVER w
             = max((to_jsonb(t) - 'valid_from' - 'valid_to')::text) OVER w
             AS payload_uniform
      FROM {table} t
    WINDOW w AS (PARTITION BY t.{key}, t.valid_from
                 ORDER BY t.valid_to DESC NULLS FIRST, t.ctid
                 ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
),
losers AS (
    SELECT * FROM ranked WHERE versions > 1 AND payload_uniform AND rn > 1
),
archived AS (
    INSERT INTO scd2_repair_quarantine (run_id, table_name, id_value, reason, row_data)
    SELECT %s, '{table}', id_value, 'duplicate_same_start_same_payload', row_data FROM losers
)
DELETE FROM {table} d USING losers l WHERE d.ctid = l.rid;
"""

# Append-only audit of every row the repair deleted, rewrote, or flagged as
# ambiguous. row_data is the complete original row, so a human can adjudicate or
# re-insert without reaching for the backup table. Never pruned: run_id
# distinguishes re-runs, so repeated runs accumulate rather than overwrite, and
# "surface ambiguity rather than discard it" holds across runs too.
SCD2_QUARANTINE_DDL_SQL = """
CREATE TABLE IF NOT EXISTS scd2_repair_quarantine (
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id      TEXT        NOT NULL,
    table_name  TEXT        NOT NULL,
    id_value    TEXT        NOT NULL,
    reason      TEXT        NOT NULL,
    row_data    JSONB       NOT NULL
);
"""

SCD2_QUARANTINE_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_scd2_repair_quarantine
    ON scd2_repair_quarantine (table_name, id_value);
"""

SCD2_QUARANTINE_INSERT_SQL = """
INSERT INTO scd2_repair_quarantine (run_id, table_name, id_value, reason, row_data)
VALUES (%s, %s, %s, %s, %s);
"""

SCD2_QUARANTINE_SUMMARY_SQL = """
SELECT table_name, reason, count(*) AS rows
  FROM scd2_repair_quarantine WHERE run_id = %s
 GROUP BY 1, 2 ORDER BY 1, 2;
"""

# ---- Dead letter (issue #17, #20) --------------------------------------------
#
# Where the meta worker puts an item it is about to stop retrying. Two kinds
# reach it: a write the SCD2 constraints refuse (retrying replays the same
# conflicting row forever, so the queue would wedge) and a registry change that
# arrived after a newer version already landed (splicing it in blind is how the
# intervals got corrupted in the first place).
#
# Both used to exist only as an in-memory counter and a log line, which meant a
# restart erased the evidence that anything had been dropped at all. A table
# survives restarts, sits next to the data it failed to become, and is where
# issue #20 already proposed the counters should live. It is created by
# sync_setup_schema so the worker can always assume it exists.
#
# `item` is the complete queue item, so a human can fix the cause and replay it
# by hand. Never pruned automatically — this is evidence, not cache.
METADATA_DEADLETTER_DDL_SQL = """
CREATE TABLE IF NOT EXISTS metadata_deadletter (
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason      TEXT        NOT NULL,
    registry    TEXT,
    registry_id TEXT,
    error       TEXT,
    item        JSONB       NOT NULL
);
"""

METADATA_DEADLETTER_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_metadata_deadletter_detected_at
    ON metadata_deadletter (detected_at DESC);
"""

METADATA_DEADLETTER_INSERT_SQL = """
INSERT INTO metadata_deadletter (reason, registry, registry_id, error, item)
VALUES (%s, %s, %s, %s, %s);
"""

METADATA_DEADLETTER_SUMMARY_SQL = """
SELECT reason, count(*) AS rows, max(detected_at) AS latest
  FROM metadata_deadletter GROUP BY 1 ORDER BY 2 DESC;
"""

# Reasons, so the writer and the reader cannot drift apart.
DEADLETTER_REASON_INTEGRITY = "scd2_constraint_rejected"
DEADLETTER_REASON_OUT_OF_ORDER = "arrived_after_newer_version"

# Undo helper for a quarantined row, documented in the repair script's --help.
_SCD2_QUARANTINE_RESTORE_SQL = """
INSERT INTO {table}
SELECT (jsonb_populate_record(NULL::{table}, row_data)).*
  FROM scd2_repair_quarantine
 WHERE table_name = '{table}' AND id_value = %s;
"""

# ---- Verification ------------------------------------------------------------
#
# All checks must return zero rows for the invariant to hold. INVERTED runs
# first and RANGE is skipped whenever it found anything: tstzrange() raises on an
# inverted range rather than returning a row, so on the very damage this repair
# exists to fix, running RANGE aborts the run instead of reporting. Ordering
# alone is not enough — the dependency is declared in SCD2_VERIFY_GUARDS below
# and enforced by the runner.

_SCD2_VERIFY_INVERTED_SQL = """
SELECT {key}::text AS id_value, valid_from, valid_to
  FROM {table} WHERE valid_to IS NOT NULL AND valid_to < valid_from;
"""

# The assumption the whole repair rests on. A hit here means stop and
# re-diagnose rather than repair.
_SCD2_VERIFY_VALID_FROM_SQL = """
SELECT {key}::text AS id_value, valid_from
  FROM {table}
 WHERE valid_from IS NULL OR valid_from > now() + interval '1 day';
"""

_SCD2_VERIFY_MULTI_OPEN_SQL = """
SELECT {key}::text AS id_value, count(*) AS open_rows
  FROM {table} WHERE valid_to IS NULL
 GROUP BY 1 HAVING count(*) > 1;
"""

# Cheap window gate: catches an interval overrunning its successor and any open
# row that is not the newest version.
#
# The window ORDER BY must match the rebuild's tiebreak exactly. Ordering by
# valid_from alone leaves ties broken arbitrarily, so when several versions share
# a valid_from the open one can sort first and get flagged for having a
# successor — even though the rows it "precedes" are empty ranges that overlap
# nothing. That is a different question from the one the constraint asks, and it
# made this check disagree with _SCD2_VERIFY_RANGE_SQL on correctly repaired data.
_SCD2_VERIFY_SEQUENCE_SQL = """
SELECT id_value, valid_from, valid_to, next_from FROM (
    SELECT {key}::text AS id_value, valid_from, valid_to,
           lead(valid_from) OVER (
               PARTITION BY {key}
               ORDER BY valid_from, (valid_to IS NULL), valid_to, ctid
           ) AS next_from
      FROM {table}
) s
WHERE next_from IS NOT NULL AND (valid_to IS NULL OR valid_to > next_from);
"""

# Ground truth, derived differently from SEQUENCE on purpose: this is literally
# what the exclusion constraint enforces. The redundancy is the point — if the
# two ever disagree, the reconstruction is wrong.
_SCD2_VERIFY_RANGE_SQL = """
SELECT a.{key}::text AS id_value,
       a.valid_from AS a_from, a.valid_to AS a_to,
       b.valid_from AS b_from, b.valid_to AS b_to
  FROM {table} a
  JOIN {table} b ON a.{key} = b.{key} AND a.ctid < b.ctid
   AND tstzrange(a.valid_from, COALESCE(a.valid_to, 'infinity'::timestamptz), '[)')
    && tstzrange(b.valid_from, COALESCE(b.valid_to, 'infinity'::timestamptz), '[)');
"""

# Informational only: entities that produced states but have no dimension row at
# all. Overwhelmingly these are not missing history — HA keeps plenty of entities
# in its state machine without an entity-registry entry (sun.sun, zone.home,
# conversation.*, YAML automations and helpers), and those can never have a
# dimension row. The repair has no metadata to invent for them and must not try.
# Never drives a mutation.
SCD2_STATES_WITHOUT_DIM_SQL = f"""
SELECT s.entity_id, c.n AS states, c.last_seen
  FROM (SELECT DISTINCT entity_id FROM {TABLE_NAME}
        EXCEPT
        SELECT DISTINCT entity_id FROM entities) s
  CROSS JOIN LATERAL (
      SELECT count(*) AS n, max(last_updated) AS last_seen
        FROM {TABLE_NAME} st WHERE st.entity_id = s.entity_id) c
 ORDER BY c.n DESC;
"""


# States whose entity HAS a dimension row, but none covering the moment the
# state was recorded. Complements SCD2_STATES_WITHOUT_DIM_SQL, which finds
# entities with no row at all, and SCD2_SUSPICIOUS_GAPS_SQL, which only looks
# between two versions. This one also catches the two edges that neither sees:
# states before an entity's first valid_from and states after its last close.
# Both come back from states_flat with NULL metadata, so both need counting —
# an unreported NULL is the same silence issue #17 was made of.
#
# range_agg unions each entity's eras into one multirange and `@>` asks whether
# the state falls in any of them, which is the same question states_flat's join
# asks and cheaper than a correlated subquery per row.
#
# greatest(...) applies the clamp's semantics inline: tstzrange() raises on an
# inverted interval, so without it this would abort on exactly the damage the
# repair exists to fix. A clamped interval is empty and covers nothing, which is
# the honest answer for a row whose recorded era is impossible.
SCD2_STATES_UNCOVERED_SQL = f"""
WITH covered AS (
    SELECT entity_id,
           range_agg(tstzrange(
               valid_from,
               greatest(COALESCE(valid_to, 'infinity'::timestamptz), valid_from),
               '[)')) AS eras
      FROM entities GROUP BY entity_id)
SELECT s.entity_id, count(*) AS states, min(s.last_updated) AS first_uncovered,
       max(s.last_updated) AS last_uncovered
  FROM {TABLE_NAME} s
  JOIN covered c ON c.entity_id = s.entity_id
 WHERE NOT (c.eras @> s.last_updated)
 GROUP BY 1
 ORDER BY 2 DESC;
"""

# Entity versions pointing at an area or device that has no version covering the
# entity version's own era. Those produce NULL area_name / device_name in
# states_flat for every state in that era. Dimension-only — no hypertable scan —
# because the entity version already says which period is affected.
_SCD2_DIM_UNCOVERED_REFS_SQL = """
SELECT e.{key}::text AS ref_id, count(*) AS entity_versions
  FROM entities e
 WHERE e.{key} IS NOT NULL
   AND NOT EXISTS (
       SELECT 1 FROM {table} t
        WHERE t.{key} = e.{key}
          AND t.valid_from <= e.valid_from
          AND greatest(COALESCE(t.valid_to, 'infinity'::timestamptz), t.valid_from)
              > e.valid_from)
 GROUP BY 1
 ORDER BY 2 DESC;
"""

SCD2_DIM_UNCOVERED_REFS_SQL = {
    "areas": _SCD2_DIM_UNCOVERED_REFS_SQL.format(table="areas", key="area_id"),
    "devices": _SCD2_DIM_UNCOVERED_REFS_SQL.format(table="devices", key="device_id"),
}


def _per_dimension(template: str) -> dict[str, str]:
    """Bind a {table}/{key} template to every SCD2 dimension."""
    return {
        table: template.format(table=table, key=key)
        for table, key in SCD2_DIMENSIONS
    }


SCD2_REPAIR_REBUILD_SQL = _per_dimension(_SCD2_REPAIR_REBUILD_SQL)
SCD2_REPAIR_REBUILD_PREVIEW_SQL = _per_dimension(_SCD2_REPAIR_REBUILD_PREVIEW_SQL)
SCD2_REPAIR_CLAMP_SQL = _per_dimension(_SCD2_REPAIR_CLAMP_SQL)
SCD2_REPAIR_CLAMP_PREVIEW_SQL = _per_dimension(_SCD2_REPAIR_CLAMP_PREVIEW_SQL)
SCD2_REPAIR_CLAMP_ROWS_SQL = _per_dimension(_SCD2_REPAIR_CLAMP_ROWS_SQL)
SCD2_REPAIR_AMBIGUOUS_SQL = _per_dimension(_SCD2_REPAIR_AMBIGUOUS_SQL)
SCD2_REPAIR_IDENTICAL_DUPES_SQL = _per_dimension(_SCD2_REPAIR_IDENTICAL_DUPES_SQL)
SCD2_REPAIR_COLLAPSE_SQL = _per_dimension(_SCD2_REPAIR_COLLAPSE_SQL)
SCD2_QUARANTINE_RESTORE_SQL = _per_dimension(_SCD2_QUARANTINE_RESTORE_SQL)

# Verification checks in mandatory execution order — INVERTED first (see above).
SCD2_VERIFY_CHECKS = (
    ("inverted", _per_dimension(_SCD2_VERIFY_INVERTED_SQL)),
    ("valid_from_sanity", _per_dimension(_SCD2_VERIFY_VALID_FROM_SQL)),
    ("multi_open", _per_dimension(_SCD2_VERIFY_MULTI_OPEN_SQL)),
    ("sequence", _per_dimension(_SCD2_VERIFY_SEQUENCE_SQL)),
    ("range_overlap", _per_dimension(_SCD2_VERIFY_RANGE_SQL)),
)

# {check: the check that must return zero before it can run}. A guarded check
# whose guard found something is not merely noisy — its SQL raises on that input.
SCD2_VERIFY_GUARDS = {"range_overlap": "inverted"}

# Recorded in place of a row count for a check that was not run because its guard
# failed. Negative so it can never be mistaken for "clean" by a truthiness or
# any() test, and so the display can name it.
SCD2_CHECK_SKIPPED = -1


# ---- Fidelity reporting (issue #17) ------------------------------------------
#
# The repair normalises intervals. It cannot tell whether the resulting history
# is TRUE — and two classes of doubt are measurable, so they get reported rather
# than passed over in silence.

# A gap — a version closed before its successor starts — is only a real absence
# if the entity really was gone. States recorded inside the gap prove it was not:
# something existed and was producing data while the dimension says nothing was.
# That is evidence the gap is an artefact, though not proof of any one cause.
# The repair preserves the gap regardless — inventing metadata continuity would
# be a worse lie than admitting the hole — but the operator gets to see it.
# Entities only: the other dimensions have no fact table to check against.
#
# greatest(valid_to, valid_from) applies the clamp's semantics here, so an
# inverted interval cannot report a gap starting before its own version does.
# The window tiebreak matches _SCD2_REPAIR_PLAN_SQL exactly; ordering by
# valid_from alone leaves ties arbitrary and makes the answer nondeterministic
# on rows sharing a timestamp.
# `valid_to IS NOT NULL` is required, not tidiness: GREATEST ignores NULLs in
# PostgreSQL, so an OPEN row would yield gap_start = valid_from and every open
# row that has a successor would be reported as a gap spanning its own era.
# Those are not gaps — the rebuild closes them at the successor's valid_from.
#
# The metadata either side of the gap is compared too, with modified_at stripped
# (HA rewrites it on every internal registry write, which is why the
# integration's own change detection ignores it) and ha_entity_uuid included
# (an entity_id reused by a new registry entry is a different entity, however
# alike the two look). Identical payloads mean the gap could be closed without
# asserting anything new; differing payloads mean the era genuinely cannot be
# attributed to either version. The answer drives --merge-identical-gaps, so it
# must ask exactly the question SCD2_MERGE_PLAN_SQL asks.
SCD2_SUSPICIOUS_GAPS_SQL = f"""
WITH ordered AS (
    SELECT entity_id, valid_from, valid_to,
           (ha_entity_uuid, name, domain, platform, device_id, area_id, labels,
            device_class, unit_of_measurement, disabled_by)::text AS payload,
           (extra - 'modified_at')::text AS extra_cmp,
           lead(valid_from) OVER w AS gap_end,
           lead((ha_entity_uuid, name, domain, platform, device_id, area_id, labels,
                 device_class, unit_of_measurement, disabled_by)::text) OVER w AS next_payload,
           lead((extra - 'modified_at')::text) OVER w AS next_extra
    FROM entities t
    WINDOW w AS (PARTITION BY entity_id
                 ORDER BY valid_from, (valid_to IS NULL), valid_to, ctid)),
gaps AS (
    SELECT entity_id, greatest(valid_to, valid_from) AS gap_start, gap_end,
           (payload IS NOT DISTINCT FROM next_payload
            AND extra_cmp IS NOT DISTINCT FROM next_extra) AS same_metadata
      FROM ordered
     WHERE valid_to IS NOT NULL AND gap_end IS NOT NULL
       AND greatest(valid_to, valid_from) < gap_end)
SELECT gaps.entity_id, gaps.gap_start, gaps.gap_end, s.n AS states_inside,
       gaps.same_metadata
  FROM gaps
  CROSS JOIN LATERAL (
      SELECT count(*) AS n FROM {TABLE_NAME} st
       WHERE st.entity_id = gaps.entity_id
         AND st.last_updated >= gaps.gap_start
         AND st.last_updated <  gaps.gap_end) s
 WHERE s.n > 0
 ORDER BY s.n DESC;
"""

# Ids whose newest version is closed while an older one is still open. The
# rebuild closes every non-final open row and never reopens the final one, so
# these end up with no current version at all — correct if the thing really was
# removed, wrong if it still exists in HA. The invariant checks and the
# constraint both pass either way, which is exactly why this needs saying.
#
# "Newest" must mean the row the rebuild treats as final, so this ordering is
# _SCD2_REPAIR_PLAN_SQL's reversed in every term. Ordering on valid_from alone
# picks an arbitrary row out of a tied group and can name the wrong id.
_SCD2_NO_CURRENT_VERSION_SQL = """
WITH ranked AS (
    SELECT {key}::text AS id_value, valid_from, valid_to,
           row_number() OVER (
               PARTITION BY {key}
               ORDER BY valid_from DESC, (valid_to IS NULL) DESC,
                        valid_to DESC, ctid DESC) AS rn,
           count(*) FILTER (WHERE valid_to IS NULL) OVER (PARTITION BY {key}) AS open_rows
      FROM {table})
SELECT id_value, valid_from AS newest_from, valid_to AS newest_to
  FROM ranked
 WHERE rn = 1 AND valid_to IS NOT NULL AND open_rows > 0
 ORDER BY 1;
"""

SCD2_NO_CURRENT_VERSION_SQL = _per_dimension(_SCD2_NO_CURRENT_VERSION_SQL)


# ---- Opt-in: merge identical versions separated by a contradicted gap --------
#
# Two consecutive versions of one entity, separated by a period no version
# covers, carrying identical payloads, with `states` showing the entity was
# still producing data inside that period. The gap is not a real absence, and
# the two rows describe the same unchanged entity, so they are one version split
# in half by a lost write.
#
# What the `states` probe proves is weaker than continuous recording: it says at
# least one state landed inside the gap, so the entity existed then. That is
# enough to rule out a real absence and is all the fact table can honestly say —
# an entity can be alive and silent for hours.
#
# Merging replaces them with a single row spanning both — the earlier row's
# valid_to is extended to the later row's valid_to, and the later row is deleted
# (archived first). That is a truer SCD2 record than closing the gap and leaving
# two adjacent identical versions, which would assert a metadata change that
# never happened.
#
# Off by default: this is the one operation that EXTENDS a recorded close time,
# which the rest of the repair never does. Entities only — the other dimensions
# have no fact table to corroborate a gap against.
#
# `payload` includes ha_entity_uuid: an entity_id freed by a deletion and taken
# by a new registry entry produces two versions that can be identical in every
# user-visible field while being different entities. Merging across that would
# delete the newer identity and assert a continuity that never existed.
#
# `payload` deliberately excludes modified_at, which HA rewrites on every
# internal registry write; the integration's own change detection ignores it for
# the same reason.
SCD2_MERGE_PLAN_SQL = """
CREATE TEMP TABLE scd2_merge_plan ON COMMIT DROP AS
WITH ordered AS (
    SELECT ctid AS rid, entity_id, valid_from, valid_to,
           (ha_entity_uuid, name, domain, platform, device_id, area_id, labels,
            device_class, unit_of_measurement, disabled_by)::text AS payload,
           (extra - 'modified_at')::text AS extra_cmp,
           lead(ctid)       OVER w AS next_rid,
           lead(valid_from) OVER w AS next_from,
           lead(valid_to)   OVER w AS next_to,
           lead((ha_entity_uuid, name, domain, platform, device_id, area_id, labels,
                 device_class, unit_of_measurement, disabled_by)::text) OVER w AS next_payload,
           lead((extra - 'modified_at')::text) OVER w AS next_extra
      FROM entities t
    WINDOW w AS (PARTITION BY entity_id
                 ORDER BY valid_from, (valid_to IS NULL), valid_to, ctid)),
mergeable AS (
    SELECT o.* FROM ordered o
     WHERE o.valid_to IS NOT NULL
       AND o.next_from IS NOT NULL
       AND o.valid_to < o.next_from
       AND o.payload   IS NOT DISTINCT FROM o.next_payload
       AND o.extra_cmp IS NOT DISTINCT FROM o.next_extra
       AND EXISTS (SELECT 1 FROM states st
                    WHERE st.entity_id = o.entity_id
                      AND st.last_updated >= o.valid_to
                      AND st.last_updated <  o.next_from))
-- One merge per row per pass: a row that is itself the right-hand side of
-- another mergeable pair is left for the next pass, so a chain A-B-C collapses
-- over successive passes instead of two statements fighting over the same row.
SELECT m.rid, m.next_rid, m.entity_id, m.next_to
  FROM mergeable m
 WHERE NOT EXISTS (SELECT 1 FROM mergeable m2 WHERE m2.next_rid = m.rid);
"""

# Every reference is schema-qualified for the same reason as the DROP below: a
# plan statement must never be able to resolve to a permanent table.
SCD2_MERGE_ARCHIVE_SQL = """
INSERT INTO scd2_repair_quarantine (run_id, table_name, id_value, reason, row_data)
SELECT %s, 'entities', p.entity_id, 'merged_into_previous_version', to_jsonb(b)
  FROM pg_temp.scd2_merge_plan p JOIN entities b ON b.ctid = p.next_rid;
"""

# Extends the surviving row to cover both eras. valid_from is untouched.
SCD2_MERGE_EXTEND_SQL = """
UPDATE entities a SET valid_to = p.next_to
  FROM pg_temp.scd2_merge_plan p WHERE a.ctid = p.rid;
"""

SCD2_MERGE_DELETE_SQL = """
DELETE FROM entities d USING pg_temp.scd2_merge_plan p WHERE d.ctid = p.next_rid;
"""

SCD2_MERGE_PLAN_COUNT_SQL = "SELECT count(*) FROM pg_temp.scd2_merge_plan;"

# pg_temp is not tidiness. On the first pass no temp table exists yet, so an
# unqualified DROP resolves through search_path and would take a PERMANENT table
# of that name with it — outside the backups, outside the quarantine, gone. The
# schema qualification makes the statement incapable of naming anything but this
# transaction's own temp table.
SCD2_MERGE_PLAN_DROP_SQL = "DROP TABLE IF EXISTS pg_temp.scd2_merge_plan;"
