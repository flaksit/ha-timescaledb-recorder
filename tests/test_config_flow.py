"""Unit tests for TimescaleDB config flow."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.timescaledb_recorder.config_flow import TimescaledbConfigFlow
from custom_components.timescaledb_recorder.const import CONF_DSN


async def test_show_form():
    flow = TimescaledbConfigFlow()
    flow.hass = MagicMock()
    result = await flow.async_step_user(None)
    assert result["type"] == "form"
    assert result["step_id"] == "user"


async def test_valid_dsn():
    flow = TimescaledbConfigFlow()
    flow.hass = MagicMock()
    mock_conn = AsyncMock()
    with patch(
        "psycopg.AsyncConnection.connect",
        new=AsyncMock(return_value=mock_conn),
    ):
        result = await flow.async_step_user({CONF_DSN: "postgresql://user:pass@localhost/db"})
    assert result["type"] == "create_entry"
    assert result["title"] == "TimescaleDB"
    assert result["data"][CONF_DSN] == "postgresql://user:pass@localhost/db"
    mock_conn.close.assert_awaited_once()


async def test_invalid_dsn():
    flow = TimescaledbConfigFlow()
    flow.hass = MagicMock()
    with patch(
        "psycopg.AsyncConnection.connect",
        new=AsyncMock(side_effect=Exception("connection refused")),
    ):
        result = await flow.async_step_user({CONF_DSN: "postgresql://bad/db"})
    assert result["type"] == "form"
    assert result["errors"]["base"] == "cannot_connect"
