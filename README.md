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
- A running PostgreSQL + TimescaleDB instance (the [TimescaleDB HA app](https://github.com/flaksit/hass-timescaledb) is the intended companion)
- The DSN user must have `CREATE TABLE` privileges — use the `homeassistant` role created by the app, **not** `homeassistant_rw` which is read/write only and lacks DDL rights

## Installation

### HACS (recommended)

1. Open HACS in your Home Assistant UI
2. Go to **Integrations** and click the three-dot menu in the top-right corner
3. Select **Custom repositories**, paste `https://github.com/flaksit/ha-timescaledb-recorder`, choose category **Integration**, and click **Add**
4. Search for "TimescaleDB Recorder" and click **Install**
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/ha_timescaledb/` directory into your HA config's `custom_components/` folder
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
ha_timescaledb:
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

## Differences from the built-in recorder

### Unavailable and unknown states are always recorded

The built-in HA recorder skips `unavailable` and `unknown` states to save SQLite storage. This integration records them because they carry meaningful information: an "unavailable" reading means the sensor was offline, which is fundamentally different from "the value stayed the same." Omitting these states creates a false impression of continuity in the time series and makes it impossible to accurately track device reliability or uptime.

### Entity filtering is additive, not a replacement

Entity filtering works the same way (same include/exclude syntax), but this integration runs alongside the built-in recorder — it does not replace it. Both can have independent filter configurations.

## Troubleshooting

**Cannot connect to TimescaleDB**

- Verify the DSN in the integration options (Settings > Devices & Services > Configure)
- Check that the TimescaleDB app is running and shows "Started" in the HA Supervisor panel
- Confirm the user in the DSN is the `homeassistant` role (has DDL rights) — `homeassistant_rw` cannot create tables

**No data appearing in `ha_states`**

- Open the HA log viewer and filter for `custom_components.ha_timescaledb` — any connection errors appear here
- Check your entity filter: if you added an `include` block, only listed domains/entities are written
- The default `flush_interval` is 10 seconds — wait at least 10 seconds after a state change before querying

**Slow writes or high latency**

- Avoid querying a chunk that is currently being compressed. TimescaleDB holds a lock during compression; the background job runs periodically and briefly.
- Increase `batch_size` to reduce flush frequency under high event volume
