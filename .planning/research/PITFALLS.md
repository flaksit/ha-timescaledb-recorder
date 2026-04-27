# Pitfalls Research

**Domain:** HA custom component — threaded worker + psycopg3 + asyncio integration
**Researched:** 2026-04-19
**Confidence:** HIGH (all claims verified against official HA developer docs, psycopg3 docs, CPython source, and HA core source)

## Critical Pitfalls

### Pitfall 1: Calling `hass.async_*` methods directly from the worker thread

**What goes wrong:**
The worker thread calls `hass.async_create_task`, `hass.bus.async_fire`, `async_write_state`, or any registry mutation directly. These are not thread-safe. Internals of the asyncio event loop get corrupted or the call silently schedules work on the wrong loop, causing intermittent crashes, lost events, or state corruption that is extremely hard to reproduce.

**Why it happens:**
The `async_*` prefix reads as "the async version" of a method; developers assume it is always the right one. The distinction between "safe to call from any thread" (sync API) and "must only be called from the event loop thread" (async API) is not obvious unless you have read the HA thread-safety documentation.

**How to avoid:**
Use only the thread-safe sync equivalents from the worker thread:
- `hass.create_task` — not `hass.async_create_task`
- `hass.bus.fire` — not `hass.bus.async_fire`
- `hass.add_job` — for anything that lacks a sync equivalent

For full coroutine scheduling (e.g., reading history API), use `asyncio.run_coroutine_threadsafe(coro, hass.loop)` and call `.result()` only when a blocking wait is acceptable — never inside a hot path.

**Warning signs:**
- `RuntimeError: Non-thread-safe operation invoked on an event loop other than the current one`
- `RuntimeError: This event loop is already running` appearing in worker thread tracebacks
- Intermittent `AttributeError` or `asyncio.InvalidStateError` that only occurs under load

**Phase to address:** Worker thread scaffolding (Phase 1 / thread setup)

---

### Pitfall 2: `run_coroutine_threadsafe` deadlock during HA shutdown

**What goes wrong:**
The worker thread calls `run_coroutine_threadsafe(...).result()` (blocking) during teardown. HA's event loop has already entered the `stopping` stage and stopped dispatching new work. The future never resolves; `.result()` blocks forever; HA's executor cannot join the thread; HA hangs at shutdown.

