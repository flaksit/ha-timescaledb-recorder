# Feature Research: HA API Semantics for v1.1 Robust Ingestion

**Domain:** HA custom component — backfill + observability APIs
**Researched:** 2026-04-19
**Confidence:** HIGH (all findings from HA core source, dev branch)

## Group A — `state_changes_during_period` Semantics

### Import path

```python
from homeassistant.components.recorder.history import state_changes_during_period
```

### Exact signature (dev branch, 2026-03-compatible)

```python
def state_changes_during_period(
    hass: HomeAssistant,
    start_time: datetime,
    end_time: datetime | None = None,
    entity_id: str | None = None,   # REQUIRED — raises ValueError if falsy
    no_attributes: bool = False,
    descending: bool = False,
    limit: int | None = None,
    include_start_time_state: bool = True,
) -> dict[str, list[State]]:
```

Source: `homeassistant/components/recorder/history/__init__.py` (dev branch).

### A1 — Attribute-only changes

**Answer: excluded.** The SQL statement filters on:

```python
(States.last_changed_ts == States.last_updated_ts) | States.last_changed_ts.is_(None)
```

`last_changed_ts` is NULL when only attributes changed; `last_updated_ts` always advances. The
condition retains only rows where the state *value* changed (or the very first row for an entity,
where `last_changed_ts` is NULL by convention). Attribute-only updates do **not** appear in results.

**Implication for backfill:** The returned rows are safe to `INSERT … ON CONFLICT DO NOTHING` against
`states` without concern about double-counting attribute noise. The row count per entity per
window is the number of actual state-value transitions.

### A2 — `include_start_time_state=False`

**Answer: correct interpretation.** With `False`, the function skips the boundary-state subquery
entirely — no synthesized row at `start_time`, only real rows with `last_updated_ts > start_time`.
With `True` (default), it issues an extra subquery to find the most recent state whose
`last_updated_ts < start_time` and prepends that row (using the historical record as-is, not
interpolated). For backfill use `include_start_time_state=False` to avoid re-ingesting a row
already written at or before the checkpoint.

### A3 — Unknown/unavailable during window

**Answer: captured as real rows.** `unknown` and `unavailable` are ordinary state strings in HA.
When an entity transitions to `unavailable`, a row is written with `state = 'unavailable'` and
`last_changed_ts = last_updated_ts`. These satisfy the filter condition and appear in results
normally. Entities that were unknown for the *entire* window produce rows only for the transition
into that state if it falls in the window; they are not synthesized.

### A4 — Performance: streaming vs in-memory

**Answer: fully in-memory.** The function builds a SQLAlchemy lambda-statement, executes it
via `execute_stmt_lambda_element`, and collects all matching `State` objects into a
`dict[str, list[State]]` before returning. There is no lazy cursor or streaming generator.

For large windows (24h, 50k+ rows): expect full result set to be materialised in RAM plus ORM
object overhead per row (~500–1000 bytes per `State`). 50k rows ≈ 25–50 MB. Acceptable for
occasional startup backfill; not acceptable for continuous use.

**Mitigation strategy for backfill:** chunk the window into 1-hour or 6-hour slices so each
call returns at most ~5k rows, then release and flush before next chunk. The write mutex pattern
in the PROJECT.md already requires per-chunk release — this is consistent.

### A5 — Recorder exclude filters

**Answer: filters are NOT applied.** The function explicitly rejects filter arguments:

```python
if filters is not None:
    raise NotImplementedError("Filters are no longer supported")
```

The function returns all state changes for the requested entity regardless of the recorder's
`exclude_domains`, `include_entities`, etc. config.

**Implication:** Our own entity filter (the existing include/exclude list in `const.py`) must be
applied *after* fetching from HA — iterate the returned dict and drop entity_ids not in our
filter. This is already how the live ingestion path works (filter before buffering).

### Usage example (backfill loop)

```python
# Called from worker thread via recorder.async_add_executor_job — see Group B
from homeassistant.components.recorder.history import state_changes_during_period
from datetime import datetime, timezone

def _backfill_chunk(
    hass: HomeAssistant,
    entity_id: str,
    chunk_start: datetime,
    chunk_end: datetime,
) -> list[State]:
    # Runs on recorder's DB executor thread
    result = state_changes_during_period(
        hass,
        start_time=chunk_start,
        end_time=chunk_end,
        entity_id=entity_id,
        include_start_time_state=False,   # no boundary row
        no_attributes=False,              # we store attributes
    )
    return result.get(entity_id, [])
```

