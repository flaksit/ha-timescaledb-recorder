---
quick_id: 260424-l5k
status: complete
date: 2026-04-24
commit: 1b59297
---

# Quick Task 260424-l5k: Fix backfill entity set for non-registry entities

## What was done

**backfill.py** (lines 162-178): Added `state_machine_entities` from
`hass.states.async_all()` as a third source for the entity set. Updated
comment (D-08-f) to document the three sources. The union is now:

    entities = live_entities | state_machine_entities | open_entities

**tests/test_backfill.py**: Added `test_orchestrator_includes_non_registry_entities`
verifying that an entity present in `hass.states` but absent from `entity_reg`
(e.g. `sun.sun`) appears in the entity set passed to `_fetch_slice_raw`. All 13
tests pass.

## Result

`sun.sun`, `zone.home`, `conversation.*`, and any other entity without a `unique_id`
are now included in every backfill cycle's entity set. Startup states for these
entities are no longer silently skipped.
