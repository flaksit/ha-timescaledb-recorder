"""HA repair-issue helpers. Phase 2 wires only buffer_dropping (D-10);
Phase 3 extends this module with additional translation_keys (D-10-e).
"""
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Issue id MUST match the strings.json key under "issues".
# Not configurable — callers pass hass only.
_BUFFER_DROPPING_ISSUE_ID = "buffer_dropping"


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


def clear_buffer_dropping_issue(hass: HomeAssistant) -> None:
    """Delete the buffer_dropping repair issue. Idempotent — no-op if absent.

    Orchestrator calls this inline on the event loop after clear_and_reset_overflow
    (D-10-c).
    """
    ir.async_delete_issue(hass, DOMAIN, _BUFFER_DROPPING_ISSUE_ID)
