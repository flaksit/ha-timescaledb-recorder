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
- A running PostgreSQL + TimescaleDB instance > 2.18 (the [TimescaleDB HA app](https://github.com/flaksit/hass-timescaledb) is the intended companion)
- The DSN user must have `CREATE TABLE` privileges — use the `homeassistant` role created by the app, **not** `homeassistant_rw` which is read/write only and lacks DDL rights

## Installation

### HACS (recommended)

1. Open HACS in your Home Assistant UI
2. Go to **Integrations** and click the three-dot menu in the top-right corner
3. Select **Custom repositories**, paste `https://github.com/flaksit/ha-timescaledb-recorder`, choose category **Integration**, and click **Add**
4. Search for "TimescaleDB Recorder" and click **Install**
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/ha_timescaledb_recorder/` directory into your HA config's `custom_components/` folder
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

Entity filtering is configured in `configuration.yaml` using the same include/exclude syntax as the recorder integration. Add the filter under the integration domain:

```yaml
ha_timescaledb_recorder:
  include:
    domains:
      - sensor
      - binary_sensor
      - switch
  exclude:
    entities:
      - sensor.some_noisy_sensor
    entity_globs:
      - sensor.weather_*
```

When no filter is configured, all entities are ingested.

Include/exclude precedence follows the same rules as the HA recorder. See the [HA recorder filter documentation](https://www.home-assistant.io/integrations/recorder/#configure-filter) for the full precedence matrix.

## Schema

The `ha_states` table is created automatically on the first HA start after the integration is installed:

```sql
CREATE TABLE IF NOT EXISTS ha_states (
    last_updated  TIMESTAMPTZ NOT NULL,
    last_changed  TIMESTAMPTZ NOT NULL,
    entity_id     TEXT        NOT NULL,
    state         TEXT,
    attributes    JSONB
);
```

**Hypertable partitioning** — chunks are 7 days wide (default), partitioned on `last_updated`.

**Compression** — segments by `entity_id`, ordered by `last_updated DESC`. Chunks older than `compress_after_days` (default 7, matching the chunk interval) are compressed automatically by the background policy.

**Index** — `idx_ha_states_entity_time ON ha_states (entity_id, last_updated DESC)` for fast per-entity time-series lookups.

## Querying

### Latest state for every entity

```sql
SELECT DISTINCT ON (entity_id)
    entity_id,
    state,
    last_updated
FROM ha_states
ORDER BY entity_id, last_updated DESC;
```

### Time series for a single sensor

```sql
SELECT last_updated, state::numeric AS value
FROM ha_states
WHERE entity_id = 'sensor.living_room_temperature'
  AND last_updated > NOW() - INTERVAL '7 days'
ORDER BY last_updated;
```

### Check compression status

```sql
SELECT hypertable_name,
       chunk_name,
       is_compressed,
       compressed_total_size,
       uncompressed_total_size
FROM timescaledb_information.chunks
WHERE hypertable_name = 'ha_states'
ORDER BY range_start DESC;
```

### Verify compression policy

```sql
SELECT *
FROM timescaledb_information.compression_settings
WHERE hypertable_name = 'ha_states';
```

## Metadata Sync

In addition to state ingestion, the integration syncs HA registry metadata (entities, devices, areas, labels) to PostgreSQL dimension tables. These tables use **SCD2 (Slowly Changing Dimension Type 2)** temporal tracking, which means every historical version of a registry object is preserved with a time range indicating when it was current.

This enables historically correct joins: for any row in `ha_states`, you can join to the metadata that was current at that exact moment — entity name, area, device, labels, device class, and more.

### Dimension tables

All four tables follow the same SCD2 pattern. The current row for any object has `valid_to IS NULL`. When metadata changes, the existing row is closed (`valid_to` set to the timestamp of the change) and a new row is inserted (`valid_from` = that same timestamp, `valid_to = NULL`). Historical rows are never deleted.

Every table also carries an `extra` JSONB column that stores the full registry object serialisation. This column is for forward-compatibility — when HA adds or renames internal fields across versions, the typed columns remain stable while the raw data is still accessible via `extra`.

#### dim_entities

| Column | Type | Description |
|--------|------|-------------|
| `entity_id` | TEXT | HA entity ID (e.g. `sensor.living_room_temperature`) |
| `ha_entity_uuid` | TEXT | HA's internal stable UUID — survives entity_id renames |
| `name` | TEXT | Friendly name |
| `domain` | TEXT | Domain (e.g. `sensor`, `switch`) |
| `platform` | TEXT | Integration that provides the entity |
| `device_id` | TEXT | FK to `dim_devices.device_id` |
| `area_id` | TEXT | FK to `dim_areas.area_id` |
| `labels` | TEXT[] | Array of label IDs |
| `device_class` | TEXT | Device class (e.g. `temperature`, `power`) |
| `unit_of_measurement` | TEXT | Unit (e.g. `°C`, `W`) |
| `disabled_by` | TEXT | Non-null when entity is disabled |
| `valid_from` | TIMESTAMPTZ | When this version became current |
| `valid_to` | TIMESTAMPTZ | When this version was superseded (NULL = current) |
| `extra` | JSONB | Full registry entry serialisation |

#### dim_devices

| Column | Type | Description |
|--------|------|-------------|
| `device_id` | TEXT | HA device ID |
| `name` | TEXT | Device name |
| `manufacturer` | TEXT | Manufacturer |
| `model` | TEXT | Model |
| `area_id` | TEXT | FK to `dim_areas.area_id` |
| `labels` | TEXT[] | Array of label IDs |
| `valid_from` | TIMESTAMPTZ | When this version became current |
| `valid_to` | TIMESTAMPTZ | When this version was superseded (NULL = current) |
| `extra` | JSONB | Full registry entry serialisation |

#### dim_areas

| Column | Type | Description |
|--------|------|-------------|
| `area_id` | TEXT | HA area ID |
| `name` | TEXT | Area name |
| `valid_from` | TIMESTAMPTZ | When this version became current |
| `valid_to` | TIMESTAMPTZ | When this version was superseded (NULL = current) |
| `extra` | JSONB | Full registry entry serialisation |

#### dim_labels

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

Each event triggers the SCD2 close-and-insert cycle: the current open row is closed (`valid_to = now()`), and a new row is inserted with the updated fields (`valid_from = now()`). Entity renames (entity_id changes) are handled the same way — the old entity_id row closes and a new one opens under the new entity_id.

The dimension tables are created idempotently on every integration startup (same as `ha_states`), so no manual schema migration is needed after updates.

### Example query: point-in-time metadata join

```sql
SELECT s.entity_id, s.state, e.name, e.area_id, a.name AS area_name
FROM ha_states s
JOIN dim_entities e ON e.entity_id = s.entity_id
  AND s.last_updated >= e.valid_from
  AND (e.valid_to IS NULL OR s.last_updated < e.valid_to)
LEFT JOIN dim_areas a ON a.area_id = e.area_id
  AND s.last_updated >= a.valid_from
  AND (a.valid_to IS NULL OR s.last_updated < a.valid_to)
WHERE s.entity_id = 'sensor.living_room_temperature'
ORDER BY s.last_updated DESC
LIMIT 10;
```

The join conditions (`>= valid_from AND (valid_to IS NULL OR < valid_to)`) ensure you get the metadata row that was current at the time each state was recorded — not necessarily the current metadata. This is what makes the join historically correct.

## Differences from the built-in recorder

### Runs alongside the recorder, not a replacement

This integration runs alongside the built-in recorder — it does not replace it. Both can have independent entity filter configurations.

## Troubleshooting

**Cannot connect to TimescaleDB**

- Verify the DSN in the integration options (Settings > Devices & Services > Configure)
- Check that the TimescaleDB app is running and shows "Started" in the HA Supervisor panel
- Confirm the user in the DSN is the `homeassistant` role (has DDL rights) — `homeassistant_rw` cannot create tables

**No data appearing in `ha_states`**

- Open the HA log viewer and filter for `custom_components.ha_timescaledb_recorder` — any connection errors appear here
- Check your entity filter: if you added an `include` block, only listed domains/entities are written
- The default `flush_interval` is 10 seconds — wait at least 10 seconds after a state change before querying

**Slow writes or high latency**

- Avoid querying a chunk that is currently being compressed. TimescaleDB holds a lock during compression; the background job runs periodically and briefly.
- Increase `batch_size` to reduce flush frequency under high event volume

## Backfilling historical gaps

If you installed the integration after HA had already been running for a while, or after a TimescaleDB outage, use the included backfill script to fill the gaps from HA's SQLite recorder.

From the HA host terminal (SSH addon):

```bash
docker exec homeassistant python3 \
    /config/custom_components/ha_timescaledb_recorder/backfill_gaps.py
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
