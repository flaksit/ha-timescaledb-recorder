---
phase: 260427-lin
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - custom_components/timescaledb_recorder/persistent_queue.py
  - custom_components/timescaledb_recorder/__init__.py
  - custom_components/timescaledb_recorder/manifest.json
  - tests/test_persistent_queue.py
autonomous: true
requirements:
  - GH-ISSUE-11
must_haves:
  truths:
    - "Initial registry backfill performs ONE fsync regardless of registry entry count (batched write)."
    - "Consumer.get() skips and warns on a malformed JSONL line instead of crashing the meta worker."
    - "Orchestrator and meta_init both spawn after HA is fully started (state == running), regardless of fresh boot vs reload, with no `is_running`/`async_listen_once` split."
    - "State events that arrive between `setup_entry` return and HA `started` are buffered (state_listener still registers at t=0; live_queue accepts events; orchestrator drains the gap on its first cycle)."
    - "HA loads the integration's `setup_entry` after recorder's `setup_entry` (manifest `after_dependencies`)."
  artifacts:
    - path: "custom_components/timescaledb_recorder/persistent_queue.py"
      provides: "PersistentQueue with put_many / put_many_async batched writers; tolerant get() that skips malformed lines."
    - path: "custom_components/timescaledb_recorder/__init__.py"
      provides: "_async_initial_registry_backfill collects items into one list and calls put_many_async once; setup_entry uses async_at_started for orchestrator and meta_init."
    - path: "custom_components/timescaledb_recorder/manifest.json"
      provides: "after_dependencies: [recorder]; bumped version."
    - path: "tests/test_persistent_queue.py"
      provides: "Coverage for put_many, put_many_async, malformed-line tolerance."
  key_links:
    - from: "_async_initial_registry_backfill"
      to: "PersistentQueue.put_many_async"
      via: "single await with the full ordered items list"
      pattern: "put_many_async\\("
    - from: "async_setup_entry"
      to: "homeassistant.helpers.start.async_at_started"
      via: "two registrations (orchestrator spawn, meta_init spawn)"
      pattern: "async_at_started\\("
    - from: "manifest.json"
      to: "recorder integration setup ordering"
      via: "after_dependencies key"
      pattern: "after_dependencies"
---

<objective>
Resolve GitHub issue #11 (HA startup delay caused by per-entry fsync during registry
backfill and by orchestrator / meta_init competing with HA bootstrap).

Five concrete changes, scoped to a single plan because they share files and form one
behavioral fix:

1. Add `PersistentQueue.put_many` / `put_many_async` for batched single-fsync writes.
2. Make `PersistentQueue.get()` tolerant of malformed JSON lines (crash recovery).
3. Use `put_many_async` once in `_async_initial_registry_backfill`.
4. Replace `is_running` / `async_listen_once(EVENT_HOMEASSISTANT_STARTED)` for both
   orchestrator spawn and `_async_meta_init` spawn with
   `homeassistant.helpers.start.async_at_started`.
5. Add `"after_dependencies": ["recorder"]` and bump the manifest version.

Purpose: HA bootstrap is no longer slowed by N synchronous fsyncs (often hundreds of
registry entries) and by background work that should run after `started`. A
crash-truncated JSONL queue no longer poisons the meta worker on the next boot.

Output: One commit-ready set of edits across the four files above, with tests
covering the new queue methods and the malformed-line tolerance.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@CLAUDE.md
@.planning/STATE.md
@custom_components/timescaledb_recorder/persistent_queue.py
@custom_components/timescaledb_recorder/__init__.py
@custom_components/timescaledb_recorder/manifest.json
@tests/test_persistent_queue.py

<interfaces>
PersistentQueue (current public surface, persistent_queue.py):
- `put(item: dict) -> None`                         — sync, fsync per call
- `async put_async(item: dict) -> None`             — runs `put` in default executor
- `get() -> dict | None`                            — blocking, returns front item without removal
- `task_done() -> None`                             — atomically rewrites file without front line
- `async join() -> None` / `_join_blocking()`       — blocks until empty + nothing in-flight
- `wake_consumer() -> None`                         — notify_all for shutdown unblock
- Internal helper: `_read_first_line_locked()`      — reads first line under lock