### Alternative: `get_significant_states` for multi-entity calls

```python
from homeassistant.components.recorder.history import get_significant_states

def get_significant_states(
    hass: HomeAssistant,
    start_time: datetime,
    end_time: datetime | None = None,
    entity_ids: list[str] | None = None,   # list, required
    filters: Filters | None = None,         # always raises NotImplementedError
    include_start_time_state: bool = True,
    significant_changes_only: bool = True,
    minimal_response: bool = False,
    no_attributes: bool = False,
    compressed_state_format: bool = False,
) -> dict[str, list[State | dict[str, Any]]]:
```

`significant_changes_only=True` applies domain-specific significance logic (e.g. some sensor
domains suppress minor value changes). For our backfill we want every recorded change — use
`state_changes_during_period` per entity to avoid the significance filter, or set
`significant_changes_only=False` in `get_significant_states`.

**Tradeoff:** `get_significant_states` can fetch multiple entities in one DB round trip;
`state_changes_during_period` is one entity per call but has cleaner semantics for our use case
(significance filter off by default, change-only filter always on). Recommend
`state_changes_during_period` per entity unless per-call overhead proves measurable.

## Group B — Thread Safety

### B6 — Safety from non-HA worker thread

**Answer: NOT directly safe.** `state_changes_during_period` calls `session_scope(hass=hass)`,
which internally calls `get_instance(hass)`:

```python
@functools.lru_cache(maxsize=1)
def get_instance(hass: HomeAssistant) -> Recorder:
    return hass.data[DATA_INSTANCE]
```

`hass.data` access is not thread-safe in general. However, `get_instance` uses `lru_cache` and
the recorder instance is stored at startup before any worker thread exists — in practice the read
is safe after setup completes, but this relies on implementation detail not contract.

More critically, `session_scope` calls `instance.get_session()` which creates a SQLAlchemy session
from the recorder's scoped factory. SQLAlchemy scoped sessions are thread-local — each thread gets
its own session, which is safe. However, the recorder's `DBInterruptibleThreadPoolExecutor` is the
intended execution context for these calls. Calling from an arbitrary OS thread outside that pool
is unsupported by HA's architecture.

**Conclusion:** Do not call `state_changes_during_period` directly from our worker thread.

### B7 — Required calling pattern

**Answer: use `recorder_instance.async_add_executor_job` from the event loop.** This is the
canonical pattern used by HA's own `history_stats` integration:

```python
# From history_stats/data.py (HA core, dev branch)
instance = get_instance(self.hass)  # called from event loop
states = await instance.async_add_executor_job(
    self._state_changes_during_period,
    start_ts,
    end_ts,
)
```

`Recorder.async_add_executor_job` is a `@callback` (not `async def`) that schedules the target
on `self._db_executor` (a `DBInterruptibleThreadPoolExecutor`). It returns an `asyncio.Future`.
Because it is `@callback`-decorated, it must be called **from the event loop thread**.

**Implication for our architecture:** Backfill cannot be initiated directly from our OS worker
thread. The worker thread must schedule backfill via `hass.add_job` (which calls
`loop.call_soon_threadsafe`) to post a coroutine onto the event loop, which then awaits
`instance.async_add_executor_job(...)`. The result (list of states) is returned to the event loop
caller, which must hand it back to the worker thread (e.g. via a queue or `Future`).

### B8 — Alternative: `asyncio.run_coroutine_threadsafe`

**Answer: viable but roundabout.** From a worker thread:

```python
import asyncio

future = asyncio.run_coroutine_threadsafe(
    _async_fetch_backfill_chunk(hass, entity_id, start, end),
    hass.loop,
)
states = future.result(timeout=60)  # blocks worker thread until done
```

Where `_async_fetch_backfill_chunk` is a coroutine that awaits
`instance.async_add_executor_job(state_changes_during_period, ...)`.

This pattern is semantically equivalent to B7 but inverted: the worker thread submits to the
event loop and blocks. This is acceptable for our backfill use case (backfill already runs in a
dedicated thread, blocking it is fine). It avoids having to redesign the backfill coordination
flow around `asyncio.Queue`.

**Recommended pattern for backfill worker:**

