"""Tests for the YAML section schema (entity_filter.DOMAIN_SCHEMA)."""
import probatio
import pytest

from custom_components.timescaledb_recorder.entity_filter import (
    DOMAIN_SCHEMA,
    build_entity_filter,
)


def test_minute_rollup_holds_become_whole_seconds():
    conf = DOMAIN_SCHEMA({"minute_rollup": [
        {"entity_id": "sensor.plug", "hold": 120, "zero_hold": "720:00:00"},
        {"entity_id": "sensor.meter", "hold": {"minutes": 5}},
    ]})
    assert conf["minute_rollup"] == [
        {"entity_id": "sensor.plug", "hold": 120, "zero_hold": 2592000},
        {"entity_id": "sensor.meter", "hold": 300},
    ]


@pytest.mark.parametrize("entries", [
    [{"entity_id": "sensor.a", "hold": 0}],
    [{"entity_id": "sensor.a"}],
    [{"entity_id": "not an entity", "hold": 60}],
    [{"entity_id": "sensor.a", "hold": 60}, {"entity_id": "sensor.a", "hold": 30}],
])
def test_minute_rollup_rejects_bad_entries(entries):
    with pytest.raises(probatio.Invalid):
        DOMAIN_SCHEMA({"minute_rollup": entries})


def test_rollup_alone_leaves_the_filter_pass_all():
    conf = DOMAIN_SCHEMA({"minute_rollup": [{"entity_id": "sensor.a", "hold": 60}]})
    assert build_entity_filter(conf)("light.anything")
