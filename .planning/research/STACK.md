# Stack Research

**Domain:** HA custom component — PostgreSQL/TimescaleDB write path (threaded worker)
**Researched:** 2026-04-19
**Confidence:** HIGH (all critical claims verified against official sources)

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| `psycopg[binary]` | `==3.3.3` | Sync PostgreSQL driver replacing asyncpg | Only pg driver designed for sync/threaded use; binary wheel avoids libpq dependency; pipeline mode auto-used in executemany; supports Python 3.14+; no conflict with HA core |
| `threading.Thread` | stdlib | Worker isolation from HA event loop | Matches HA's own recorder pattern; sync psycopg3 fits naturally; no event loop inside thread needed |
| `threading.Lock` | stdlib | Serialise flush vs. backfill | Single writer, two paths (periodic flush + on-recovery backfill); stdlib, no deps |
| `collections.deque` | stdlib | Bounded ring buffer for state events | `maxlen=10000` gives drop-oldest for free; O(1) append/popleft; no external dep |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `psycopg.types.json.Jsonb` | (ships with psycopg) | Wrap Python dicts for JSONB columns | Required — psycopg3 does NOT auto-adapt dicts to JSONB; explicit wrapper mandatory |
| `asyncio.run_coroutine_threadsafe` | stdlib | Call HA async APIs from worker thread | Use to invoke `hass.async_add_executor_job`, `persistent_notification`, etc. from inside the worker thread |

### Development Tools

| Tool | Purpose | Notes |
|------|---------|-------|
| `uv` | Package management and venv | Used project-wide; `uv add psycopg[binary]` for dev venv; HA installs via its own pip from manifest.json |
| `psycopg[pool]` | Optional connection pooling | Not needed for this project — single dedicated thread owns the connection; pool adds complexity with no benefit here |

## Installation

```bash
# Dev venv (via uv)
uv add "psycopg[binary]==3.3.3"

# manifest.json requirements (HA installs this on component load)
"requirements": ["psycopg[binary]==3.3.3"]
```

Note: `psycopg[binary]` installs both `psycopg==3.3.3` and `psycopg-binary==3.3.3` as a unit. Do not pin `psycopg-binary` separately — use the extras syntax on `psycopg` so version coupling is enforced.

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| `psycopg[binary]` | `psycopg[c]` | If building from source is acceptable (e.g. HA OS with build tools present); produces a smaller install but requires gcc and libpq-dev |
| `psycopg[binary]` | `asyncpg` | Never for this project — asyncpg is async-only and running it inside a thread requires a nested event loop, which is fragile and unsupported |
| `psycopg[binary]` | `psycopg2-binary` | Only if dropping Python 3.10+ support; psycopg3 is the current upstream; psycopg2 is in maintenance mode only |
| Single `threading.Thread` | `concurrent.futures.ThreadPoolExecutor` | If multiple parallel DB workers were needed; our workload is I/O-bound single-writer so a pool adds overhead with no gain |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `asyncpg` | Async-only; requires event loop; running in a thread requires `asyncio.new_event_loop()` inside the thread — fragile, unsupported, crashes propagate to the creating loop | `psycopg[binary]` with sync API |
| `psycopg[pool]` (`ConnectionPool`) | Our architecture is single-thread, single-connection; a pool adds overhead and the pool's own background threads complicate shutdown lifecycle in HA | Plain `psycopg.connect()` owned by the worker thread |
| `psycopg` (pure Python, no extras) | 3–5× slower than the binary/C extension variant; binary wheel is available for all HA-supported platforms including armv7, arm64, amd64 | `psycopg[binary]` |
| Sharing one `psycopg.Cursor` across calls | Cursors are **not thread-safe**; even though Connection is thread-safe, sharing a cursor across calls causes data corruption | Create a new cursor per operation (cursors are cheap) |
| `psycopg2` | Maintenance-mode; not compatible with Python 3.14+; HA 2025.10 removed it as a bundled dep, making it unreliable as a transitive assumption | `psycopg[binary]` |

## Stack Patterns by Variant

