# TimescaleDB Recorder for Home Assistant

A custom integration that writes Home Assistant entity state changes to a TimescaleDB hypertable for long-term analytics. Every state change on the HA event bus is buffered in memory and flushed in batches to PostgreSQL, giving you a fully queryable time-series record of your home with better performance and storage efficiency than the built-in recorder.

## Features

- Real-time state ingestion via the HA event bus (`state_changed`)
- Buffered batch writes (configurable size and flush interval)
- Entity filtering matching recorder semantics — include/exclude by domain, entity ID, or glob
- JSONB attributes column alongside the state value
- Automatic hypertable creation and compression policy setup on first start
- HACS compatible

## Prerequisites

- Home Assistant 2024.1 or later
- The built-in HA recorder integration must be enabled — the integration uses the recorder's Python API for automatic gap detection and backfill (any recorder backend works).
- The standalone [`backfill_gaps.py`](#backfilling-historical-gaps) (optional use) script requires the default SQLite backend.
- A running PostgreSQL + TimescaleDB instance > 2.18 (the [TimescaleDB HA app](https://github.com/flaksic/hass-timescaledb) is the intended companion)
- The DSN user must have `CREATE TABLE` privileges — use the `homeassistant` role created by the app, **not** `homeassistant_rw` which is read/write only and lacks DDL rights

## Installation

### HACS (recommended)

1. Open HACS in your Home Assistant UI
2. Go to **Integrations** and click the three-dot menu in the top-right corner
3. Select **Custom repositories**, paste `https://github.com/flaksit/ha-timescaledb-recorder`, choose category **Integration**, and click **Add**
4. Search for "TimescaleDB Recorder" and click **Install**
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/timescaledb_recorder/` directory into your HA config's `custom_components/` folder
2. Restart Home Assistant

## Configuration

### Initial setup

Go to **Settings > Devices & Services > Add Integration** and search for "TimescaleDB". The setup form asks for a single field:

| Field | Example | Notes |
|-------|---------|-------|
| DSN | `postgresql://homeassistant:secret@d00de1c4-timescaledb:5432/homeassistant` | Full connection string; get the password from the app logs |

The config flow validates the connection before creating the entry — if the credentials are wrong or the app is not running you will see a "Cannot connect" error.

### Options (post-install)

Click **Configure** next to the integration entry to adjust:

| Option | Default | Description |
|--------|---------|-------------|
| `batch_size` | 200 | Number of state rows buffered before a forced flush |
| `flush_interval` | 10 | Seconds between timer-triggered flushes |
| `chunk_interval_days` | 7 | Days per hypertable chunk (only affects new chunks) |
| `compress_after_days` | 7 | Age threshold before chunks are compressed |

Options take effect immediately — the integration reloads automatically when you save.

### Entity Filtering

Entity filtering is configured via `configuration.yaml` and mirrors [HA recorder semantics](https://www.home-assistant.io/integrations/recorder/#configure-filter). Omitting the filter block ingests all entities.

```yaml
timescaledb_recorder:
  include:
    domains:
      - sensor
      - binary_sensor
    entities:
      - switch.living_room_light
    entity_globs:
      - sensor.weather_*
  exclude:
    domains:
      - media_player
    entities:
      - sensor.debug_probe
    entity_globs:
      - sensor.*_internal
```

Filter behaviour:

- `include.domains` — only states from these domains are ingested
- `include.entities` — only these specific entity IDs are ingested
- `include.entity_globs` — only entities matching these glob patterns are ingested
- `exclude.domains`, `exclude.entities`, `exclude.entity_globs` — exclude matching entities even if they satisfy an include rule
- All keys are optional; omitting both `include` and `exclude` ingests all entities
- When only `include` is specified, non-matching entities are dropped (allow-list)
- When only `exclude` is specified, matching entities are dropped (deny-list)
- When both are specified, HA recorder precedence rules apply: include first, then exclude wins

After editing `configuration.yaml`, call the reload service to apply the new filter without restarting HA:  
**Settings → Developer tools → YAML → YAML configuration reloading → TimescaleDB Recorder**  
This re-parses `configuration.yaml` and reloads the integration's config entry. A full HA restart also works but is not required.

## Schema

The `states` table is created automatically on the first HA start after the integration is installed:

```sql
CREATE TABLE IF NOT EXISTS states (
    last_updated  TIMESTAMPTZ NOT NULL,
    last_changed  TIMESTAMPTZ NOT NULL,
    entity_id     TEXT        NOT NULL,
    state         TEXT,
    attributes    JSONB
);
```

**Hypertable partitioning** — chunks are 7 days wide (default), partitioned on `last_updated`.

**Compression** — segments by `entity_id`, ordered by `last_updated DESC`. Chunks older than `compress_after_days` (default 7, matching the chunk interval) are compressed automatically by the background policy.

**Index** — `idx_states_entity_time ON states (entity_id, last_updated DESC)` for fast per-entity time-series lookups.

### Convenience views

`state` is TEXT because HA states are untyped — the same column holds `"23.5"`, `"on"`, and `"unavailable"`. Two views are created alongside the tables so queries don't have to re-derive the numeric cast:

| View | Adds | Use for |
| ---- | ---- | ------- |
| `states_numeric` | `value NUMERIC` — the guarded cast of `state` | Numeric queries on a known entity |
| `states_flat` | `value` plus the registry metadata that was current *when each state was recorded*: `entity_name`, `domain`, `platform`, `device_class`, `unit_of_measurement`, `labels`, `area_id`, `area_name`, `device_id`, `device_name`, `manufacturer`, `model` | Exploring by name, area, or unit instead of raw entity IDs |

`value` is `NULL` wherever `state` is not a number, so non-numeric rows stay visible and aggregates (which ignore NULLs) still return the numeric answer. The cast guard accepts negatives — solar export and sub-zero temperatures are numeric states that a `^[0-9]` guard silently drops.

`states_flat` left-joins each dimension on its recorded interval `[valid_from, valid_to)` (open versions run to infinity). Every state row is kept; a row no version covers gets **NULL metadata**, except `domain`, which is derived from `entity_id` and always populated. NULL metadata means one of:

- **The entity is not in HA's entity registry** — `sun.sun`, `zone.home`, `conversation.*`, YAML-defined automations and helpers. Expected; query by `entity_id` and `domain`. The repair script's Coverage section lists them.
- **The registry history has a gap** for a registered entity. A real defect, reported by `repair_scd2.py --dry-run` under Fidelity.

On a database written before 2.4.0, run the [repair](#repairing-scd2-history) first: overlapping versions duplicate rows in this join.

The views matter most for SQL query builders such as Grafana's: a builder reads the column list and emits `AVG(state)`, which fails with `function avg(text) does not exist`. Pointing it at a view exposes `value` as a real numeric column, so aggregation, grouping, and filtering become point-and-click.

Consequences:

- An entity deleted from HA keeps its metadata for the period it existed. Matching on `valid_to IS NULL` instead would blank its entire history.
- Each era shows the name of its time. If a rename must not split a series, `GROUP BY entity_id` rather than `entity_name`.

The range join is not free. Measured over ~48 M rows: an entity-filtered 7-day query runs 99 ms (vs 57 ms for a current-row join), a broad 7-day aggregate 1.7 s (vs 442 ms). Use `states_numeric` when you don't need metadata.

Views are recreated on every integration startup, so they survive a database rebuild or an app reinstall. Read-only roles inherit `SELECT` through the owner's default privileges; no manual `GRANT` is needed.

## Querying

### Latest state for every entity

```sql
SELECT DISTINCT ON (entity_id)
    entity_id,
    state,
    last_updated
FROM states
ORDER BY entity_id, last_updated DESC;
```

### Time series for a single sensor

```sql
SELECT last_updated, value
FROM states_numeric
WHERE entity_id = 'sensor.living_room_temperature'
  AND last_updated > NOW() - INTERVAL '7 days'
ORDER BY last_updated;
```

Casting `state::numeric` directly against the base table instead would abort the whole query the first time the sensor reported `unavailable`.

### Hourly averages by area

```sql
SELECT time_bucket('1 hour', last_updated) AS bucket,
       area_name,
       AVG(value) AS avg_watts
FROM states_flat
WHERE unit_of_measurement = 'W'
  AND last_updated > NOW() - INTERVAL '7 days'
GROUP BY bucket, area_name
ORDER BY bucket;
```

### Check compression status

```sql
SELECT hypertable_name,
       chunk_name,
       is_compressed,
       compressed_total_size,
       uncompressed_total_size
FROM timescaledb_information.chunks
WHERE hypertable_name = 'states'
ORDER BY range_start DESC;
```

### Verify compression policy

```sql
SELECT *
FROM timescaledb_information.compression_settings
WHERE hypertable_name = 'states';
```

## Metadata Sync

In addition to state ingestion, the integration syncs HA registry metadata (entities, devices, areas, labels) to PostgreSQL dimension tables. These tables use **SCD2 (Slowly Changing Dimension Type 2)** temporal tracking, which means every historical version of a registry object is preserved with a time range indicating when it was current.

This enables historically correct joins: for any row in `states`, you can join to the metadata that was current at that exact moment — entity name, area, device, labels, device class, and more.

### Dimension tables

All four tables follow the same SCD2 pattern. The current row for any object has `valid_to IS NULL`. When metadata changes, the existing row is closed (`valid_to` set to the timestamp of the change) and a new row is inserted (`valid_from` = that same timestamp, `valid_to = NULL`). Historical rows are never deleted.

Every table also carries an `extra` JSONB column that stores the full registry object serialisation. This column is for forward-compatibility — when HA adds or renames internal fields across versions, the typed columns remain stable while the raw data is still accessible via `extra`.

#### entities

| Column | Type | Description |
|--------|------|-------------|
| `entity_id` | TEXT | HA entity ID (e.g. `sensor.living_room_temperature`) |
| `ha_entity_uuid` | TEXT | HA's internal stable UUID — survives entity_id renames |
| `name` | TEXT | Friendly name |
| `domain` | TEXT | Domain (e.g. `sensor`, `switch`) |
| `platform` | TEXT | Integration that provides the entity |
| `device_id` | TEXT | FK to `devices.device_id` |
| `area_id` | TEXT | FK to `areas.area_id` |
| `labels` | TEXT[] | Array of label IDs |
| `device_class` | TEXT | Device class (e.g. `temperature`, `power`) |
| `unit_of_measurement` | TEXT | Unit (e.g. `°C`, `W`) |
| `disabled_by` | TEXT | Non-null when entity is disabled |
| `valid_from` | TIMESTAMPTZ | When this version became current |
| `valid_to` | TIMESTAMPTZ | When this version was superseded (NULL = current) |
| `extra` | JSONB | Full registry entry serialisation |

#### devices

| Column | Type | Description |
|--------|------|-------------|
| `device_id` | TEXT | HA device ID |
| `name` | TEXT | Device name |
| `manufacturer` | TEXT | Manufacturer |
| `model` | TEXT | Model |
| `area_id` | TEXT | FK to `areas.area_id` |
| `labels` | TEXT[] | Array of label IDs |
| `valid_from` | TIMESTAMPTZ | When this version became current |
| `valid_to` | TIMESTAMPTZ | When this version was superseded (NULL = current) |
| `extra` | JSONB | Full registry entry serialisation |

#### areas

| Column | Type | Description |
|--------|------|-------------|
| `area_id` | TEXT | HA area ID |
| `name` | TEXT | Area name |
| `valid_from` | TIMESTAMPTZ | When this version became current |
| `valid_to` | TIMESTAMPTZ | When this version was superseded (NULL = current) |
| `extra` | JSONB | Full registry entry serialisation |

#### labels

| Column | Type | Description |
|--------|------|-------------|
| `label_id` | TEXT | HA label ID |
| `name` | TEXT | Label name |
| `color` | TEXT | Display color |
| `valid_from` | TIMESTAMPTZ | When this version became current |
| `valid_to` | TIMESTAMPTZ | When this version was superseded (NULL = current) |
| `extra` | JSONB | Full registry entry serialisation |

### How it works

On integration load, a full snapshot of all four registries is taken and inserted into the dimension tables. The snapshot uses idempotent `WHERE NOT EXISTS` inserts so restarting the integration never creates duplicate open rows.

After the snapshot, the integration subscribes to four HA registry events:

- `EVENT_ENTITY_REGISTRY_UPDATED`
- `EVENT_DEVICE_REGISTRY_UPDATED`
- `EVENT_AREA_REGISTRY_UPDATED`
- `EVENT_LABEL_REGISTRY_UPDATED`

Each event triggers the SCD2 close-and-insert cycle: the open row is closed and a new row inserted, both stamped with the time the event fired, so each interval ends exactly where its successor begins. Events are written in arrival order. Entity renames (entity_id changes) close the old entity_id row and open one under the new entity_id.

The dimension tables are created idempotently on every integration startup (same as `states`), so no manual schema migration is needed after updates.

### When a change cannot be written

A registry change is dropped instead of retried when the exclusion constraint rejects it, or when a newer version of the same id is already stored. The item goes to the **`metadata_deadletter`** table (reason, registry, id, full item as JSONB) and a `metadata_dropped` repair issue appears in Home Assistant. The issue does not clear itself. Inspect the table, fix the cause, and re-apply the change by editing the entity, device, area or label in HA:

```sql
SELECT detected_at, reason, registry_id, error FROM metadata_deadletter ORDER BY detected_at DESC;
```

The table is normally empty. Each row is a gap in the dimension history, shown as NULL metadata in `states_flat`.

Registry changes during startup, before the initial snapshot completes, are discarded. Creates and updates are picked up by the snapshot; a **removal** is lost and the row stays open ([#19](https://github.com/flaksit/ha-timescaledb-recorder/issues/19)).

### The invariant

For any id, version intervals never overlap, so at most one version is open (`valid_to IS NULL`). Each dimension enforces this with an exclusion constraint over `(id, tstzrange(valid_from, COALESCE(valid_to, 'infinity'), '[)'))`, installed on startup. It needs the `btree_gist` extension; without it the integration logs a warning and falls back to a unique index on open rows, which cannot detect overlaps between closed versions.

On a database written before 2.4.0 the constraints fail to apply (logged, not fatal) until the history is [repaired](#repairing-scd2-history).

### Example query: point-in-time metadata join

`states_flat` already does it:

```sql
SELECT last_updated, state, value, entity_name, area_name
FROM states_flat
WHERE entity_id = 'sensor.living_room_temperature'
ORDER BY last_updated DESC
LIMIT 10;
```

A hand-written inner join on the dimensions drops state rows no version covers (e.g. before the entity's first version); `states_flat` keeps them with NULL metadata. `valid_to IS NULL` alone gives current metadata, not point-in-time, and drops removed entities.

## Differences from the built-in recorder

### Runs alongside the recorder, not a replacement

This integration runs alongside the built-in recorder — it does not replace it. Both can have independent entity filter configurations.

## Troubleshooting

**Cannot connect to TimescaleDB**

- Verify the DSN in the integration options (Settings > Devices & Services > Configure)
- Check that the TimescaleDB app is running and shows "Started" in the HA Supervisor panel
- Confirm the user in the DSN is the `homeassistant` role (has DDL rights) — `homeassistant_rw` cannot create tables

**No data appearing in `states`**

- Open the HA log viewer and filter for `custom_components.timescaledb_recorder` — any connection errors appear here
- Check your entity filter: if you added an `include` block, only listed domains/entities are written
- The default `flush_interval` is 10 seconds — wait at least 10 seconds after a state change before querying

**Slow writes or high latency**

- Avoid querying a chunk that is currently being compressed. TimescaleDB holds a lock during compression; the background job runs periodically and briefly.
- Increase `batch_size` to reduce flush frequency under high event volume

## Backfilling historical gaps

If you installed the integration after HA had already been running for a while, or after a TimescaleDB outage, use the included backfill script to fill the gaps from HA's SQLite recorder. The script reads SQLite directly and requires the recorder's default SQLite backend — it is separate from the automatic gap backfill built into the integration.

From the HA host terminal (SSH addon):

```bash
docker exec homeassistant python3 /config/custom_components/timescaledb_recorder/backfill_gaps.py
```

No arguments needed. The script auto-detects the SQLite database path and reads the TimescaleDB DSN from the integration config. `psycopg[binary]` is already present in the HA container once the integration is installed.

Optional arguments:

| Argument | Default | Description |
|----------|---------|-------------|
| `--sqlite PATH` | `/config/home-assistant_v2.db` | SQLite recorder database path |
| `--pg-dsn DSN` | read from integration config | PostgreSQL connection string |
| `--start ISO8601` | earliest SQLite row | Start of backfill window (inclusive) |
| `--end ISO8601` | latest SQLite row | End of backfill window (inclusive, precision-snapped) |
| `--bucket-minutes M` | `60` | Time bucket width; larger = fewer PG queries, more memory |
| `--batch-size N` | `500` | Rows per INSERT batch |
| `--entities ENTITY_IDS` | all | Comma-separated entity_ids to backfill |
| `--dry-run` | off | Show what would be inserted without writing |

The script is safe to run while HA is active — SQLite is opened read-only and re-running is idempotent. Each bucket does a cheap `COUNT(*)` comparison first; buckets already in sync are skipped with no row fetches.

**`--end` precision snapping:** `--end 2026-04-10` includes the full day; `--end 2026-04-10T14:30` includes the full minute, and so on.

## Repairing SCD2 history

Versions before 2.4.0 could write overlapping dimension versions and several open versions per id. Any join on a dimension then duplicates state rows for affected ids, inflating sums and counts. 2.4.0 stops new damage; repair existing damage once:

```bash
# 1. install 2.4.0 via HACS and restart HA
# 2. inspect — mutates nothing
docker exec homeassistant python3 \
    /config/custom_components/timescaledb_recorder/repair_scd2.py --dry-run
# 3. back up, repair, verify, add constraints
docker exec homeassistant python3 \
    /config/custom_components/timescaledb_recorder/repair_scd2.py --apply
# 4. restart HA again
```

Order matters:

- **Install 2.4.0 before repairing.** Older versions retry rejected writes forever, so the new constraints would wedge their metadata queue. Without the constraints (`--no-constraints`) the history re-corrupts.
- **Keep steps 1–3 close together.** 2.4.0 redefines `states_flat` to join `valid_to` literally, so it duplicates rows until the repair runs.
- **Restart after `--apply`.** The startup snapshot re-creates a current version for ids the repair left without one (see Fidelity below).

**Downgrading below 2.4.0** after the constraints are in place: first `ALTER TABLE <t> DROP CONSTRAINT excl_<t>_period` for `entities`, `devices`, `areas` and `labels`, or the old writer wedges as above.

| Argument | Default | Description |
|----------|---------|-------------|
| `--dsn DSN` | read from integration config | PostgreSQL connection string |
| `--dry-run` | default | Report the damage and what would change; mutates nothing |
| `--apply` | off | Back up, repair, verify, and add the constraints |
| `--verify-only` | off | Check the invariant only; skips the Ambiguity, Fidelity and Coverage sections, which scan the whole `states` hypertable |
| `--collapse-duplicates` | off | Also delete rows sharing a surviving row's id, `valid_from` and payload |
| `--merge-identical-gaps` | off | Merge two identical versions separated by a gap that `states` contradicts into one row |
| `--no-constraints` | off | Repair without adding the exclusion constraints |
| `--yes` | off | Skip the `--apply` confirmation. Required without a terminal |

Exit codes: 0 invariant holds; 1 invariant violated, or a table still fails verification or its constraint could not be created; 2 `valid_from` itself looks unsound — stop and investigate. Ambiguity, Fidelity and Coverage findings do not change the exit code; read them. `--collapse-duplicates` and `--merge-identical-gaps` also work on an already-repaired database, so `--dry-run`, then `--apply --merge-identical-gaps` after reading Fidelity, is a valid sequence.

### What it does

Each version's `valid_to` is set to the next version's `valid_from`. Only `valid_to` is written, and only ever shrunk, so recorded gaps survive; the last version per id is untouched; nothing is deleted unless a flag says so. `valid_from` is trusted as the anchor, though the earliest writer stamped it at processing time, so it can lag the real change. History that was never written cannot be recovered, and the live HA registries are not consulted.

One exception writes `valid_from`: an entity whose first version starts before its device's first version, typically left by a history import that backdated entities but not devices, is moved later to the device's start — but never past the entity's first recorded state, and not if that would empty the version.

Each table is first copied to `<table>_prerepair_<utc timestamp>`, the only full record of the old values; drop those once satisfied. Deleted, clamped or start-aligned rows are also archived as JSONB in `scd2_repair_quarantine`, tagged with the run id.

### Undoing a repair

The backup table has no constraints and holds rows the constraint rejects, so drop the constraint first:

```sql
BEGIN;
ALTER TABLE entities DROP CONSTRAINT excl_entities_period;
TRUNCATE entities;
INSERT INTO entities SELECT * FROM entities_prerepair_<stamp>;
COMMIT;
```

Repeat per table. To restore one archived row: `INSERT INTO entities SELECT (jsonb_populate_record(NULL::entities, row_data)).* FROM scd2_repair_quarantine WHERE ...`.

### Read the report

Consistent intervals are not necessarily true ones. The script reports what it cannot decide:

- **Ambiguity** — two versions with the same id and `valid_from` but different contents. Both are kept; one gets an empty interval.
- **Fidelity: gaps `states` contradicts** — a version closed with no successor while the entity kept recording. Those states get NULL metadata in `states_flat`. If the versions either side are identical, `--merge-identical-gaps` merges them and restores that metadata; otherwise the gap stays.
- **Fidelity: ids with no current version** — the newest version was already closed while an older one was open, and the rebuild closes the older one too. Correct if the thing was removed. If it still exists, the restart after `--apply` re-creates its current version; until then it is missing from `valid_to IS NULL` queries.
- **Coverage** — states no version covers: entities never in the registry (expected), states before an entity's first version (normal where history predates the dimension), and states after its last close or pointing at an uncovered device or area (the dimension stopped describing something that kept recording). Entity versions starting before their device are listed here too; `--apply` aligns them as above.

A second `--apply` changes nothing.

### If it fails

Each table is repaired in one transaction under `SHARE ROW EXCLUSIVE` with a 10-second `lock_timeout`, backup included. HA can keep running; if the metadata worker holds the lock, the repair times out having changed nothing — run it again. Any interruption leaves the current table untouched and earlier tables repaired, each consistent on its own; re-running converges.
