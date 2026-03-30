"""StateIngester: buffered batch writer for TimescaleDB."""
import json
import logging
from datetime import timedelta

import asyncpg

from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers.entityfilter import EntityFilter
from homeassistant.helpers.event import async_track_time_interval

from .const import DEFAULT_BATCH_SIZE, DEFAULT_FLUSH_INTERVAL, INSERT_SQL

_LOGGER = logging.getLogger(__name__)


class StateIngester:
    """Buffer state changes and flush them to TimescaleDB in batches."""

    def __init__(
        self,
        hass: HomeAssistant,
        pool: asyncpg.Pool,
        entity_filter: EntityFilter,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval: int = DEFAULT_FLUSH_INTERVAL,
    ) -> None:
        self._hass = hass
        self._pool = pool
        self._entity_filter = entity_filter
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._buffer: list[tuple] = []
        self._cancel_listener = None
        self._cancel_timer = None

    def async_start(self) -> None:
        """Register event listener and start the flush timer."""
        from homeassistant.const import EVENT_STATE_CHANGED

        self._cancel_listener = self._hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._handle_state_changed
        )
        self._cancel_timer = async_track_time_interval(
            self._hass,
            self._async_flush_timer,
            timedelta(seconds=self._flush_interval),
            name="ha_timescaledb_flush",
        )

    @callback
    def _handle_state_changed(self, event: Event) -> None:
        """Handle state_changed events — synchronous, never awaits."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        entity_id = new_state.entity_id
        if not self._entity_filter(entity_id):
            return

        self._buffer.append((
            entity_id,
            new_state.state,
            json.dumps(dict(new_state.attributes), default=str),
            new_state.last_updated,
            new_state.last_changed,
        ))

        if len(self._buffer) >= self._batch_size:
            self._hass.async_create_task(self._async_flush())

    async def _async_flush(self) -> None:
        """Flush buffer to TimescaleDB."""
        if not self._buffer:
            return

        rows = self._buffer[:]
        self._buffer.clear()

        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(INSERT_SQL, rows)
        except (asyncpg.PostgresConnectionError, OSError):
            # Connection error — re-queue rows and expire pool connections
            self._pool.expire_connections()
            self._buffer.extend(rows)
            _LOGGER.warning("Connection error during flush; %d rows re-queued", len(rows))
        except asyncpg.PostgresError as err:
            # Bad SQL or other PG error — drop batch to avoid retrying corrupt data
            _LOGGER.error("PostgresError during flush; %d rows dropped: %s", len(rows), err)
            if rows:
                _LOGGER.debug("First failed row: %r", rows[0])

    async def _async_flush_timer(self, _now) -> None:
        """Timer-triggered flush — only flushes if buffer is non-empty."""
        if self._buffer:
            await self._async_flush()

    async def async_stop(self) -> None:
        """Cancel listener and timer, then perform a final flush."""
        if self._cancel_listener is not None:
            self._cancel_listener()
            self._cancel_listener = None
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        await self._async_flush()
