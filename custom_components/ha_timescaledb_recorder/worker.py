"""StateRow payload dataclass — the only surviving Phase 1 export.

DbWorker and MetaCommand were retired in Phase 2 per D-15. States are handled
by states_worker.TimescaledbStateRecorderThread; metadata is handled by
meta_worker.TimescaledbMetaRecorderThread receiving plain dicts from
PersistentQueue.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class StateRow:
    """Immutable state change payload enqueued from the HA event loop.

    frozen=True: safe to read from the worker thread after enqueue.
    slots=True: reduces memory for high-volume state events.
    attributes is a plain dict; Jsonb() wrapping happens in the worker
    _insert_chunk call site (see states_worker.py).
    """

    entity_id: str
    state: str
    attributes: dict
    last_updated: datetime
    last_changed: datetime

    @classmethod
    def from_ha_state(cls, state: Any) -> "StateRow":
        """Build a StateRow from a Home Assistant `State` (or compatible) object.

        Used by the backfill slice-to-rows transform (D-04-d) where the
        orchestrator hands the worker a dict[entity_id, list[HA State]]. HA
        State attributes may be a `ReadOnlyDict` / `MappingProxyType`; we
        copy into a plain dict to break the reference and allow mutation by
        downstream consumers (e.g., Jsonb wrapping).

        Expected attributes on `state`: entity_id, state, attributes,
        last_updated, last_changed (all datetime-aware).
        """
        return cls(
            entity_id=state.entity_id,
            state=state.state,
            attributes=dict(state.attributes),
            last_updated=state.last_updated,
            last_changed=state.last_changed,
        )