**Why it happens:**
This was a real HA bug (PR #45807). The event loop stops accepting new callbacks before executor threads are joined, creating a window where a thread submitting work to the loop will block until an OS-level timeout kills the process. Custom components that copy the HA recorder pattern without understanding this nuance are especially vulnerable.

**How to avoid:**
- Use fire-and-forget scheduling (`hass.add_job`) wherever possible from the worker — do not wait on the result.
- If you must use `run_coroutine_threadsafe(...).result()`, wrap with a timeout: `.result(timeout=N)` and handle `concurrent.futures.TimeoutError`.
- Drain and stop the worker via a sentinel value (`None` / `StopTask`) in the queue, never by calling async HA APIs from the thread during teardown.
- Register the shutdown handler via `hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, ...)` in `async_setup_entry` — this fires in the event loop, so the handler is async-safe and can call `await hass.async_add_executor_job(worker_thread.join)` to wait for the worker without risk of deadlock.

**Warning signs:**
- HA restart takes much longer than usual (> 30 s)
- `HomeAssistant stop_stage_shutdown_timeout exceeded` in logs
- Worker thread still `is_alive()` after HA has logged "Stopping Home Assistant"

**Phase to address:** Worker thread lifecycle (Phase 1)

---

### Pitfall 3: Thread-unsafe `queue.SimpleQueue` under free-threaded Python (no-GIL)

**What goes wrong:**
HA 2026.x runs Python 3.14 with the GIL enabled by default, so `queue.SimpleQueue` is safe. However, if HA ever adopts the free-threaded build (`--disable-gil`), `SimpleQueue` is **not thread-safe** — its C implementation relies on GIL exclusion (CPython issue #113884). Code that works perfectly today silently corrupts queue internals in a future free-threaded deployment.

**Why it happens:**
CPython's `queue.SimpleQueue` stores mutable internal state that is protected by the GIL, not by an explicit lock. The Python docs describe it as thread-safe, but that guarantee assumes GIL-present builds. In free-threaded builds the guarantee evaporates.

**How to avoid:**
Use `queue.SimpleQueue` as intended for now (GIL-enabled Python 3.14). Add a comment noting the GIL assumption so the future reader understands the constraint. If HA ever moves to a free-threaded build, switch to `queue.Queue` (which uses `threading.Condition` internally and is safe without the GIL).

For the actual producer/consumer design: the asyncio event loop thread calls `queue.put_nowait()` (or `.put()`) and the worker thread calls `queue.get()`. This cross-thread, cross-model usage is the intended use case for `queue.SimpleQueue` — it is **not** an `asyncio.Queue` misuse.

**Warning signs:**
- Only manifests under free-threaded Python (opt-in `python3t` interpreter)
- Random `IndexError` or silent data loss in queue under high throughput with `--disable-gil`

**Phase to address:** Worker thread scaffolding (Phase 1)

---

### Pitfall 4: Blocking the HA event loop with `psycopg3.ConnectionPool` operations

**What goes wrong:**
`psycopg3.ConnectionPool` (sync pool) is designed for threaded code. If `pool.wait()`, `pool.open()`, or `pool.close()` is called directly from `async_setup_entry` or `async_unload_entry` (both run in the event loop), they block the loop for the duration of the TCP connect/handshake to PostgreSQL. HA logs `Detected blocking call` and may flag the integration.

**Why it happens:**
`pool.wait()` is synchronous and blocks until `min_size` connections are established. `pool.close()` flushes and joins the pool's own background workers. Calling either from an async function without wrapping in an executor is a classic asyncio trap.

**How to avoid:**
All blocking psycopg3 pool operations must go through `hass.async_add_executor_job`:
```python
# In async_setup_entry:
await hass.async_add_executor_job(pool.open)
await hass.async_add_executor_job(pool.wait)

# In async_unload_entry:
await hass.async_add_executor_job(pool.close)
```
Alternatively, open the pool lazily inside the worker thread's `run()` method (before entering the event loop), which is the cleanest approach — the pool never touches the event loop thread at all.

**Warning signs:**
- `Detected blocking call to socket.connect_ex inside the event loop` in HA logs during startup
- UI responsiveness drops for a second during integration load/unload
- HA 2024.5+ logging `blocking_integration` warnings pointing at your component

**Phase to address:** Worker thread scaffolding (Phase 1)

---

### Pitfall 5: Using `psycopg3.Connection` shared between the worker thread and any other context

**What goes wrong:**
Unlike `ConnectionPool`, a bare `psycopg3.Connection` object — while technically documented as thread-safe (cursors are not) — shares a single transaction across all threads. If two threads concurrently use cursors on the same connection, a DB error in one cursor puts the entire connection into an aborted transaction state until rollback. The other thread's pending operations fail with `InFailedSqlTransaction`.

**Why it happens:**
Developers read "Connection is thread-safe" and conclude that one shared connection suffices. The cursor-level unsafety and shared transaction state are documented caveats that are easy to miss.

**How to avoid:**
Use `psycopg3.ConnectionPool` with `min_size=1, max_size=1` for the single worker thread pattern. The pool handles reconnection, health-checking, and lifetime management that a bare connection does not. Never check out a connection from the pool in a context where two callers could hold it simultaneously.

**Warning signs:**
- `psycopg.errors.InFailedSqlTransaction` appearing on operations that should succeed
- Intermittent row-drop after a previous SQL error
- Cursor operations silently no-op after an unrelated exception

**Phase to address:** Worker thread scaffolding / psycopg3 integration (Phase 1)

---

### Pitfall 6: Not handling the `reconnect_failed` callback — silent write loss during extended DB outage

**What goes wrong:**
When TimescaleDB is unreachable, psycopg3's `ConnectionPool` retries with exponential backoff. After `reconnect_timeout` seconds of failure it calls the optional `reconnect_failed` callback and then **keeps trying indefinitely in the background**. If no callback is registered, the pool is quiet — no log, no alert — while the in-RAM buffer fills up and starts dropping records.

**Why it happens:**
Developers focus on the hot path (successful inserts) and forget that the pool's reconnection logic is opaque. The default reconnect_failed is a no-op, so nothing appears in logs unless you hook it.

**How to avoid:**
Register a `reconnect_failed` callback on the pool that:
1. Logs a WARNING (not DEBUG) with the connection string (DSN, redacted password).
2. Fires a HA repair issue via `issue_registry.async_create_issue` to surface the outage in the UI.
3. Does NOT raise or kill the thread — the pool will keep retrying.

Also check `pool.get_stats()` periodically from the watchdog to detect `pool_unavailable` state.

**Warning signs:**
- No DB errors in logs but no new rows in TimescaleDB
- `pool.get_stats()["pool_available"] == 0` and `pool_waiting > 0` for an extended period
- HA integration loads without error but records silently stop

**Phase to address:** Observability / error handling (Phase 2)

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Single bare `psycopg3.Connection` instead of pool | Simpler code, no pool dependency | No reconnect logic, no health-check, shared transaction risk | Never — pool with min=max=1 has trivial cost |
| Skip `await hass.async_add_executor_job(pool.open)` and call `pool.open()` directly in `async_setup_entry` | Two fewer lines | Blocks event loop during TCP connect; HA flags as blocking integration | Never |
| Use `asyncio.run_coroutine_threadsafe(...).result()` without timeout | Simpler call site | Permanent deadlock on HA shutdown if loop stops before future resolves | Never without timeout + `TimeoutError` handler |
| Call `hass.bus.async_fire` from worker thread | Avoids setting up `hass.add_job` plumbing | Non-thread-safe; unpredictable corruption; hard to debug | Never |
| No `reconnect_failed` callback on pool | One less callback to write | Silent data loss during DB outage; no user-visible alert | Never in production |
| `queue.SimpleQueue` without GIL-dependency comment | Slightly cleaner code | Future free-threaded Python maintainer won't understand the constraint | Acceptable in v1.1 with comment |

---

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| HA event bus → worker thread | Forgetting the producer is `@callback` and must not block; calling `queue.put()` which could block if queue is bounded | Use `queue.SimpleQueue.put_nowait()` (unbounded, never blocks); apply backpressure in consumer, not producer |
| psycopg3 pool in threaded HA component | Creating pool in `__init__` or `async_setup_entry` directly | Create pool inside worker `run()` before entering main loop, or open via executor job |
| HA shutdown sequence | Listening for `EVENT_HOMEASSISTANT_STOP` with `async_listen` instead of `async_listen_once` | Use `hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, handler)` — it fires exactly once, handler runs in event loop, can await executor join |
| Worker thread exception visibility | Assuming unhandled thread exceptions propagate to HA | HA's `bootstrap.py` installs `threading.excepthook` that logs "Uncaught thread exception" — thread dies silently from HA's perspective; implement a watchdog async task that polls `thread.is_alive()` |
| `run_coroutine_threadsafe` during teardown | Calling `.result()` (blocking) after HA enters stopping state | Avoid blocking result waits from worker during teardown; use one-way sentinel in queue to signal stop, then join from async side |
| psycopg3 cursor sharing | Sharing a single cursor between flush and backfill codepaths | Each call site opens its own cursor via `with conn.cursor() as cur`; never share cursor objects |

---

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Pool `min_size > 1` for a single-writer thread | Idle connections consume TimescaleDB connection slots; pool workers do redundant health-checks | Set `min_size=1, max_size=1` for single writer; adjust only if backfill and flush overlap heavily | At any scale; wastes DB resources from day one |
| `pool.wait()` on each reconnect in the worker's hot loop | Adds 100–500 ms latency per flush attempt when DB is flapping | `pool.wait()` once at startup; rely on pool's backoff-retry for reconnections | High-frequency flush intervals (< 5s) combined with flapping DB |
| Unbounded JSON serialisation in `@callback` (event loop thread) | Slow `json.dumps` of large attribute dicts blocks event loop | Profile; if attributes dict is large, serialize in the worker thread instead, enqueue raw `State` objects | State with attributes > ~10 KB |

---

## "Looks Done But Isn't" Checklist

- [ ] **Worker thread start:** `thread.daemon = True` set so Python doesn't hang on interpreter exit if `async_unload_entry` wasn't called — verify daemon flag is set.
- [ ] **EVENT_HOMEASSISTANT_STOP handler:** Confirm it calls `await hass.async_add_executor_job(thread.join, TIMEOUT)` and not `thread.join()` directly (which would block the event loop).
- [ ] **Pool lifecycle:** Confirm pool is opened inside the worker thread's `run()` (or via executor job) and closed via executor job in `async_unload_entry`, never directly from async context.
- [ ] **Queue sentinel on unload:** Confirm `async_unload_entry` enqueues a sentinel value (`None` or `StopTask`) to wake the blocking `queue.get()` in the worker, rather than relying only on `thread.join(timeout=...)`.
- [ ] **Unhandled worker exception path:** Confirm watchdog async task checks `thread.is_alive()` periodically and fires a persistent notification + restarts thread on death — HA only logs "Uncaught thread exception", it does not restart the thread.
- [ ] **Async API from thread audit:** Grep codebase for `hass.async_`, `async_fire`, `async_create_task` in any non-`@callback` / non-`async def` function path that executes in the worker thread. All must be absent or replaced with sync equivalents.
- [ ] **psycopg3-binary wheel availability:** Confirm psycopg-binary has a wheel for the target Python (3.14) and architecture (aarch64 for Raspberry Pi 4/5 HAOS). PyPI confirms cp314 + linux_aarch64 wheels exist as of psycopg-binary 3.3.3.

---

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| async_* called from worker, event loop corrupted | HIGH | HA restart required; add async API audit to CI |
| Shutdown deadlock | MEDIUM | OS-level SIGKILL after STOP_STAGE_SHUTDOWN_TIMEOUT (100s); user restarts HA; fix: add timeout to `.result()` call |
| Worker thread died undetected | LOW | Watchdog fires persistent notification; operator reloads integration or restarts HA; fix: watchdog restart logic |
| Pool stuck on reconnect with no alert | MEDIUM | User notices missing data; trigger repair issue manually; fix: register `reconnect_failed` callback |
| Blocking pool.open() froze event loop once | LOW | HA recovers after timeout; move pool.open() to executor job |

---

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| `hass.async_*` from worker thread | Phase 1: Worker thread scaffolding | Grep for `async_` methods in worker code paths; HA `asyncio_thread_safety` linter |
| `run_coroutine_threadsafe` deadlock | Phase 1: Worker lifecycle / shutdown path | Test HA restart 10× with active DB writes in flight; confirm clean exit < 10s |
| `SimpleQueue` GIL assumption | Phase 1: Worker scaffolding | Code comment; future-proof note in architecture doc |
| Blocking pool ops in event loop | Phase 1: psycopg3 integration | HA blocking-call detector in 2024.5+ logs; integration test with slow DB |
| Shared bare Connection risk | Phase 1: psycopg3 integration | Code review: only `ConnectionPool` instantiated, never bare `psycopg3.connect()` |
| No `reconnect_failed` callback | Phase 2: Observability / error handling | Kill DB container during integration test; verify repair issue appears in HA UI within `reconnect_timeout` seconds |
| Worker death not detected | Phase 2: Watchdog implementation | Kill worker thread artificially in test; verify watchdog fires persistent_notification and restarts thread |

---

## Sources

- [HA Developer Docs: Thread safety with asyncio](https://developers.home-assistant.io/docs/asyncio_thread_safety/) — HIGH confidence
- [HA Developer Docs: Working with async](https://developers.home-assistant.io/docs/asyncio_working_with_async/) — HIGH confidence
- [HA core.py shutdown timeouts](https://github.com/home-assistant/core/blob/dev/homeassistant/core.py) — HIGH confidence (STOPPING=20s, STOP=100s, FINAL_WRITE=60s, CLOSE=30s constants confirmed)
- [HA recorder/core.py — StopTask sentinel, EVENT_HOMEASSISTANT_FINAL_WRITE, async join pattern](https://github.com/home-assistant/core/blob/dev/homeassistant/components/recorder/core.py) — HIGH confidence
- [HA bootstrap.py — threading.excepthook installation](https://github.com/home-assistant/core/blob/dev/homeassistant/bootstrap.py) — HIGH confidence
- [HA PR #45807 — run_callback_threadsafe shutdown deadlock fix](https://github.com/home-assistant/core/pull/45807) — HIGH confidence
- [psycopg3 Connection Pool docs](https://www.psycopg.org/psycopg3/docs/advanced/pool.html) — HIGH confidence
- [psycopg3 Concurrent operations docs](https://www.psycopg.org/psycopg3/docs/advanced/async.html) — HIGH confidence
- [psycopg3 Install / platform support](https://www.psycopg.org/psycopg3/docs/basic/install.html) — HIGH confidence
- [psycopg-binary PyPI — cp314 + linux_aarch64 wheels confirmed in 3.3.3](https://pypi.org/project/psycopg-binary/) — HIGH confidence
- [CPython issue #113884 — SimpleQueue not thread-safe without GIL](https://github.com/python/cpython/issues/113884) — HIGH confidence
- [Python docs — queue.SimpleQueue thread safety](https://docs.python.org/3/library/queue.html) — HIGH confidence
- [Python docs — threading.excepthook](https://docs.python.org/3/library/threading.html) — HIGH confidence
- [HA 2026.3 release — Python 3.14 upgrade confirmed](https://www.home-assistant.io/blog/2026/03/04/release-20263/) — HIGH confidence
- [HA architecture discussion #1230 — armv7 dropped, aarch64 only on Raspberry Pi](https://github.com/home-assistant/architecture/discussions/1230) — HIGH confidence
- [HA issue #145141 — "Uncaught thread exception" log pattern](https://github.com/home-assistant/core/issues/145141) — MEDIUM confidence (confirms logging behaviour, not a spec)
- [psycopg issue #438 — reconnect_failed callback semantics](https://github.com/psycopg/psycopg/issues/438) — MEDIUM confidence

---
*Pitfalls research for: HA custom component — threaded worker + psycopg3 ingestion*
*Researched: 2026-04-19*