```python
def _backfill_worker(hass: HomeAssistant, entity_ids: list[str], start: datetime, end: datetime):
    # Running in our OS worker thread
    loop = hass.loop
    recorder_instance = asyncio.run_coroutine_threadsafe(
        _get_recorder_instance(hass), loop
    ).result()

    for entity_id in entity_ids:
        # Post DB query onto recorder's executor via the event loop
        states = asyncio.run_coroutine_threadsafe(
            _async_fetch_states(recorder_instance, entity_id, start, end),
            loop,
        ).result(timeout=120)
        # Now write states to TimescaleDB using our sync psycopg3 connection
        _write_states(states)

async def _async_fetch_states(
    recorder_instance, entity_id: str, start: datetime, end: datetime
) -> list[State]:
    return await recorder_instance.async_add_executor_job(
        state_changes_during_period,
        hass,          # hass reference captured via closure
        start,
        end,
        entity_id,
        False,         # no_attributes=False — we need them
        False,         # descending=False
        None,          # limit=None
        False,         # include_start_time_state=False
    )
```

## Group C — Notification APIs (HA 2026)

### C9 — `persistent_notification.async_create`

**Status: stable, no deprecation.** The API has not changed since 2024.

**Import path:**

```python
from homeassistant.components.persistent_notification import async_create, async_dismiss
```

**Exact signatures (dev branch):**

```python
@callback
def async_create(
    hass: HomeAssistant,
    message: str,
    title: str | None = None,
    notification_id: str | None = None,
) -> None: ...

@callback
def async_dismiss(
    hass: HomeAssistant,
    notification_id: str,
) -> None: ...
```

Both are `@callback` — must be called from the event loop thread or scheduled via
`hass.add_job(async_create, hass, message, title, notification_id)` from a worker thread.

**Usage example (from worker thread):**

```python
hass.add_job(
    async_create,
    hass,
    "TimescaleDB worker thread died — check logs. Restart HA to recover.",
    "ha-timescaledb-recorder: Worker died",
    "timescaledb_recorder_worker_dead",
)
```

**When to use vs issue_registry:** Use `persistent_notification` for one-shot events (worker death,
backfill completed). Use `issue_registry` for persistent ongoing conditions the user must act on
(DB unreachable >5 min, buffer dropping, recorder disabled).

### C10 — `issue_registry.async_create_issue`

**Import path:**

```python
import homeassistant.helpers.issue_registry as ir
```

**Exact signature (dev branch):**

```python
@callback
def async_create_issue(
    hass: HomeAssistant,
    domain: str,           # your DOMAIN constant
    issue_id: str,         # stable string key, e.g. "db_unreachable"
    *,
    breaks_in_ha_version: str | None = None,
    data: dict[str, str | int | float | None] | None = None,
    is_fixable: bool,      # required — no default
    is_persistent: bool = False,
    issue_domain: str | None = None,
    learn_more_url: str | None = None,
    severity: ir.IssueSeverity,  # required — no default
    translation_key: str,        # required — must match strings.json key
    translation_placeholders: dict[str, str] | None = None,
) -> None: ...
```

`IssueSeverity` enum values: `CRITICAL = "critical"`, `ERROR = "error"`, `WARNING = "warning"`.

`async_create_issue` is idempotent — calling it multiple times with the same `(domain, issue_id)`
updates the existing issue rather than creating duplicates. Safe to call on every monitoring tick.

**translation_key semantics:** Must match a key under `issues` in `strings.json`. The strings.json
structure for a non-fixable informational issue:

```json
{
  "issues": {
    "db_unreachable": {
      "title": "TimescaleDB connection lost",
      "description": "The TimescaleDB recorder cannot reach the database. States are buffering in RAM. Reconnection is automatic."
    },
    "buffer_dropping": {
      "title": "State buffer overflow — data loss",
      "description": "The in-memory buffer is full. Oldest states are being dropped. Check TimescaleDB connectivity."
    }
  }
}
```

`translation_placeholders` allows `{variable}` substitution in the title/description strings.

**Gotcha:** `translation_key` is required even for custom components with no translations shipped.
If `strings.json` does not exist or does not contain the key, HA will log a warning but the issue
will still appear with a fallback display. Ship a `strings.json` with all issue keys to avoid
log noise.

### C11 — Clearing resolved issues

**Answer: use `async_delete_issue`.** No deprecation, stable API.

**Exact signature (dev branch):**

```python
@callback
def async_delete_issue(
    hass: HomeAssistant,
    domain: str,
    issue_id: str,
) -> None: ...
```

