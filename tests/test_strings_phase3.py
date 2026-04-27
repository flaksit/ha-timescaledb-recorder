"""Tests for Phase 3 strings.json issues entries (D-10)."""
import json
import pathlib


def _load_strings():
    src = pathlib.Path("custom_components/timescaledb_recorder/strings.json")
    return json.loads(src.read_text())


def test_strings_json_is_valid_json():
    """strings.json must be parseable."""
    data = _load_strings()
    assert isinstance(data, dict)


def test_strings_json_has_all_five_issue_keys():
    data = _load_strings()
    expected = {
        "buffer_dropping",
        "states_worker_stalled",
        "meta_worker_stalled",
        "db_unreachable",
        "recorder_disabled",
    }
    assert set(data["issues"].keys()) == expected


def test_new_issue_entries_have_nonempty_title_and_description():
    data = _load_strings()
    new_keys = [
        "states_worker_stalled",
        "meta_worker_stalled",
        "db_unreachable",
        "recorder_disabled",
    ]
    for key in new_keys:
        entry = data["issues"][key]
        assert "title" in entry, f"{key} missing title"
        assert "description" in entry, f"{key} missing description"
        assert entry["title"], f"{key} title is empty"
        assert entry["description"], f"{key} description is empty"


def test_buffer_dropping_entry_unchanged():
    data = _load_strings()
    entry = data["issues"]["buffer_dropping"]
    assert entry["title"] == "TimescaleDB recorder buffer overflow"
    assert "dropping state events" in entry["description"]
