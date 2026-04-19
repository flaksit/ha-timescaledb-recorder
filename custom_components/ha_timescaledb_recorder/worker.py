"""DbWorker: dedicated OS thread that owns all psycopg3 DB writes for ha_timescaledb_recorder."""
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import psycopg
import psycopg.rows
from psycopg.types.json import Jsonb

from homeassistant.core import HomeAssistant

from .const import (
    INSERT_SQL,
    SCD2_CLOSE_ENTITY_SQL, SCD2_CLOSE_DEVICE_SQL,
    SCD2_CLOSE_AREA_SQL, SCD2_CLOSE_LABEL_SQL,
    SCD2_SNAPSHOT_ENTITY_SQL, SCD2_SNAPSHOT_DEVICE_SQL,
    SCD2_SNAPSHOT_AREA_SQL, SCD2_SNAPSHOT_LABEL_SQL,
    SCD2_INSERT_ENTITY_SQL, SCD2_INSERT_DEVICE_SQL,
    SCD2_INSERT_AREA_SQL, SCD2_INSERT_LABEL_SQL,
    SELECT_ENTITY_CURRENT_SQL, SELECT_DEVICE_CURRENT_SQL,
    SELECT_AREA_CURRENT_SQL, SELECT_LABEL_CURRENT_SQL,
)
from .schema import sync_setup_schema

if TYPE_CHECKING:
    from .syncer import MetadataSyncer

_LOGGER = logging.getLogger(__name__)

# Singleton sentinel enqueued by stop() to signal the worker thread to exit cleanly.
# Identity-checked with `is` — never compared by value.
_STOP = object()


@dataclass(frozen=True, slots=True)
class StateRow:
    """Immutable state change payload enqueued from the HA event loop.

    frozen=True: immutable after enqueue — safe to read from worker thread.
    slots=True: reduces per-object memory for high-volume state events (Python 3.10+).
    attributes is kept as a plain dict here; Jsonb() wrapping happens in DbWorker._flush()
    to keep the @callback enqueue site simple and allocation-free.
    """

    entity_id: str
    state: str
    attributes: dict
    last_updated: datetime
    last_changed: datetime


@dataclass(frozen=True, slots=True)
class MetaCommand:
    """Immutable registry change payload enqueued from the HA event loop.

    registry: one of "entity", "device", "area", "label"
    action:   one of "create", "update", "remove"
    registry_id: entity_id / device_id / area_id / label_id (the primary key)
    old_id:   only set on entity renames (old_entity_id); None otherwise
    params:   pre-extracted positional tuple for the SCD2 SQL; None on "remove" actions
              (registry entry is already gone at remove time — D-08)
    """

    registry: str
    action: str
    registry_id: str
    old_id: str | None
    params: tuple | None
