"""Shared test fixtures for ha_timescaledb_recorder tests."""
from unittest.mock import AsyncMock, MagicMock
import pytest


@pytest.fixture
def mock_conn():
    """Return a mock asyncpg connection."""
    conn = AsyncMock()
    return conn


@pytest.fixture
def mock_pool(mock_conn):
    """Return a mock asyncpg pool whose acquire() context manager yields mock_conn."""
    pool = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    return pool
