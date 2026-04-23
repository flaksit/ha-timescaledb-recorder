"""Unit tests for issues.create/clear helpers (D-10-a/b/c, Phase 3 D-02, D-11)."""
from unittest.mock import MagicMock, patch

import pytest

from custom_components.ha_timescaledb_recorder.const import DOMAIN
from custom_components.ha_timescaledb_recorder.issues import (
    create_buffer_dropping_issue,
    clear_buffer_dropping_issue,
    create_states_worker_stalled_issue,
    clear_states_worker_stalled_issue,
    create_meta_worker_stalled_issue,
    clear_meta_worker_stalled_issue,
    create_db_unreachable_issue,
    clear_db_unreachable_issue,
    create_recorder_disabled_issue,
    clear_recorder_disabled_issue,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ir_create_kwargs(hass, func):
    """Call func(hass) under a patch and return the kwargs passed to async_create_issue."""
    with patch(
        "custom_components.ha_timescaledb_recorder.issues.ir.async_create_issue"
    ) as mock_create:
        func(hass)
    mock_create.assert_called_once()
    return mock_create.call_args.kwargs


def _ir_delete_args(hass, func):
    """Call func(hass) under a patch and return positional args to async_delete_issue."""
    with patch(
        "custom_components.ha_timescaledb_recorder.issues.ir.async_delete_issue"
    ) as mock_delete:
        func(hass)
    mock_delete.assert_called_once()
    return mock_delete.call_args.args


# ---------------------------------------------------------------------------
# Existing buffer_dropping — regression tests
# ---------------------------------------------------------------------------

def test_create_buffer_dropping_issue_delegates_to_ir():
    hass = MagicMock()
    kwargs = _ir_create_kwargs(hass, create_buffer_dropping_issue)
    assert kwargs["domain"] == DOMAIN
    assert kwargs["issue_id"] == "buffer_dropping"
    assert kwargs["is_fixable"] is False
    assert kwargs["translation_key"] == "buffer_dropping"
    from homeassistant.helpers import issue_registry as ir
    assert kwargs["severity"] == ir.IssueSeverity.WARNING


def test_clear_buffer_dropping_issue_delegates_to_ir():
    hass = MagicMock()
    args = _ir_delete_args(hass, clear_buffer_dropping_issue)
    assert args == (hass, DOMAIN, "buffer_dropping")


# ---------------------------------------------------------------------------
# states_worker_stalled
# ---------------------------------------------------------------------------

def test_create_states_worker_stalled_issue():
    hass = MagicMock()
    kwargs = _ir_create_kwargs(hass, create_states_worker_stalled_issue)
    from homeassistant.helpers import issue_registry as ir
    assert kwargs["domain"] == DOMAIN
    assert kwargs["issue_id"] == "states_worker_stalled"
    assert kwargs["is_fixable"] is False
    assert kwargs["severity"] == ir.IssueSeverity.ERROR
    assert kwargs["translation_key"] == "states_worker_stalled"


def test_clear_states_worker_stalled_issue():
    hass = MagicMock()
    args = _ir_delete_args(hass, clear_states_worker_stalled_issue)
    assert args == (hass, DOMAIN, "states_worker_stalled")


# ---------------------------------------------------------------------------
# meta_worker_stalled
# ---------------------------------------------------------------------------

def test_create_meta_worker_stalled_issue():
    hass = MagicMock()
    kwargs = _ir_create_kwargs(hass, create_meta_worker_stalled_issue)
    from homeassistant.helpers import issue_registry as ir
    assert kwargs["domain"] == DOMAIN
    assert kwargs["issue_id"] == "meta_worker_stalled"
    assert kwargs["is_fixable"] is False
    assert kwargs["severity"] == ir.IssueSeverity.ERROR
    assert kwargs["translation_key"] == "meta_worker_stalled"


def test_clear_meta_worker_stalled_issue():
    hass = MagicMock()
    args = _ir_delete_args(hass, clear_meta_worker_stalled_issue)
    assert args == (hass, DOMAIN, "meta_worker_stalled")


# ---------------------------------------------------------------------------
# db_unreachable
# ---------------------------------------------------------------------------

def test_create_db_unreachable_issue():
    hass = MagicMock()
    kwargs = _ir_create_kwargs(hass, create_db_unreachable_issue)
    from homeassistant.helpers import issue_registry as ir
    assert kwargs["domain"] == DOMAIN
    assert kwargs["issue_id"] == "db_unreachable"
    assert kwargs["is_fixable"] is False
    assert kwargs["severity"] == ir.IssueSeverity.ERROR
    assert kwargs["translation_key"] == "db_unreachable"


def test_clear_db_unreachable_issue():
    hass = MagicMock()
    args = _ir_delete_args(hass, clear_db_unreachable_issue)
    assert args == (hass, DOMAIN, "db_unreachable")


# ---------------------------------------------------------------------------
# recorder_disabled
# ---------------------------------------------------------------------------

def test_create_recorder_disabled_issue():
    hass = MagicMock()
    kwargs = _ir_create_kwargs(hass, create_recorder_disabled_issue)
    from homeassistant.helpers import issue_registry as ir
    assert kwargs["domain"] == DOMAIN
    assert kwargs["issue_id"] == "recorder_disabled"
    assert kwargs["is_fixable"] is False
    assert kwargs["severity"] == ir.IssueSeverity.WARNING
    assert kwargs["translation_key"] == "recorder_disabled"


def test_clear_recorder_disabled_issue():
    hass = MagicMock()
    args = _ir_delete_args(hass, clear_recorder_disabled_issue)
    assert args == (hass, DOMAIN, "recorder_disabled")


# ---------------------------------------------------------------------------
# Single-positional-argument shape — required for hass.add_job bridge
# ---------------------------------------------------------------------------

def test_all_create_helpers_take_only_hass():
    """Each create/clear helper must accept exactly one positional arg (hass).

    hass.add_job(helper, hass) passes positional args only — extra kwargs would
    be silently dropped (RESEARCH Pitfall 2). Verify via inspect.
    """
    import inspect
    helpers = [
        create_states_worker_stalled_issue,
        clear_states_worker_stalled_issue,
        create_meta_worker_stalled_issue,
        clear_meta_worker_stalled_issue,
        create_db_unreachable_issue,
        clear_db_unreachable_issue,
        create_recorder_disabled_issue,
        clear_recorder_disabled_issue,
        create_buffer_dropping_issue,
        clear_buffer_dropping_issue,
    ]
    for fn in helpers:
        sig = inspect.signature(fn)
        params = [
            p for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
               and p.kind in (
                   inspect.Parameter.POSITIONAL_ONLY,
                   inspect.Parameter.POSITIONAL_OR_KEYWORD,
               )
        ]
        assert len(params) == 1, (
            f"{fn.__name__} must have exactly 1 positional param, got {len(params)}"
        )
        assert list(sig.parameters)[0] == "hass", (
            f"{fn.__name__} first param must be named 'hass'"
        )


# ---------------------------------------------------------------------------
# Translation-key round-trip (MEDIUM-10 from cross-AI review 2026-04-23)
# ---------------------------------------------------------------------------

def test_all_issue_id_constants_present_in_strings_json():
    """Every _*_ID constant in issues.py must have a matching key in strings.json.

    This test catches issue-ID/translation-key drift before it reaches runtime
    (cross-AI review 2026-04-23, concern MEDIUM-10). The mapping is:
      issues.py private _*_ID constant value → strings.json["issues"] key.
    """
    import importlib
    import json
    import re
    import pathlib

    import custom_components.ha_timescaledb_recorder.issues as issues_mod

    # Collect all private module-level constants whose name ends in _ID
    id_constants = {
        name: getattr(issues_mod, name)
        for name in dir(issues_mod)
        if re.match(r"^_[A-Z_]+_ID$", name)
    }

    strings_path = pathlib.Path(
        "custom_components/ha_timescaledb_recorder/strings.json"
    )
    strings = json.loads(strings_path.read_text())
    issues_keys = set(strings.get("issues", {}).keys())

    for const_name, issue_id in id_constants.items():
        assert issue_id in issues_keys, (
            f"issues.py constant {const_name}={issue_id!r} has no matching "
            f"key in strings.json['issues']. Add it or remove the constant."
        )


def test_strings_json_has_matching_translation_key():
    """Regression guard — no 'missing translation_key' warnings at runtime."""
    import json
    import pathlib

    src = pathlib.Path("custom_components/ha_timescaledb_recorder/strings.json")
    data = json.loads(src.read_text())
    assert "issues" in data
    assert "buffer_dropping" in data["issues"]
    assert "title" in data["issues"]["buffer_dropping"]
    assert "description" in data["issues"]["buffer_dropping"]
