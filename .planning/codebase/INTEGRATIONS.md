---
generated: 2026-04-18
focus: tech
---

# External Integrations

**Analysis Date:** 2026-04-18

## APIs & External Services

**Home Assistant Core:**
- Service: Home Assistant event bus and registries (in-process, not over network)
- Used for: subscribing to `EVENT_STATE_CHANGED`, `EVENT_ENTITY_REGISTRY_UPDATED`, `EVENT_DEVICE_REGISTRY_UPDATED`, `EVENT_AREA_REGISTRY_UPDATED`, `EVENT_LABEL_REGISTRY_UPDATED`
- Client: `homeassistant.core`, `homeassistant.helpers.entity_registry`, `homeassistant.helpers.device_registry`, `homeassistant.helpers.area_registry`, `homeassistant.helpers.label_registry`
- Auth: None — runs inside HA process with direct Python API access

**HACS (Home Assistant Community Store):**
- Service: HACS distribution platform
- Used for: integration discovery and installation by end users
- Config: `hacs.json` at repo root

## Data Storage

**Databases:**
- TimescaleDB (PostgreSQL with TimescaleDB extension)
  - Connection: DSN string stored in HA config entry (`CONF_DSN`); entered by user during config flow, validated by attempting `asyncpg.connect(dsn)` then immediately closing
  - Client: `asyncpg` 0.31.0 (async, connection pool)
  - Pool config: `min_size=1`, `max_size=3`, `max_inactive_connection_lifetime=300s`, `max_queries=50_000`, `command_timeout=30s`, `ssl=False`
  - Pool usage: acquired per-operation with `async with pool.acquire() as conn`; `pool.expire_connections()` called on `PostgresConnectionError` to force reconnect

**Tables created by the integration (all DDL is idempotent via `IF NOT EXISTS`):**
- `states` — TimescaleDB hypertable; fact table for state history
  - Columns: `last_updated TIMESTAMPTZ`, `last_changed TIMESTAMPTZ`, `entity_id TEXT`, `state TEXT`, `attributes JSONB`
  - Hypertable chunk interval: configurable (default 7 days)
  - Compression: enabled, segmented by `entity_id`, ordered by `last_updated DESC`; default compress after 2 hours
  - Index: `idx_states_entity_time ON states (entity_id, last_updated DESC)`
- `entities` — SCD2 dimension table for entity registry metadata
- `devices` — SCD2 dimension table for device registry metadata
- `areas` — SCD2 dimension table for area registry metadata
- `labels` — SCD2 dimension table for label registry metadata

**File Storage:**
- Local filesystem only — no external object storage

**Caching:**
- None — no Redis/Memcached; in-process write buffer (`StateIngester._buffer: list[tuple]`) provides batching before DB writes

## Authentication & Identity

**Auth Provider:**
- None — the integration itself has no authentication layer
- TimescaleDB credentials embedded in the DSN string (PostgreSQL standard authentication); DSN stored in HA's encrypted config entry storage

## Monitoring & Observability

**Error Tracking:**
- None — no Sentry or similar external service

**Logs:**
- Standard Python `logging` module; logger name `custom_components.timescaledb_recorder` (via `logging.getLogger(__name__)` in each module)
- Log levels used: `DEBUG` for skip/no-op events, `WARNING` for connection errors with row counts, `ERROR` for `PostgresError` batch drops

## CI/CD & Deployment

**Hosting:**
- Deployed as a HACS custom component inside user's Home Assistant instance
- No cloud hosting; runs locally on the HA host

**CI Pipeline:**
- Not detected (no GitHub Actions workflow files found in repo)

## Webhooks & Callbacks

**Incoming:**
- None — no HTTP endpoints exposed

**Outgoing:**
- None — integration only writes to TimescaleDB; no outbound webhooks

## Environment Configuration

**Required configuration (user-provided via HA config flow UI):**
- `dsn` — PostgreSQL DSN for TimescaleDB (e.g. `postgresql://user:pass@host:5432/dbname`)

**Tunable options (HA options flow, all have defaults):**
- `write_batch_size_records` — default 200, range 1–10000
- `flush_interval_seconds` — default 10, range 1–300
- `chunk_interval_days` — default 7, range 1–365
- `compress_after_hours` — default 2, range 1–720

**Secrets location:**
- DSN (containing DB password) stored in HA's config entry storage (`config/.storage/core.config_entries`), which HA manages and encrypts at rest

---

*Integration audit: 2026-04-18*
