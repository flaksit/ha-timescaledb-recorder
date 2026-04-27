---
quick_id: 260424-l5k
status: complete
date: 2026-04-24
commit: 566f8e6
---

# Quick Task 260424-l5k: Fix backfill entity set for non-registry entities

## What was done

**backfill.py**: Replaced three-source entity set (entity_reg + hass.states +
open_entities) with a cleaner two-source design:

- `all_db_entities` from `SELECT_ALL_KNOWN_ENTITIES_SQL` (DB query via executor)
- `state_machine_entities` from `hass.states.async_all()` (first-run supplement)

`SELECT_ALL_KNOWN_ENTITIES_SQL`:

    SELECT entity_id FROM entities
    UNION
    SELECT entity_id FROM states GROUP BY entity_id

`entities` (no `valid_to` filter) covers all registry entities ever seen,
including removed ones. `states GROUP BY` covers non-registry entities written
by prior cycles. `GROUP BY` forces index use before the UNION dedup step —
plain `UNION` caused a full hypertable scan (~27 s).

Dropped `live_entities` / `entity_reg` entirely — redundant given `entities`.
Renamed `open_entities_reader` → `all_entities_reader` throughout.
Used decorator syntax for `fetch_slice` retry wrap.

**states_worker.py**: `read_open_entities` → `read_all_known_entities`, new SQL.

**const.py**: `SELECT_OPEN_ENTITIES_SQL` → `SELECT_ALL_KNOWN_ENTITIES_SQL`.

**__init__.py**: Updated call site and stale comment.

**tests/test_backfill.py**: Removed `er.async_get` patches, updated entity setup
to use `all_entities_reader`. All 13 tests pass.

## Result

All entities — registry (including removed), non-registry (sun.sun, zone.home,
conversation.*), and entities removed during an outage — are now included in
every backfill cycle's entity set. Closes #8.
