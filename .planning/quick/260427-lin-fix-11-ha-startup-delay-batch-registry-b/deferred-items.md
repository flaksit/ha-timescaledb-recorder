# Deferred Items — 260427-lin

Out-of-scope discoveries logged during execution per executor SCOPE BOUNDARY rule.
NOT fixed by this plan. Track for a future cleanup task.

## Pre-existing broken tests (unrelated to issue #11)

### tests/test_states_worker.py

`ImportError: cannot import name 'SELECT_OPEN_ENTITIES_SQL' from
'custom_components.timescaledb_recorder.const'`.

Stale reference to a constant removed when the codebase was renamed
(commit `6411d6b rename: remove ha_ and dim_ prefix from code and tables`).
The test module itself was not updated to track the const cleanup.

Action: rename / remove the obsolete test module, or rewrite it against
the current `const.py` symbols.

### tests/test_config_flow.py::test_valid_dsn

`AssertionError: assert <FlowResultType.FORM: 'form'> == 'create_entry'`.

Pre-existing assertion mismatch — test expects the config flow to land on
`create_entry` after the DSN form, but the current flow returns `form`
(probably an extra step was added without the test being updated).

Action: align the test with the current config flow shape.

Both failures reproduce on the clean tree (verified: `git stash` reported
no local changes, fresh `pytest` run still fails identically) and predate
this plan.
