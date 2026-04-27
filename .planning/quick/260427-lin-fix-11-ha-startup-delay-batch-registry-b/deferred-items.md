# Deferred Items — 260427-lin

All initially-deferred pre-existing test failures have been fixed in
follow-up commit alongside this plan. Nothing remains deferred.

## Resolved

### tests/test_states_worker.py — fixed

`SELECT_OPEN_ENTITIES_SQL` constant was renamed to `SELECT_ALL_KNOWN_ENTITIES_SQL`
in commit `566f8e6` (refactor: replace open-entity filter with full known-entity
union). Test imports + assertion updated to track the rename. The
`read_open_entities` test was renamed to `test_read_all_known_entities_returns_set_of_ids`
and asserts against `SELECT_ALL_KNOWN_ENTITIES_SQL`.

### tests/test_config_flow.py::test_valid_dsn / test_invalid_dsn — fixed

The config flow was migrated from `asyncpg.connect` to
`psycopg.AsyncConnection.connect` in commit `ef77e6e`. Tests still patched
the old `asyncpg.connect` symbol, so the real `psycopg.AsyncConnection.connect`
ran (or attempted to run) and returned an error path. Patches updated to
target `psycopg.AsyncConnection.connect`. `mock_conn.close` is now awaited
(`assert_awaited_once`) to match the async-connection lifecycle.
