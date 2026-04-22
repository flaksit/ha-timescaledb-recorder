"""Unit tests for StateRow (D-15: only surviving Phase 1 export).

DbWorker and MetaCommand were retired in Phase 2 per D-15. This file tests
StateRow exclusively: field schema, immutability, and the from_ha_state factory.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.ha_timescaledb_recorder.worker import StateRow


def test_state_row_is_frozen():
    """StateRow must be immutable — frozen=True ensures thread-safe enqueue."""
    t = datetime.now(timezone.utc)
    r = StateRow(entity_id="x.y", state="on", attributes={"a": 1},
                 last_updated=t, last_changed=t)
    with pytest.raises((AttributeError, Exception)):
        r.state = "off"


def test_state_row_slots_saves_memory():
    """StateRow must use __slots__ — slots=True rejects new attributes.

    Python 3.14 raises TypeError instead of AttributeError for slot violations
    on frozen dataclasses; accept both to stay version-agnostic.
    """
    t = datetime.now(timezone.utc)
    r = StateRow(entity_id="x.y", state="on", attributes={},
                 last_updated=t, last_changed=t)
    with pytest.raises((AttributeError, TypeError)):
        r.unknown_attr = 123  # slots=True rejects new attrs


def test_from_ha_state_copies_attributes_dict():
    """from_ha_state must copy attributes — mutating the result must not affect the source."""
    t = datetime.now(timezone.utc)
    src_attrs = {"unit": "°C"}
    ha_state = MagicMock(
        entity_id="sensor.x", state="22",
        attributes=src_attrs, last_updated=t, last_changed=t,
    )
    row = StateRow.from_ha_state(ha_state)
    assert row.entity_id == "sensor.x"
    assert row.state == "22"
    assert row.attributes == src_attrs
    # Mutating the copy must NOT affect the source
    row.attributes["injected"] = 1
    assert "injected" not in src_attrs


def test_state_row_does_not_export_dbworker_or_metacommand():
    """worker.py must not expose DbWorker, MetaCommand, or _STOP in Phase 2 (D-15)."""
    import custom_components.ha_timescaledb_recorder.worker as w
    assert not hasattr(w, "DbWorker")
    assert not hasattr(w, "MetaCommand")
    assert not hasattr(w, "_STOP")


def test_state_row_fields_round_trip():
    """All StateRow fields must be exactly what was passed in."""
    t_updated = datetime(2026, 4, 21, 10, 0, 0, tzinfo=timezone.utc)
    t_changed = datetime(2026, 4, 21, 10, 0, 5, tzinfo=timezone.utc)
    r = StateRow(
        entity_id="sensor.temp",
        state="21.5",
        attributes={"unit": "°C"},
        last_updated=t_updated,
        last_changed=t_changed,
    )
    assert r.entity_id == "sensor.temp"
    assert r.state == "21.5"
    assert r.attributes == {"unit": "°C"}
    assert r.last_updated == t_updated
    assert r.last_changed == t_changed
