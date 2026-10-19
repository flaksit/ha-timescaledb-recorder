# TimescaleDB Recorder for Home Assistant

A custom integration that writes Home Assistant entity state changes to a TimescaleDB hypertable for long-term analytics. Every state change on the HA event bus is buffered in memory and flushed in batches to PostgreSQL, giving you a fully queryable time-series record of your home with better performance and storage efficiency than the built-in recorder.

## Features

- Real-time state ingestion via the HA event bus (`state_changed`)
- Buffered batch writes (configurable size and flush interval)
- Entity filtering matching recorder semantics — include/exclude by domain, entity ID, or glob
- JSONB attributes column alongside the state value
- Automatic hypertable creation and compression policy setup on first start
- Optional per-minute time-weighted rollup of chosen numeric entities, for dashboards that integrate over months
- HACS compatible

## Prerequisites

- Home Assistant 2026.10.1 or later
- The built-in HA recorder integration — declared as a dependency, so HA loads it automatically. The integration uses the recorder's Python API for automatic gap detection and backfill (any recorder backend works).
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

### Per-minute rollup

Integrating power over months from raw samples is slow: a 1 Hz meter adds 2.6 M rows a month, and every dashboard load walks them all. List such entities under `minute_rollup` and the integration keeps their [time-weighted integral per minute](#per-minute-rollup-states_minute) in `states_minute`, filled by a TimescaleDB job inside the database.

```yaml
timescaledb_recorder:
  minute_rollup:
    - entity_id: sensor.electricity_meter_grid_power
      hold: 300
    - entity_id: sensor.water_heater_plug_power
      hold: 120
      zero_hold: "720:00:00"
```

| Key | Required | Meaning |
|-----|----------|---------|
| `entity_id` | yes | A numeric entity |
| `hold` | yes | How long a reading stays valid when no newer one arrives: seconds or `HH:MM:SS`. Longer silences count as no data. |
| `zero_hold` | no | `hold` for a reading of exactly 0. Report-on-change devices go quiet while idle at zero, so their 0 stays true far longer than a non-zero reading they refresh every few seconds. |

Adding an entity, or changing its holds, queues its whole history; the job works through it newest first, spending up to 20 s per run until done. Removing an entity deletes its rows. Apply changes with the reload service above.

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

### When a state row cannot be written

Connection loss, timeouts, deadlocks, server-internal errors (SQLSTATE class XX) and other transient database errors are retried until they succeed. Any other error is taken to mean the data itself is rejected (for example a NUL character in an attribute). Once the batch has failed at least 3 times over at least 60 seconds it is retried row by row. If rows of 5 different entities all fail, the database rather than the rows is blamed: nothing is dropped, the batch goes back to retrying (raising the stalled repair issue) and the row-by-row pass is tried again later. Otherwise each row that still fails, again after 3 attempts over 60 seconds, goes to the **`states_deadletter`** table (entity, timestamp, error, full row as JSON text) and raises a `states_dropped` repair issue, so ingestion carries on. The issue does not clear itself:

```sql
SELECT detected_at, entity_id, last_updated, error FROM states_deadletter ORDER BY detected_at DESC;
```

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

### Per-minute rollup (`states_minute`)

Created and maintained when [`minute_rollup`](#per-minute-rollup) lists entities:

| Column | Meaning |
|--------|---------|
| `entity_id`, `minute` | The entity and the start of a closed UTC minute |
| `integral` | ∫ value dt over the covered part of the minute (value × seconds) |
| `covered_seconds` | Seconds of the minute a reading held for (0 < covered ≤ 60) |

A reading holds until the next numeric reading, capped by `hold` (or `zero_hold` for a 0). Non-numeric states such as `unavailable` are skipped, not treated as an end. `integral / covered_seconds` is the mean of the covered part; `integral / 60` counts the uncovered rest as 0. Every minute with any coverage is stored, so choose a threshold on `covered_seconds` that suits the query.

It is a hypertable filled by a job, not a continuous aggregate: a minute's value depends on the last reading *before* it, which a continuous aggregate bucket cannot see.

The job runs every minute. It recomputes the last 15 minutes before each entity's frontier through the current minute (catching rows that arrived late), moves the frontier, then works through `states_minute_dirty` (history of newly added entities, and ranges the automatic backfill or `backfill_gaps.py` wrote behind the frontier). `states_minute_entities.filled_until` is the frontier: minutes before it are final. Compute minutes from there on from raw samples:

```sql
WITH f AS (SELECT filled_until FROM states_minute_entities
           WHERE entity_id = 'sensor.electricity_meter_grid_power')
SELECT minute, integral / 60 AS watts
FROM states_minute, f
WHERE entity_id = 'sensor.electricity_meter_grid_power'
  AND minute >= now() - interval '30 days' AND minute < f.filled_until
-- UNION ALL the live computation over [filled_until, now()) from states_numeric
ORDER BY minute;
```

Job health: `SELECT * FROM timescaledb_information.job_stats WHERE job_id = (SELECT job_id FROM timescaledb_information.jobs WHERE proc_name = 'states_minute_job');` and `timescaledb_information.job_errors`.

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

Every table also carries an `extra` JSONB column that stores the full registry object serialisation. This column is for forward-compatibility — when HA adds or renames internal fields across versions, the typed columns remain stable while the raw data is still accessible via `extra`. Set-valued fields (e.g. `identifiers`, `connections`, `aliases`) are stored as sorted JSON arrays, and `labels` arrays are sorted (code point order). Rows written by older versions hold sets as Python set strings such as `"{'a', 'b'}"`, in arbitrary order, and may hold unsorted `labels`. On its first start after the upgrade the metadata worker rewrites those rows once, before processing any registry change: only `extra` and `labels` change, never `valid_from`/`valid_to`, and the `schema_migrations` table records that it ran so later starts skip it.

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

Child devices (HA 2026.10+) are stored here too; their `manufacturer` and `model` are NULL and `extra` carries `parent_device_id`.

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

On integration load, the integration subscribes to four HA registry events:

- `EVENT_ENTITY_REGISTRY_UPDATED`
- `EVENT_DEVICE_REGISTRY_UPDATED`
- `EVENT_AREA_REGISTRY_UPDATED`
- `EVENT_LABEL_REGISTRY_UPDATED`

Each event triggers the SCD2 close-and-insert cycle: the open row is closed and a new row inserted, both stamped with the time the event fired, so each interval ends exactly where its successor begins. Events are written in arrival order. Entity renames (entity_id changes) close the old entity_id row and open one under the new entity_id. A change that leaves the stored metadata unchanged writes nothing.

Once HA has started, a snapshot of all four registries is queued behind the events received so far, stamped with the snapshot time. Each entry is reconciled against its open row: missing → inserted, equal → nothing, different → closed and replaced. So a restart repairs any drift, and a change made while HA was down is recorded at the restart time, not when it happened. Events are recorded from integration load on, so changes during startup are not lost.

An id's first version starts at the registry entry's `created_at` rather than the event or snapshot time, so the states HA writes before announcing a new entity are covered. This only applies when the id has no earlier version (for entities: neither the entity_id nor the registry uuid), never to a rename, and never to entries whose `created_at` is HA's 1970 placeholder for "unknown".

After the snapshot, each registry's open rows whose id the registry no longer holds are closed at the snapshot time, so an entry removed while HA was down ends there. As a safeguard nothing is closed when a registry lists no entries while its table has open rows (logged as a warning), and a sweep that would close more than half of a table's open rows (and more than 2) is refused and goes to `metadata_deadletter` with reason `sweep_refused`, raising the `metadata_dropped` repair issue. A refused sweep is retried, and refused again, on every restart until the rows are closed by hand or the registry lists them again.

The dimension tables are created idempotently on every integration startup (same as `states`), so no manual schema migration is needed after updates. Registry changes queue on disk until that setup has finished.

### When a change cannot be written

A registry change is dropped instead of retried when the exclusion constraint rejects it, when a newer version of the same id is already stored, or when it fails at least 3 times over at least 60 seconds with a non-transient error (anything other than connection loss, timeouts, deadlocks, server-internal errors and similar, which are retried until they succeed). There is no circuit breaker here as for state rows: the startup snapshot re-reads every registry, so after a restart the current version of every existing entry is written again and removed entries are closed; only intermediate versions and the exact time of removals are lost. The item goes to the **`metadata_deadletter`** table (reason, registry, id, full item as JSONB) and a `metadata_dropped` repair issue appears in Home Assistant. The issue does not clear itself. Inspect the table, fix the cause, and re-apply the change by editing the entity, device, area or label in HA:

```sql
SELECT detected_at, reason, registry_id, error FROM metadata_deadletter ORDER BY detected_at DESC;
```

The table is normally empty. Each row is a gap in the dimension history, shown as NULL metadata in `states_flat`.

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

## Monitoring

### Diagnostic entities

All on the "TimescaleDB Recorder" device:

| Entity | Shows |
|--------|-------|
| Health | `ok` / `degraded` / `error` rollup of the repair issues below and worker liveness |
| DB Status | `connected` / `disconnected` / `stalled` |
| Last Write | Timestamp of the last successful states insert, refreshed every 60 s. Alert on it: it keeps moving only while data is actually written |
| Queue Depth Peak, Events Dropped, Meta Queue Depth | Buffer pressure |
| States Worker, Metadata Worker | Worker thread alive |
| Buffer Overflow | Live buffer full and dropping (recovered by backfill) |

Example alert when nothing was written for 15 minutes:

```yaml
template:
  - trigger:
      - trigger: time_pattern
        minutes: "/5"
    binary_sensor:
      - name: TimescaleDB writes stale
        state: >
          {{ now() - states('sensor.timescaledb_recorder_last_write') | as_datetime(now()) > timedelta(minutes=15) }}
```

### Repair issues

| Issue | Severity | Raised when | Clears |
|-------|----------|-------------|--------|
| `states_worker_stalled`, `meta_worker_stalled` | error | 5 consecutive failed writes | on next success |
| `db_unreachable` | error | writes failing for 5 min | on next success |
| `ingestion_stalled` | error | a worker made no progress for 10 min without an error: a DB call still in flight, or the states worker waiting for backfill data | when the worker moves again |
| `buffer_dropping` | warning | live buffer full | when the catch-up backfill starts |
| `states_dropped`, `metadata_dropped` | error | data rejected and dead-lettered | never (inspect the dead-letter table) |
| `recorder_disabled` | warning | HA recorder disabled via `recorder.disable` | within a minute of it being enabled again |

A worker thread that dies is restarted by the watchdog with a persistent notification.

### Database timeouts

Every worker connection bounds its waits so a hang turns into a logged, retried error: TCP keepalives (dead connection detected after ~60 s idle), `tcp_user_timeout` 60 s, `connect_timeout` 10 s, and `statement_timeout` 5 min for reads and writes (schema setup at startup, the one-time canonicalization of dimension rows and the backfill's scan of all known entity ids run without it, since DDL on a large table and work that grows with history can take longer; the watchdog still reports either if it runs past 10 min). Values you set in the DSN win, e.g. `postgresql://...?keepalives_idle=120&options=-c%20statement_timeout%3D600000`. A `statement_timeout` configured on the role or database is kept too. These are session settings, so connect directly or through a pooler in session mode (PgBouncer `pool_mode = session`); transaction or statement pooling is not supported.

## Troubleshooting

**Cannot connect to TimescaleDB**

- Verify the DSN in the integration options (Settings > Devices & Services > Configure)
- Check that the TimescaleDB app is running and shows "Started" in the HA Supervisor panel
- Confirm the user in the DSN is the `homeassistant` role (has DDL rights) — `homeassistant_rw` cannot create tables

**No data appearing in `states`**

- Open the HA log viewer and filter for `custom_components.timescaledb_recorder` — any connection errors appear here
- Check your entity filter: if you added an `include` block, only listed domains/entities are written
- The default `flush_interval` is 10 seconds — wait at least 10 seconds after a state change before querying

**Writes stopped (`ingestion_stalled` repair issue, Last Write not advancing)**

- The issue and the log line `Ingestion stalled: ...` name the worker and the DB call or wait. A call should end within the timeouts above; one that does not points at the network path or a server-side wait the timeouts did not cover
- Look for the worker's session on the server: `SELECT pid, state, wait_event_type, wait_event, now() - query_start AS running, query FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() ORDER BY query_start;`
- `schema_setup` on a large table after an upgrade can legitimately take this long — wait for it
- Otherwise reload the integration. The gap is backfilled from HA's recorder as long as it still holds the data (`purge_keep_days`)

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

By default only entities the integration records are backfilled: the script reads the `include`/`exclude` filter from the `timescaledb_recorder` section of `configuration.yaml`, resolving `!include` and `!secret` the way HA does. `--entities` overrides the filter.

Optional arguments:

| Argument | Default | Description |
|----------|---------|-------------|
| `--sqlite PATH` | `/config/home-assistant_v2.db` | SQLite recorder database path |
| `--pg-dsn DSN` | read from integration config | PostgreSQL connection string |
| `--start ISO8601` | earliest SQLite row | Start of backfill window (inclusive) |
| `--end ISO8601` | latest SQLite row | End of backfill window (inclusive, precision-snapped) |
| `--bucket-minutes M` | `60` | Time bucket width; larger = fewer PG queries, more memory |
| `--batch-size N` | `500` | Rows per INSERT batch |
| `--entities ENTITY_IDS` | all recorded | Comma-separated entity_ids to backfill; overrides the filter |
| `--ha-config DIR` | `/config` | HA config directory holding `configuration.yaml` |
| `--dry-run` | off | Show what would be inserted without writing |

When it inserts rows, it also queues the affected minutes of the [per-minute rollup](#per-minute-rollup-states_minute) for recomputation.

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
- **`--dry-run` and `--apply` refuse (exit 2) until the integration has canonicalized the dimension rows**, which it does once on startup (step 1). They compare row payloads, and rows in the old set-string form would make identical versions look different. `--verify-only` compares intervals only and always runs.

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

Exit codes: 0 invariant holds; 1 invariant violated, or a table still fails verification or its constraint could not be created; 2 `valid_from` itself looks unsound — stop and investigate — or the dimension rows are not canonicalized yet (start the updated integration first). Ambiguity, Fidelity and Coverage findings do not change the exit code; read them. `--collapse-duplicates` and `--merge-identical-gaps` also work on an already-repaired database, so `--dry-run`, then `--apply --merge-identical-gaps` after reading Fidelity, is a valid sequence.

### What it does

Each version's `valid_to` is set to the next version's `valid_from`. Only `valid_to` is written, and only ever shrunk, so recorded gaps survive; the last version per id is untouched; nothing is deleted unless a flag says so. `valid_from` is trusted as the anchor, though the earliest writer stamped it at processing time, so it can lag the real change. History that was never written cannot be recovered, and the live HA registries are not consulted.

Two exceptions write `valid_from`, both for first versions only:

- An entity whose first version starts before its device's first version, typically left by a history import that backdated entities but not devices, is moved later to the device's start — but never past the entity's first recorded state, and not if that would empty the version.
- A first version that starts after the entity's first state is moved back to that state, if the registry entry's `created_at` shows it already existed then. Up to 2.4.1 the version was stamped when the integration heard of the entity, just after the first state, or ~30 s later for entities created during HA startup ([#19](https://github.com/flaksit/ha-timescaledb-recorder/issues/19)); first versions now start at `created_at`. The entity's device follows, but never before its own `created_at`.

Each table is first copied to `<table>_prerepair_<utc timestamp>`, the only full record of the old values; drop those once satisfied. Deleted or clamped rows, and rows whose start moved, are also archived as JSONB in `scd2_repair_quarantine`, tagged with the run id.

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
- **Coverage** — states no version covers: entities never in the registry (expected), states before an entity's first version (normal where history predates the dimension), and states after its last close or pointing at an uncovered device or area (the dimension stopped describing something that kept recording). Entity versions starting before their device, and first versions starting after their first state, are listed here too; `--apply` moves them as above.

A second `--apply` changes nothing.

### If it fails

Each table is repaired in one transaction under `SHARE ROW EXCLUSIVE` with a 10-second `lock_timeout`, backup included. HA can keep running; if the metadata worker holds the lock, the repair times out having changed nothing — run it again. Any interruption leaves the current table untouched and earlier tables repaired, each consistent on its own; re-running converges.

## Development

`uv run pytest` runs the suite. The `*_db` test modules skip unless `SCD2_TEST_DSN` points at a scratch TimescaleDB on loopback; see the docstring of `tests/test_scd2_invariant_db.py` for the Docker command.

`uv run basedpyright` type-checks the project (config in `pyproject.toml`); it must report 0 errors.

CI (`.github/workflows/ci.yml`) runs basedpyright and the full suite, DB modules included, against a TimescaleDB service container.

[Renovate](https://docs.renovatebot.com/) (`renovate.json`) opens weekly PRs for GitHub Actions, the CI TimescaleDB image, Python dependencies and `uv.lock`. It bumps psycopg in `pyproject.toml` and `manifest.json` together; merging such a PR changes what users' HA installs on the next release. The `hacs.json` minimum HA version is not managed — raise it by hand.
