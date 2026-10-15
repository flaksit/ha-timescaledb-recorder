"""YAML include/exclude filter: schema and the predicate built from it.

Kept free of integration imports so backfill_gaps.py, which runs as a plain
script inside the HA container, applies exactly the filter the integration does.
"""
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