Consumer contract (do NOT change): `get() -> process -> task_done()`. `_in_flight` keeps
the front line "checked out" until `task_done()` rewrites the file. Crash safety relies
on the front line still being on disk after a crash between `get()` and `task_done()`.

HA helper to use (Home Assistant 2026.3.4):
- `homeassistant.helpers.start.async_at_started(hass, callback) -> CALLBACK_TYPE`
  Fires `callback(hass)` once when state becomes RUNNING, OR immediately if already
  running. Callback may be a sync `@callback` function or a coroutine function.
  Return value is an unsubscribe handle; we do not need to track it here because the
  callback fires at most once and is safe to ignore.

Existing call sites the executor will modify in `__init__.py`:
- `_async_initial_registry_backfill` (around line 181): four `await meta_queue.put_async(...)`
  calls inside four loops; preserves area → label → entity → device order.
- `_async_meta_init` (around line 365): currently spawned via
  `hass.async_create_background_task(_async_meta_init(...))` immediately in setup_entry
  (around line 468). Must move into an `async_at_started` callback.
- `_spawn_orchestrator` closure (around line 504): currently dispatched via
  `if hass.is_running: _spawn_orchestrator() else: bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, ...)`
  (around lines 533–539). Must move into an `async_at_started` callback.
- `EVENT_HOMEASSISTANT_STARTED` import on line 34 becomes unused after this change —
  remove it.
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Add put_many / put_many_async and make get() tolerant of malformed lines</name>
  <files>
    custom_components/timescaledb_recorder/persistent_queue.py,
    tests/test_persistent_queue.py
  </files>
  <behavior>
    - put_many([{a:1},{a:2},{a:3}]) writes exactly three JSONL lines, performs ONE fsync,
      and notifies waiting consumers once. After the call, get() returns {a:1}, then after
      task_done() the next get() returns {a:2}, etc. (FIFO preserved).
    - put_many([]) is a no-op: file unchanged, no fsync, no notify_all.
    - put_many_async([...]) runs put_many in the default executor (parity with put_async).
    - Tolerant get(): if the front line on disk is invalid JSON, get() logs a WARNING,
      skips that line (rewrites the file without it, same atomic temp+replace pattern as
      task_done would use, but performed BEFORE assigning _in_flight), and proceeds to the
      next line. _in_flight is only ever assigned to a successfully parsed dict.
    - Crash-recovery test: simulate a power-loss tail by appending a partial trailing line
      (`'{"k":'`) after a normal line. get() returns the good item; the partial line is
      eventually skipped with a warning when it reaches the front. (Test by calling
      task_done() then get() again so the malformed line is at the front.)
  </behavior>
  <action>
    persistent_queue.py:

    1. Add `put_many(self, items: list[dict]) -> None`:
       - If `items` is empty, return immediately (no file open, no fsync, no notify).
       - Build the payload as a single string by joining `json.dumps(item, default=str) + "\n"`
         for each item, in order.
       - Inside `with self._cond:`, open the file in `"a"` mode once, write the entire
         payload in a single `f.write(...)`, then `f.flush()` and `os.fsync(f.fileno())`
         exactly once before exiting the `with open` block.
       - After closing the file, call `self._cond.notify_all()` exactly once.
       - Note: this preserves the existing concurrency model — one lock acquisition,
         one fsync, one notify, regardless of len(items).

    2. Add `async put_many_async(self, items: list[dict]) -> None` that mirrors
       `put_async`: `loop.run_in_executor(None, self.put_many, items)`. Early-return
       (without scheduling executor work) when `items` is empty.

    3. Tolerant `get()`: rework the parse step so that when `_read_first_line_locked()`
       returns a non-None string, the parse is wrapped in try/except `json.JSONDecodeError`.
       On failure:
         - Log a WARNING via the module `_LOGGER` that includes the queue path and a
           truncated repr of the offending line (cap to ~120 chars to avoid log spam).
         - Drop the offending line from disk using the same tempfile + os.replace pattern
           used by `task_done()` (read all remaining lines after the first, write to a
           `.tmp`, fsync, `os.replace`). Do NOT touch `_in_flight` (still `_NO_IN_FLIGHT`).
         - Continue the outer `while True:` loop so the next iteration re-reads the new
           front line (which is the previously second line) and tries again.
       On success: assign `self._in_flight = parsed` and return it as today.

       The malformed-line drop must happen while still holding `self._cond` (i.e. the
       existing `with self._cond:` context); do not release and re-acquire the lock.

       Comment requirement: above the except branch, add a short comment explaining that
       a partial trailing line can occur after OS / power crash mid-fsync, and that the
       earlier write+fsync model means malformed content is bounded to at most one
       contiguous tail; we still tolerate it at any position defensively. (Per CLAUDE.md:
       document non-obvious decisions.)

    4. Refactor: extract the rewrite-without-front-line logic into a tiny private helper
       `_rewrite_without_front_line_locked(self) -> None` if doing so keeps `task_done()`
       and the new tolerant-get drop path readable; otherwise inline both. Executor's
       judgment — but DO NOT duplicate the tempfile + fsync + os.replace block in two
       places verbatim.

    tests/test_persistent_queue.py — add the following tests (mirror the existing
    file's style; reuse its fixtures / tmp_path patterns):

    - `test_put_many_writes_all_items_and_preserves_order`
    - `test_put_many_empty_is_noop` (file does not exist OR is unchanged after call;
      no exception)
    - `test_put_many_single_fsync` (assert by patching `os.fsync` with a mock and
      checking call_count == 1 after a put_many of 5 items; cf. how the existing tests
      construct PersistentQueue at tmp_path)
    - `async test_put_many_async_drains_via_get` (await put_many_async([...]); assert
      get() / task_done() loop returns items in order; await q.join() returns)
    - `test_get_skips_malformed_front_line` (write a file with an invalid JSON first
      line followed by a valid line; assert get() returns the valid item and that a
      WARNING was logged via `caplog`; subsequent task_done() leaves the file empty)
    - `test_get_recovers_from_partial_trailing_line` (put a valid item; manually append
      `'{"k":'` to the file to simulate a torn write; consume the valid item via
      get/task_done; the next get() returns None after warning + drop, OR if you choose
      to also enqueue another valid item afterwards, returns that valid item)
  </action>
  <verify>
    <automated>uv run pytest tests/test_persistent_queue.py -x -q</automated>
  </verify>
  <done>
    put_many / put_many_async exist on PersistentQueue with the contract above; get()
    no longer raises on malformed lines; all new tests pass and existing tests in
    test_persistent_queue.py still pass.
  </done>
</task>

<task type="auto">
  <name>Task 2: Batch the initial registry backfill into one put_many_async call</name>
  <files>custom_components/timescaledb_recorder/__init__.py</files>
  <action>
    Modify `_async_initial_registry_backfill` (currently around line 181):

    - Build a single local `items: list[dict] = []`.
    - Keep the existing iteration order: areas first, then labels, then entities, then
      devices. Append each constructed dict (with the same keys: `registry`, `action`,
      `registry_id`, `old_id`, `params`, `enqueued_at`) to `items` instead of calling
      `await meta_queue.put_async(...)` inside the loop.
    - After all four loops, perform exactly ONE `await meta_queue.put_many_async(items)`.
    - Preserve the existing `_to_json_safe(params)` call and the `enqueued_at = now_iso`
      assignment. Do not change the dict shape.
    - Update / extend the function docstring's ordering note to make explicit that the
      batched write performs a single fsync (the reason for batching: HA startup delay,
      issue #11). Per CLAUDE.md, document the why, not the what.
    - Leave the throwaway `RegistryListener(...)` helper instance and its
      `_extract_*_params` calls unchanged — the optimization is the batched write,
      not the extraction.

    No call-site changes outside this function are required for this task.
  </action>
  <verify>
    <automated>uv run pytest tests/test_init.py -x -q</automated>
  </verify>
  <done>
    `_async_initial_registry_backfill` performs exactly one `put_many_async` call and
    no `put_async` calls; entity ordering area → label → entity → device is preserved
    in the items list; tests for setup / meta_init still pass.
  </done>
</task>

<task type="auto">
  <name>Task 3: Defer orchestrator and meta_init via async_at_started; add manifest after_dependencies</name>
  <files>
    custom_components/timescaledb_recorder/__init__.py,
    custom_components/timescaledb_recorder/manifest.json
  </files>
  <action>
    __init__.py:

    1. Imports:
       - Remove `from homeassistant.const import EVENT_HOMEASSISTANT_STARTED` (it
         becomes unused — verify nothing else in the module references it).
       - Add `from homeassistant.helpers.start import async_at_started`.

    2. In `async_setup_entry`, replace the meta_init dispatch (currently
       `hass.async_create_background_task(_async_meta_init(...), name=...)` ~line 468)
       with an `async_at_started` registration:

       Define a `@callback` named `_start_meta_init(hass)` (sync) that calls
       `hass.async_create_background_task(_async_meta_init(hass, meta_queue, registry_listener),
       name="timescaledb_recorder_meta_init")`.

       Then call `async_at_started(hass, _start_meta_init)`.

       Rationale comment (CLAUDE.md: document non-obvious decisions): this defers the
       persisted-queue drain + registry snapshot until HA reaches RUNNING, fixing the
       startup-delay symptom in issue #11. `async_at_started` also handles the reload
       case (state already RUNNING) by firing the callback immediately.

       The `state_listener.async_start()` and `registry_listener.async_start()` calls
       MUST stay where they are (before this point in setup_entry) — they need to be
       registered at t=0 so events arriving between setup and `started` are buffered
       in `live_queue` (state) or discarded under DISCARD mode (registry, until
       `_async_meta_init` flips it to LIVE).

    3. Replace the orchestrator dispatch block (currently `if hass.is_running:
       _spawn_orchestrator() else: hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, ...)`,
       lines ~533–539) with a single `async_at_started(hass, _start_orchestrator)` call,
       where `_start_orchestrator(hass)` is a `@callback` that invokes the existing
       `_spawn_orchestrator()` closure.

       Keep `_spawn_orchestrator()` and `_make_orchestrator_done_callback(...)` exactly
       as they are — they already build kwargs from `data` and attach the done-callback
       for crash-relaunch (D-04).

    4. Do NOT move `_wait_for_recorder_and_clear`, `_overflow_watcher`, or `watchdog_loop`
       under `async_at_started`. Leave their immediate spawn behavior intact (per task
       scope: only orchestrator and meta_init move).

    5. Verify both new callbacks tolerate being called when HA is already RUNNING (reload
       path): `async_at_started` invokes the callback synchronously in that case, so the
       callback must not await — that is why both are `@callback`-decorated sync wrappers
       that schedule the actual coroutine via `async_create_background_task`. Add a
       short comment to that effect on each callback.

    manifest.json:

    1. Add the key `"after_dependencies": ["recorder"]`. JSON key ordering note: place
       it next to `"requirements"` for grouping (per existing manifest conventions in
       this repo — keep the file readable; no strict order requirement from HA).
    2. Bump `"version"`. The current value in the file is `"2.0.0"` (NOT `1.2.11` as the
       task scope text said — trust the file). Bump to `"2.0.1"` for this patch.
       If a different version scheme is in flight (executor: `git log -- manifest.json`
       to confirm), follow that scheme; otherwise `2.0.0` → `2.0.1`.

    Behavior expectations the executor MUST preserve (do not regress):
    - state_listener registers and buffers into live_queue from t=0 (not deferred).
    - registry_listener registers in DISCARD mode at t=0 and is flipped to LIVE inside
      `_async_meta_init` (already true today; do not change `_async_meta_init`).
    - On reload (HA already RUNNING), both orchestrator and meta_init still launch
      (handled automatically by `async_at_started`).
  </action>
  <verify>
    <automated>uv run pytest tests/test_init.py tests/test_backfill.py -x -q</automated>
  </verify>
  <done>
    Both orchestrator spawn and meta_init spawn flow through `async_at_started`; no
    `is_running`/`async_listen_once(EVENT_HOMEASSISTANT_STARTED, ...)` references remain
    in the module; `EVENT_HOMEASSISTANT_STARTED` import is removed; manifest.json has
    `"after_dependencies": ["recorder"]` and a bumped version; relevant tests pass.
  </done>
</task>

<task type="auto">
  <name>Task 4: Repository-wide sanity checks and final test run</name>
  <files>
    custom_components/timescaledb_recorder/__init__.py,
    custom_components/timescaledb_recorder/persistent_queue.py,
    custom_components/timescaledb_recorder/manifest.json
  </files>
  <action>
    Final pass — no new behavior, only verification:

    1. Grep for stragglers and confirm none remain in the production code:
       - `grep -n "EVENT_HOMEASSISTANT_STARTED" custom_components/timescaledb_recorder/`
         (expected: no hits, or only inside comments — if a comment exists, rewrite it
         to reference `async_at_started` instead).
       - `grep -n "hass.is_running" custom_components/timescaledb_recorder/`
         (expected: no hits in production code paths added/touched here).
       - `grep -n "put_async" custom_components/timescaledb_recorder/__init__.py`
         (expected: no hits inside `_async_initial_registry_backfill`).

    2. Run the full project test suite to catch unrelated regressions surfaced by the
       __init__.py edits:

         uv run pytest -x -q

    3. Validate manifest.json is still valid JSON and HA-loadable:
         uv run python -c "import json,sys; json.load(open('custom_components/timescaledb_recorder/manifest.json'))"

    4. If any test fails, fix it in the relevant production file rather than weakening
       the test — per CLAUDE.md, ask before substantially deviating from the plan.
  </action>
  <verify>
    <automated>uv run pytest -x -q</automated>
  </verify>
  <done>
    All tests pass; greps return clean; manifest.json parses as valid JSON.
  </done>
</task>

</tasks>

<verification>
- `uv run pytest tests/test_persistent_queue.py tests/test_init.py tests/test_backfill.py -x -q` passes.
- `uv run pytest -x -q` (full suite) passes.
- Manual trace of `async_setup_entry`: state_listener subscribes at t=0; orchestrator
  and meta_init defer to RUNNING via `async_at_started`; on reload, both fire
  immediately.
- `git diff custom_components/timescaledb_recorder/manifest.json` shows
  `after_dependencies` added and `version` bumped.
</verification>

<success_criteria>
- HA startup is no longer blocked by N synchronous fsyncs during initial registry
  backfill (single fsync via `put_many_async`).
- Orchestrator and `_async_meta_init` defer reliably until HA reaches RUNNING, on
  both fresh boot and reload, with no `EVENT_HOMEASSISTANT_STARTED`/`is_running`
  split logic.
- Meta worker survives a torn JSONL tail from a prior crash: malformed lines are
  warned and skipped instead of crashing `get()`.
- Manifest declares `after_dependencies: ["recorder"]`, ordering setup_entry after
  recorder's setup_entry.
- All existing tests pass; new tests cover put_many, put_many_async, and tolerant
  get behavior.
</success_criteria>

<output>
After completion, create `.planning/quick/260427-lin-fix-11-ha-startup-delay-batch-registry-b/260427-lin-SUMMARY.md`.
</output>
