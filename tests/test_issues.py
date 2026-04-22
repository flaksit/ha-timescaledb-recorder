"""Unit tests for issues.create/clear_buffer_dropping_issue (D-10-a/b/c)."""
from unittest.mock import MagicMock, patch

import pytest

from custom_components.ha_timescaledb_recorder.const import DOMAIN
from custom_components.ha_timescaledb_recorder.issues import (
    create_buffer_dropping_issue,
    clear_buffer_dropping_issue,
)


def test_create_buffer_dropping_issue_delegates_to_ir():
    hass = MagicMock()
    with patch(
        "custom_components.ha_timescaledb_recorder.issues.ir.async_create_issue"
    ) as mock_create:
        create_buffer_dropping_issue(hass)
    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["domain"] == DOMAIN
    assert kwargs["issue_id"] == "buffer_dropping"
    assert kwargs["is_fixable"] is False
    assert kwargs["translation_key"] == "buffer_dropping"
    # severity is WARNING
    from homeassistant.helpers import issue_registry as ir
    assert kwargs["severity"] == ir.IssueSeverity.WARNING


def test_clear_buffer_dropping_issue_delegates_to_ir():
    hass = MagicMock()
    with patch(
        "custom_components.ha_timescaledb_recorder.issues.ir.async_delete_issue"
    ) as mock_delete:
        clear_buffer_dropping_issue(hass)
    mock_delete.assert_called_once_with(hass, DOMAIN, "buffer_dropping")


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
