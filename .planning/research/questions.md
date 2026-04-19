# Research questions

Open questions to resolve during planning / research phases.

## Robust ingestion milestone (2026-04-19)

### psycopg3 compatibility with HA 2026
- Does HA 2026.x pull any PG driver as transitive dep that would conflict with `psycopg[binary]`?
- Check `manifest.json` requirements conventions for HA custom components.
- Confirm psycopg3 works with TimescaleDB features we use (JSONB, TEXT[], hypertable INSERTs, executemany).
- Any known issues with psycopg3 in threaded context (thread-per-connection? connection pool thread-safety?).

### `state_changes_during_period` semantics
- Does it return attribute-only changes (same state string, different attributes) or only state-value changes?
- Confirm `include_start_time_state=False` gives only real events within the window (no synthesized boundary state).
- What does it return for entities that were `unknown`/`unavailable` during the window? Are those captured as real rows?
- Performance: query cost for large windows (e.g. 24h, 50k+ rows). Does it stream or load fully into memory?
- Does it respect HA recorder's own exclude filters, or return raw?

### HA recorder helpers thread safety
- Can `homeassistant.components.recorder.history.*` functions be safely called from a non-HA worker thread?
- Do they require event loop access? Need to call via `hass.async_add_executor_job` from our worker?
- Alternative: worker thread calls `asyncio.run_coroutine_threadsafe(history.*, hass.loop)` to invoke on HA loop.

### HA 2026 notification APIs
- Current API for `persistent_notification.async_create` — any deprecation since 2024?
- `issue_registry` (`homeassistant.helpers.issue_registry`) API shape in 2026 — `async_create_issue` signature, `translation_key` semantics.
- Best practice for clearing resolved issues (`async_delete_issue`?).

### TimescaleDB unique constraint on hypertables
- Confirm `UNIQUE (last_updated, entity_id)` is allowed on `ha_states` hypertable (TimescaleDB requires unique index to include partitioning column).
- If allowed, use for `INSERT ... ON CONFLICT DO NOTHING` as alternative dedup mechanism (backup plan if mutex-based isolation proves insufficient).
- Index space + insert cost delta.

### HA sqlite access model
- Path to HA's sqlite DB: `hass.config.path("home-assistant_v2.db")` — confirm on 2026.
- Can we open a read-only connection while HA recorder writes? (sqlite WAL mode should allow, but verify.)
- Or must we go through `recorder.history.*` helpers exclusively (likely yes)?

### Worker thread lifecycle in HA integration
- Best pattern for starting/stopping threads in HA integration `async_setup_entry` / `async_unload_entry`.
- How to handle `hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)` to gracefully drain worker before process exits.
- HA's own recorder uses `self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, ...)` — follow same pattern.
