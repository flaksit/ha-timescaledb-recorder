"""StateIngester: thin event relay that enqueues state changes to the worker queue."""
import logging
import queue

from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers.entityfilter import EntityFilter

from .worker import StateRow

_LOGGER = logging.getLogger(__name__)


class StateIngester:
    """Relay HA state_changed events to the DbWorker queue.

    Lifecycle:
    - async_start(): register STATE_CHANGED listener
    - stop(): cancel listener (sync, no flush — worker handles final flush on shutdown)

    The @callback handler is the event loop / worker thread boundary:
    - Event loop side: receives events, applies filter, builds StateRow, enqueues
    - Worker side: dequeues StateRow items, batches, writes to TimescaleDB
    """

    def __init__(
        self,
        hass: HomeAssistant,
        queue: queue.Queue,
        entity_filter: EntityFilter,
    ) -> None:
        self._hass = hass
        self._queue = queue
        self._entity_filter = entity_filter
        self._cancel_listener = None

    def async_start(self) -> None:
        """Register the state_changed event listener."""
        from homeassistant.const import EVENT_STATE_CHANGED

        self._cancel_listener = self._hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._handle_state_changed
        )

    @callback
    def _handle_state_changed(self, event: Event) -> None:
        """Handle state_changed events — synchronous, never awaits.

        put_nowait() is non-blocking and thread-safe from an asyncio @callback context.
        dict(new_state.attributes) copies the snapshot — avoids holding a reference to
        a mutable HA object that may be updated before the worker processes the row.
        """
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        entity_id = new_state.entity_id
        if not self._entity_filter(entity_id):
            return

        self._queue.put_nowait(StateRow(
            entity_id=entity_id,
            state=new_state.state,
            attributes=dict(new_state.attributes),
            last_updated=new_state.last_updated,
            last_changed=new_state.last_changed,
        ))

    def stop(self) -> None:
        """Cancel the state_changed listener.

        Sync method (not async) — no coroutine overhead needed for simple cancellation.
        Named stop() rather than the async_ prefix convention to prevent callers from
        incorrectly awaiting it — there is no coroutine here, just a callback cancel.

        No final flush needed — DbWorker handles the remaining buffer when it
        processes the _STOP sentinel enqueued by async_unload_entry.
        """
        if self._cancel_listener is not None:
            self._cancel_listener()
            self._cancel_listener = None