**Single dedicated worker thread (this project's design):**
- Use `psycopg.connect(dsn)` — one connection owned by the thread for its full lifetime
- Re-connect on connection loss (catch `psycopg.OperationalError`)
- All cursor creation, execute, commit inside the worker thread only
- Call HA async APIs via `asyncio.run_coroutine_threadsafe(coro, hass.loop).result()` or post back via `hass.loop.call_soon_threadsafe(callback)`

**If connection pool were ever needed (not this project):**
- Use `psycopg_pool.ConnectionPool` (separate `psycopg[pool]` extra)
- Pool is thread-safe; individual connections checked out from it are not shared across threads

## Version Compatibility

| Package | Compatible With | Notes |
|---------|-----------------|-------|
| `psycopg[binary]==3.3.3` | Python >=3.10 (incl. 3.14) | Verified on PyPI — explicit Python 3.14 support confirmed |
| `psycopg[binary]==3.3.3` | TimescaleDB ≥ 2.x | TimescaleDB is a Postgres extension; psycopg3 speaks standard libpq protocol; no special support required |
| `psycopg[binary]==3.3.3` | HA 2026.3.4 | No conflict — HA core `requirements.txt` contains NO PostgreSQL driver (verified against `dev` branch April 2026). SQLAlchemy==2.0.49 is present but does not pull asyncpg or psycopg as a transitive dep |
| `psycopg[binary]==3.3.3` | `asyncpg==0.31.0` (old dep) | Not co-installed; manifest.json replaces `asyncpg==0.31.0` with `psycopg[binary]==3.3.3` |

## Type Adaptation Notes (psycopg3-specific, affects ingester code)

These are behavioural differences from asyncpg that affect the migration:

**JSONB columns (e.g. `attributes`, `context` in `ha_states`):**
psycopg3 does NOT auto-adapt Python dicts to JSONB. Explicit wrapper required:
```python
from psycopg.types.json import Jsonb
# row = {"attributes": Jsonb(state.attributes), ...}
```
Failing to wrap will raise a type error at runtime.

**TEXT[] columns (e.g. label arrays in dim tables):**
Python `list[str]` adapts automatically to `TEXT[]`. No wrapper needed.

**executemany batch inserts:**
`cursor.executemany(sql, rows)` internally uses pipeline mode (since psycopg 3.1). This is automatic — no special pipeline context manager needed for batch inserts. Performance is equivalent to asyncpg's `executemany`.

## manifest.json Requirements Convention

**Confirmed conventions (HA developer docs + HACS practice):**

1. **Pin exact version** with `==`. HA docs show `"pychromecast==3.2.0"` as the canonical example. Unpinned requirements are allowed syntactically but will cause non-deterministic behaviour across HA restarts as pip may upgrade.
2. **Extras syntax is supported**: `"psycopg[binary]==3.3.3"` is valid pip syntax and works in manifest.json.
3. **No conflict with core**: Custom integrations must not re-declare libraries already in HA's `requirements.txt`. Since HA core has no PG driver, `psycopg[binary]` is safe to declare.
4. **HA installs into `deps/` subdirectory** of config dir on first component load. No build tools available in HA OS/Container — binary wheel is essential (source build would fail).
5. **Version mismatch conflict risk**: If HA ever bundles `psycopg` in core (unlikely given it has never bundled a PG driver), the custom component's pinned version would conflict. This is a theoretical risk only.

## Sources

- [psycopg PyPI](https://pypi.org/project/psycopg/) — version 3.3.3 confirmed, Python >=3.10 support, released 2026-02-18 (HIGH confidence)
- [psycopg-binary PyPI](https://pypi.org/project/psycopg-binary/) — 3.3.3, multi-arch wheels including arm64/armv7 (HIGH confidence)
- [psycopg3 Install docs](https://www.psycopg.org/psycopg3/docs/basic/install.html) — `psycopg[binary]` as recommended install method (HIGH confidence)
- [psycopg3 Type Adaptation](https://www.psycopg.org/psycopg3/docs/basic/adapt.html) — Jsonb wrapper required for dicts; list→TEXT[] is automatic (HIGH confidence)
- [psycopg3 Concurrent Operations](https://www.psycopg.org/psycopg3/docs/advanced/async.html) — Connection thread-safe; Cursor not thread-safe; one cursor per operation (HIGH confidence)
- [HA core requirements.txt (dev branch)](https://github.com/home-assistant/core/blob/dev/requirements.txt) — no psycopg/asyncpg present; SQLAlchemy==2.0.49 only DB-adjacent dep (HIGH confidence, verified April 2026)
- [HA Integration Manifest docs](https://developers.home-assistant.io/docs/creating_integration_manifest/) — requirements pip-compatible strings, exact pinning shown in examples (HIGH confidence)
- [HA issue #153371](https://github.com/home-assistant/core/issues/153371) — psycopg2 removed from HA Container 2025.10; confirms PG drivers are NOT bundled (MEDIUM confidence — resolution details not visible, but removal confirms our approach)
- [psycopg3 release notes](https://www.psycopg.org/psycopg3/docs/news.html) — 3.3.3 is current stable; 3.3.4.dev1 in progress (HIGH confidence)

---
*Stack research for: ha-timescaledb-recorder v1.1 robust ingestion — psycopg3 + threaded worker*
*Researched: 2026-04-19*