Call when the condition resolves (DB reconnected, buffer below threshold). Like `async_create_issue`,
this is `@callback` — must run on event loop. From worker thread:

```python
hass.add_job(ir.async_delete_issue, hass, DOMAIN, "db_unreachable")
```

**Pattern for recurring conditions (e.g. DB unreachable):**

```python
# On condition detected (from worker, posts to event loop):
hass.add_job(
    ir.async_create_issue,
    hass, DOMAIN, "db_unreachable",
    # keyword args via functools.partial or lambda:
)

# Or better — use hass.loop directly since add_job doesn't support kwargs:
asyncio.run_coroutine_threadsafe(
    _post_issue(hass, "db_unreachable", ir.IssueSeverity.ERROR),
    hass.loop,
).result()  # fire-and-forget: don't .result(), just schedule

# On condition resolved:
hass.add_job(ir.async_delete_issue, hass, DOMAIN, "db_unreachable")
```

**Gotcha — `hass.add_job` and keyword arguments:** `hass.add_job` passes only positional args.
`async_create_issue` has many keyword-only parameters. Use a wrapper coroutine or
`functools.partial` to bridge this. The cleanest approach is a thin async helper:

```python
async def _async_create_issue(hass, issue_id, severity, translation_key, **kw):
    ir.async_create_issue(
        hass, DOMAIN, issue_id,
        is_fixable=False,
        severity=severity,
        translation_key=translation_key,
        **kw,
    )

# From worker thread:
asyncio.run_coroutine_threadsafe(
    _async_create_issue(hass, "db_unreachable", ir.IssueSeverity.ERROR, "db_unreachable"),
    hass.loop,
)
# Note: do NOT call .result() if fire-and-forget; the coroutine is tiny and won't fail
```

## Cross-Cutting Gotchas

| # | Gotcha | Impact | Mitigation |
|---|--------|--------|------------|
| 1 | `state_changes_during_period` requires a single `entity_id` — no batch call | O(N) calls for N entities during backfill | Chunk by time-window first, then iterate entities; keep entity count manageable via our own filter |
| 2 | `include_start_time_state=True` (default) adds a boundary row before start_time | Duplicate insertion at backfill boundary | Always pass `include_start_time_state=False` |
| 3 | Results load fully into RAM | 50k rows ≈ 25–50 MB peak | Limit window size per call to ~6 hours or use `limit` param |
| 4 | Recorder exclude filters NOT applied — returns everything | We ingest entities the user excluded | Apply our own entity filter after fetching |
| 5 | `async_add_executor_job` is `@callback` — must call from event loop | Cannot call directly from OS worker thread | Use `asyncio.run_coroutine_threadsafe` from worker, or redesign around hass async task |
| 6 | `async_create_issue` / `async_dismiss` / `async_delete_issue` are `@callback` — event loop only | Worker thread cannot call directly | Use `hass.add_job` for zero-arg cases; wrapper coroutine + `run_coroutine_threadsafe` for keyword-arg cases |
| 7 | `hass.add_job` does not support keyword arguments | `async_create_issue` is keyword-heavy | Define thin wrapper coroutines called via `run_coroutine_threadsafe` |
| 8 | `translation_key` required even for custom components | Missing strings.json causes log warnings | Ship `strings.json` with all issue keys under `"issues"` section |
| 9 | `is_fixable` and `severity` are required (no defaults) | `TypeError` at runtime if omitted | Always specify both; use `is_fixable=False, severity=ir.IssueSeverity.WARNING/ERROR` |

## Sources

- HA core dev branch — `homeassistant/components/recorder/history/__init__.py`
- HA core dev branch — `homeassistant/components/recorder/db_schema.py`
- HA core dev branch — `homeassistant/helpers/recorder.py` (`session_scope`, `get_instance`)
- HA core dev branch — `homeassistant/components/recorder/core.py` (`Recorder.async_add_executor_job`)
- HA core dev branch — `homeassistant/components/persistent_notification/__init__.py`
- HA core dev branch — `homeassistant/helpers/issue_registry.py`
- HA core dev branch — `homeassistant/components/history_stats/data.py` (canonical call pattern)
- HA core dev branch — `homeassistant/core.py` (`hass.add_job` implementation)
- [HA Developer Docs — Thread safety with asyncio](https://developers.home-assistant.io/docs/asyncio_thread_safety/)
- [HA Developer Docs — Repairs/Issues](https://developers.home-assistant.io/docs/core/platform/repairs/)
