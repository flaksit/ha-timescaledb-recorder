"""YAML schema of the integration's section, and the entity filter built from it.

Kept free of integration imports so backfill_gaps.py, which runs as a plain
script inside the HA container, applies exactly the filter the integration does.
"""
from datetime import timedelta
from typing import Any

import probatio
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entityfilter import (
    EntityFilter,
    convert_filter,
    convert_include_exclude_filter,
)

# Defaults make every key present, so convert_include_exclude_filter always
# receives well-formed dicts.
FILTER_SCHEMA_INNER = probatio.Schema({
    probatio.Optional("domains", default=[]): probatio.All(list, [cv.string]),
    probatio.Optional("entities", default=[]): probatio.All(list, [cv.string]),
    probatio.Optional("entity_globs", default=[]): probatio.All(list, [cv.string]),
})

FILTER_SCHEMA = probatio.Schema({
    probatio.Optional("include"): FILTER_SCHEMA_INNER,
    probatio.Optional("exclude"): FILTER_SCHEMA_INNER,
})


def _whole_seconds(value: timedelta) -> int:
    seconds = int(value.total_seconds())
    if seconds < 1:
        raise probatio.Invalid("must be at least 1 second")
    return seconds


def _unique_entities(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [e["entity_id"] for e in entries]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise probatio.Invalid(f"entities listed more than once: {', '.join(dupes)}")
    return entries


# Entities kept in states_minute (issue #45). hold / zero_hold become integer
# seconds; their meaning is documented at STATES_MINUTE_DDL_SQL in const.py.
MINUTE_ROLLUP_ENTITY_SCHEMA = probatio.Schema({
    probatio.Required("entity_id"): cv.entity_id,
    probatio.Required("hold"): probatio.All(cv.positive_time_period, _whole_seconds),
    probatio.Optional("zero_hold"): probatio.All(cv.positive_time_period, _whole_seconds),
})

DOMAIN_SCHEMA = FILTER_SCHEMA.extend({
    probatio.Optional("minute_rollup"): probatio.All([MINUTE_ROLLUP_ENTITY_SCHEMA], _unique_entities),
})


def build_entity_filter(raw_filter: dict[str, Any]) -> EntityFilter:
    """Filter for a FILTER_SCHEMA-validated domain config; pass-all when it is empty."""
    if raw_filter:
        empty_block = {"domains": [], "entities": [], "entity_globs": []}
        return convert_include_exclude_filter({
            "include": raw_filter.get("include", empty_block),
            "exclude": raw_filter.get("exclude", empty_block),
        })
    return convert_filter({
        "include_domains": [],
        "include_entity_globs": [],
        "include_entities": [],
        "exclude_domains": [],
        "exclude_entity_globs": [],
        "exclude_entities": [],
    })
