"""Tests for repair_scd2 helpers that need no database.

The rest of the script is exercised in test_scd2_invariant_db.py against a real
server; these are the pure functions, which are worth their own file because
one of them decides whether a password reaches the terminal.
"""
import pytest

pytest.importorskip("psycopg")

from custom_components.timescaledb_recorder.repair_scd2 import _redact  # noqa: E402


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        # URI form — the case the original implementation covered.
        ("postgresql://ha:s3cret@db:5432/homeassistant",
         "postgresql://ha:***@db:5432/homeassistant"),
        # Keyword form. This one printed the password verbatim, which then sat
        # in shell scrollback and in any log pasted into a bug report.
        ("host=db port=5432 dbname=ha user=ha password=s3cret",
         "host=db port=5432 dbname=ha user=ha password=***"),
        ("password=s3cret host=db", "password=*** host=db"),
        # Quoted values are legal in keyword DSNs and may contain spaces.
        ("host=db password='s3 cret' user=ha", "host=db password=*** user=ha"),
        # Case is not significant in keyword names.
        ("host=db PASSWORD=s3cret", "host=db password=***"),
    ],
)
def test_redact_hides_the_password(dsn, expected):
    assert _redact(dsn) == expected


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://ha@db:5432/homeassistant",
        "host=db port=5432 dbname=homeassistant user=ha",
        "postgresql:///homeassistant",
    ],
)
def test_redact_leaves_a_passwordless_dsn_readable(dsn):
    """The printed DSN is how the operator confirms the target, so redaction
    must not blur anything that is not a secret."""
    assert _redact(dsn) == dsn


def test_redact_does_not_eat_the_host_in_a_uri():
    """A password containing '@' or '/' must not swallow the rest of the URI."""
    assert _redact("postgresql://ha:p@ss/word@db:5432/homeassistant").endswith(
        "@db:5432/homeassistant")
