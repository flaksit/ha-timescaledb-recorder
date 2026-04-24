# ha-timescaledb-recorder

Custom Home Assistant integration — writes HA state changes and registry metadata to TimescaleDB. v1.1 milestone: robust ingestion via thread-worker isolation, psycopg3, bounded buffer, HA sqlite backfill.

## GSD Workflow

This project uses GSD for AI-assisted development.

Planning artifacts live in `.planning/`. Read before executing any phase:
- `.planning/PROJECT.md` — project context and requirements
- `.planning/ROADMAP.md` — phase structure and success criteria
- `.planning/REQUIREMENTS.md` — all v1 requirements with REQ-IDs
- `.planning/research/SUMMARY.md` — key research findings (critical API constraints)
- `.planning/STATE.md` — current phase state

**Phase commands:**
- `/gsd-discuss-phase N` — gather context before planning
- `/gsd-plan-phase N` — create execution plan
- `/gsd-execute-phase N` — execute the plan
- `/gsd-verify-work` — verify phase deliverables

**Always `/clear` between phases** to avoid context explosion.

## Critical Research Findings

Read `.planning/research/SUMMARY.md` before planning any phase. Key constraints:

- Backfill uses `get_significant_states(significant_changes_only=False, include_start_time_state=False)` — accepts a list of entity_ids and returns ALL state rows by `last_updated_ts`. Do NOT use `state_changes_during_period`: it filters out restart-restored states where `last_changed_ts != last_updated_ts` (value unchanged across restart), causing persistent gaps for `sun.sun`, `person.*`, `zone.home`, `conversation.*` etc.
- `hass.async_*` methods are **not thread-safe** from worker thread — use `hass.add_job` (fire-and-forget) or `run_coroutine_threadsafe` (keyword APIs).
- `run_coroutine_threadsafe(...).result()` deadlocks on HA shutdown — use sentinel + `async_add_executor_job(thread.join)`.
- psycopg3: single `psycopg.connect()` owned by worker thread; wrap dicts in `Jsonb()` for JSONB columns.
- `strings.json` with `"issues"` section required for `issue_registry` repair issues.
- TimescaleDB ≥ 2.18.1 required for safe `ON CONFLICT DO NOTHING` (≤ 2.17.2 bug aborts full batch).

## Tech Stack

- Python 3.14+, uv, direnv
- HA 2026.3.4 custom component (HACS)
- TimescaleDB via psycopg[binary]==3.3.3 (v1.1) — replacing asyncpg 0.31.0
- voluptuous (config validation), attrs (registry serialization)
- pytest + pytest-asyncio + pytest-homeassistant-custom-component

## Project Conventions

- All SQL in `const.py` — no inline SQL in business logic
- Async public methods: `async_*` prefix
- Sync private HA callbacks: `_handle_*` with `@callback` decorator
- Test files mirror production modules: `ingester.py` → `test_ingester.py`
- New runtime deps declared in `manifest.json` requirements
