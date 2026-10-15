"""backfill_gaps.py: entity selection follows the integration's YAML filter."""
import sqlite3
from pathlib import Path

import pytest

from custom_components.timescaledb_recorder.backfill_gaps import (
    load_entity_filter,
    resolve_entity_set,
    sqlite_bucket_count,
)

KNOWN = {"sensor.kept", "sensor.solar_surplus", "sensor.solar_surplus_3min", "sun.sun"}


def _write_config(config_dir: Path, domain_block: str) -> None:
    (config_dir / "secrets.yaml").write_text("tz: Europe/Brussels\n", encoding="utf-8")
    (config_dir / "tsdb.yaml").write_text(domain_block, encoding="utf-8")
    (config_dir / "configuration.yaml").write_text(
        "homeassistant:\n  time_zone: !secret tz\n"
        "timescaledb_recorder: !include tsdb.yaml\n",
        encoding="utf-8",
    )


def test_load_entity_filter_reads_exclude_through_include_and_secret(tmp_path):
    _write_config(tmp_path, "exclude:\n  entity_globs:\n    - sensor.solar_surplus*\n")
    entity_filter = load_entity_filter(str(tmp_path))
    assert {e for e in KNOWN if entity_filter(e)} == {"sensor.kept", "sun.sun"}


def test_load_entity_filter_without_domain_config_passes_all(tmp_path):
    (tmp_path / "configuration.yaml").write_text("homeassistant:\n", encoding="utf-8")
    entity_filter = load_entity_filter(str(tmp_path))
    assert all(entity_filter(e) for e in KNOWN)


def test_resolve_entity_set_pass_all_needs_no_filtering():
    assert resolve_entity_set(KNOWN, lambda _e: True) is None


def test_resolve_entity_set_drops_excluded_entities():
    keep = lambda e: not e.startswith("sensor.solar_surplus")  # noqa: E731
    assert resolve_entity_set(KNOWN, keep) == {"sensor.kept", "sun.sun"}


@pytest.fixture
def recorder_db(tmp_path) -> str:
    path = tmp_path / "home-assistant_v2.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id TEXT)")
        conn.execute("CREATE TABLE states (metadata_id INTEGER, last_updated_ts REAL)")
        conn.executemany("INSERT INTO states_meta VALUES (?, ?)", [(1, "sensor.kept"), (2, "sensor.solar_surplus")])
        conn.executemany("INSERT INTO states VALUES (?, ?)", [(1, 10.0), (2, 11.0), (2, 12.0)])
    return str(path)


def test_sqlite_bucket_count_filtered(recorder_db):
    assert sqlite_bucket_count(recorder_db, 0.0, 100.0) == 3
    assert sqlite_bucket_count(recorder_db, 0.0, 100.0, {"sensor.kept"}) == 1
