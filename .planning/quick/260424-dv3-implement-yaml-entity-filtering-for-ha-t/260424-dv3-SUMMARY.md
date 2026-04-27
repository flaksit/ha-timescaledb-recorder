---
phase: quick-260424-dv3
plan: "01"
subsystem: ingestion-filter
tags: [entity-filter, yaml-config, voluptuous, async_setup]
dependency_graph:
  requires: []
  provides: [yaml-entity-filtering]
  affects: [state_listener, backfill_orchestrator]
tech_stack:
  added: [voluptuous CONFIG_SCHEMA, cv.string validators]
  patterns: [hass.data domain store, async_setup HA startup hook]
key_files:
  created: []
  modified:
    - custom_components/timescaledb_recorder/__init__.py
    - README.md
decisions:
  - "Read filter from hass.data (set by async_setup) rather than entry.data — entry.data never carries a filter key"
  - "ALLOW_EXTRA on CONFIG_SCHEMA top-level so other domains pass HA's config validation unchanged"
  - "FILTER_SCHEMA_INNER uses vol.Optional with default=[] so convert_include_exclude_filter always receives well-formed dicts"
metrics:
  duration: "8 minutes"
  completed: "2026-04-24"
---

# Quick Task 260424-dv3: Implement YAML Entity Filtering Summary

YAML entity filtering wired end-to-end: `configuration.yaml` include/exclude config validated by voluptuous, stored in `hass.data` by `async_setup`, and consumed by `_get_entity_filter` before `StateListener` and the backfill orchestrator are constructed.

## Tasks Completed

| Task | Description | Commit |
|------|-------------|--------|
| 1 | Add CONFIG_SCHEMA, async_setup, update _get_entity_filter | 60f4d7b |
| 2 | Replace README Entity Filtering placeholder with YAML docs | 2038b92 |

## What Was Built

### Task 1 — `__init__.py`

- `_FILTER_SCHEMA_INNER`: voluptuous schema for one include/exclude block with `domains`, `entities`, `entity_globs` keys, each validated as `[cv.string]` with empty-list defaults.
- `CONFIG_SCHEMA`: top-level schema wrapping `_FILTER_SCHEMA_INNER` under the `DOMAIN` key with `extra=vol.ALLOW_EXTRA` so other HA integration configs pass through unchanged.
- `async_setup(hass, config)`: stores `config.get(DOMAIN, {})` in `hass.data[DOMAIN]["filter"]` before any config entry loads. Returns `True`.
- `_get_entity_filter(hass, entry)`: reads `hass.data.get(DOMAIN, {}).get("filter", {})`. Non-empty → `convert_include_exclude_filter(raw_filter)`. Empty → `convert_filter({...})` pass-all (unchanged fallback).
- Call site in `async_setup_entry` updated: `_get_entity_filter(hass, entry)`.

### Task 2 — `README.md`

Replaced the "not yet implemented" placeholder under "Entity Filtering" with:
- YAML example block with full `include`/`exclude` structure
- Bullet list documenting each sub-key's semantics
- Include-only = allow-list, exclude-only = deny-list, both = HA recorder precedence
- Restart-required note with explanation
- Link to HA recorder filter documentation

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None. The filter is fully wired; pass-all is the correct behaviour when no YAML config is present.

## Threat Surface Scan

No new network endpoints, auth paths, or file access patterns introduced. All user input (YAML filter values) flows through voluptuous `cv.string` validators before reaching `convert_include_exclude_filter`. T-dv3-01 mitigation applied as designed.

## Self-Check: PASSED

- `custom_components/timescaledb_recorder/__init__.py` — modified and committed at 60f4d7b
- `README.md` — modified and committed at 2038b92
- `uv run python` import check: `CONFIG_SCHEMA`, `async_setup`, `async_setup_entry` all importable
- `grep "not yet implemented" README.md` — 0 matches
- `_get_entity_filter` signature verified: `(hass, entry)`
