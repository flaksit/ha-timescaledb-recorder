"""HA repair-issue helpers. Phase 2 wires only buffer_dropping (D-10);
Phase 3 extends this module with additional translation_keys (D-10-e).
"""
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, SIGNAL_HEALTH_CHANGE

_LOGGER = logging.getLogger(__name__)

# Issue id MUST match the strings.json key under "issues".
# Not configurable — callers pass hass only.
# These constants are the single source of truth for issue IDs — both the
# issue_id arg to async_create_issue and the translation_key arg reference the
# same constant, so there is no string duplication across call sites (MEDIUM-10).
_BUFFER_DROPPING_ISSUE_ID = "buffer_dropping"
_STATES_WORKER_STALLED_ID = "states_worker_stalled"
_META_WORKER_STALLED_ID = "meta_worker_stalled"
_DB_UNREACHABLE_ID = "db_unreachable"
_RECORDER_DISABLED_ID = "recorder_disabled"
_METADATA_DROPPED_ID = "metadata_dropped"
_STATES_DROPPED_ID = "states_dropped"
_INGESTION_STALLED_ID = "ingestion_stalled"


@callback
def create_buffer_dropping_issue(hass: HomeAssistant) -> None:
    """Register the buffer_dropping repair issue. Idempotent (HA dedupes by id).

    Callable from the event loop directly, or via hass.add_job(...) from a
    worker thread (D-10-b says worker observes the overflow flip).
    """
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_BUFFER_DROPPING_ISSUE_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_BUFFER_DROPPING_ISSUE_ID,
    )
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


@callback
def clear_buffer_dropping_issue(hass: HomeAssistant) -> None:
    """Delete the buffer_dropping repair issue. Idempotent — no-op if absent.

    Orchestrator calls this inline on the event loop after clear_and_reset_overflow
    (D-10-c).
    """
    ir.async_delete_issue(hass, DOMAIN, _BUFFER_DROPPING_ISSUE_ID)
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


# ---------------------------------------------------------------------------
# Phase 3 helpers — D-02-a/b (worker stalled), D-11 (db_unreachable,
# recorder_disabled). All helpers take hass as the only positional argument so
# they are compatible with hass.add_job(helper, hass) from worker threads
# (RESEARCH Pitfall 2 — add_job passes positional args only; kwargs are dropped).
# ---------------------------------------------------------------------------


@callback
def create_states_worker_stalled_issue(hass: HomeAssistant) -> None:
    """Register the states_worker_stalled repair issue (D-02).

    Fired by retry decorator's on_stall hook after STALL_THRESHOLD
    consecutive failures. Idempotent — HA dedupes by issue_id.
    """
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_STATES_WORKER_STALLED_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=_STATES_WORKER_STALLED_ID,
    )
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


@callback
def clear_states_worker_stalled_issue(hass: HomeAssistant) -> None:
    """Delete the states_worker_stalled repair issue (D-02-c).

    Fired by retry decorator's on_recovery hook on first success after stall.
    """
    ir.async_delete_issue(hass, DOMAIN, _STATES_WORKER_STALLED_ID)
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


@callback
def create_meta_worker_stalled_issue(hass: HomeAssistant) -> None:
    """Register the meta_worker_stalled repair issue (D-02).

    Fired by retry decorator's on_stall hook in the metadata worker after
    STALL_THRESHOLD consecutive failures. Idempotent — HA dedupes by issue_id.
    """
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_META_WORKER_STALLED_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=_META_WORKER_STALLED_ID,
    )
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


@callback
def clear_meta_worker_stalled_issue(hass: HomeAssistant) -> None:
    """Delete the meta_worker_stalled repair issue (D-02-c).

    Fired by retry decorator's on_recovery hook on first success after stall.
    """
    ir.async_delete_issue(hass, DOMAIN, _META_WORKER_STALLED_ID)
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


@callback
def create_db_unreachable_issue(hass: HomeAssistant) -> None:
    """Register the db_unreachable repair issue (D-11).

    Fired by retry decorator's on_sustained_fail hook when cumulative failure
    duration exceeds DB_UNREACHABLE_THRESHOLD_SECONDS. Idempotent.
    """
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_DB_UNREACHABLE_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=_DB_UNREACHABLE_ID,
    )
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


@callback
def clear_db_unreachable_issue(hass: HomeAssistant) -> None:
    """Delete the db_unreachable repair issue (D-11).

    Fired on first successful write after sustained failure period ends.
    """
    ir.async_delete_issue(hass, DOMAIN, _DB_UNREACHABLE_ID)
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


@callback
def create_recorder_disabled_issue(hass: HomeAssistant) -> None:
    """Register the recorder_disabled repair issue (D-11).

    Fired while HA's sqlite recorder is not loaded or is disabled (setup
    check + recorder watcher in __init__.py). Severity WARNING — the integration still operates, but backfill
    after a DB outage is unavailable. Idempotent.
    """
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_RECORDER_DISABLED_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_RECORDER_DISABLED_ID,
    )
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


@callback
def clear_recorder_disabled_issue(hass: HomeAssistant) -> None:
    """Delete the recorder_disabled repair issue (D-11).

    Called by the setup check or recorder watcher when the recorder is
    available.
    """
    ir.async_delete_issue(hass, DOMAIN, _RECORDER_DISABLED_ID)
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


@callback
def create_metadata_dropped_issue(hass: HomeAssistant) -> None:
    """Register the metadata_dropped repair issue (issue #17).

    Fired by the meta worker the first time it gives up on an item. The item
    itself is in the metadata_deadletter table; this exists so the operator
    learns that metadata history has a hole without reading the log. Never
    cleared automatically — the hole does not heal on its own, and a repair
    issue that disappears on restart would hide exactly what it is for.
    """
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_METADATA_DROPPED_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=_METADATA_DROPPED_ID,
    )
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


@callback
def create_states_dropped_issue(hass: HomeAssistant) -> None:
    """Register the states_dropped repair issue (issue #20).

    Fired by the states worker each time it gives up on a row. The row is in
    the states_deadletter table. Never cleared automatically, for the same
    reason as metadata_dropped.
    """
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_STATES_DROPPED_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=_STATES_DROPPED_ID,
    )
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


@callback
def create_ingestion_stalled_issue(hass: HomeAssistant, details: str) -> None:
    """Register or update the ingestion_stalled repair issue (issue #14).

    Raised by the watchdog when a worker is stuck without failing: a DB call in
    flight past INGESTION_STALL_THRESHOLD_S, or the states worker waiting that
    long for backfill data. `details` names each stuck worker and what it is
    doing; re-creating with new details updates the issue in place.
    """
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_INGESTION_STALLED_ID,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=_INGESTION_STALLED_ID,
        translation_placeholders={"details": details},
    )
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)


@callback
def clear_ingestion_stalled_issue(hass: HomeAssistant) -> None:
    """Delete the ingestion_stalled repair issue once no worker is stuck."""
    ir.async_delete_issue(hass, DOMAIN, _INGESTION_STALLED_ID)
    async_dispatcher_send(hass, SIGNAL_HEALTH_CHANGE)
