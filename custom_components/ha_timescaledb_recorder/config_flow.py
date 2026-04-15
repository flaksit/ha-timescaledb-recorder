"""Config flow and options flow for TimescaleDB Recorder."""
import asyncpg
import voluptuous as vol

from homeassistant import config_entries

from .const import (
    CONF_BATCH_SIZE,
    CONF_CHUNK_INTERVAL,
    CONF_COMPRESS_AFTER,
    CONF_DSN,
    CONF_FLUSH_INTERVAL,
    DEFAULT_BATCH_SIZE,
    DEFAULT_CHUNK_INTERVAL_DAYS,
    DEFAULT_COMPRESS_AFTER_HOURS,
    DEFAULT_FLUSH_INTERVAL,
    DOMAIN,
)


class TimescaleDBConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle initial configuration — collect and validate the DSN."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Show the DSN form; on submit validate the connection."""
        errors = {}

        if user_input is not None:
            dsn = user_input[CONF_DSN]
            try:
                conn = await asyncpg.connect(dsn)
                await conn.close()
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title="TimescaleDB",
                    data={CONF_DSN: dsn},
                )

        schema = vol.Schema({vol.Required(CONF_DSN): str})
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return TimescaleDBOptionsFlow(config_entry)


class TimescaleDBOptionsFlow(config_entries.OptionsFlow):
    """Allow the user to tune batch/flush/compression parameters."""

    def __init__(self, config_entry) -> None:
        super().__init__()
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Show the options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options

        schema = vol.Schema({
            vol.Optional(
                CONF_BATCH_SIZE,
                default=current.get(CONF_BATCH_SIZE, DEFAULT_BATCH_SIZE),
            ): vol.All(int, vol.Range(min=1, max=10000)),
            vol.Optional(
                CONF_FLUSH_INTERVAL,
                default=current.get(CONF_FLUSH_INTERVAL, DEFAULT_FLUSH_INTERVAL),
            ): vol.All(int, vol.Range(min=1, max=300)),
            vol.Optional(
                CONF_CHUNK_INTERVAL,
                default=current.get(CONF_CHUNK_INTERVAL, DEFAULT_CHUNK_INTERVAL_DAYS),
            ): vol.All(int, vol.Range(min=1, max=365)),
            vol.Optional(
                CONF_COMPRESS_AFTER,
                default=current.get(CONF_COMPRESS_AFTER, DEFAULT_COMPRESS_AFTER_HOURS),
            ): vol.All(int, vol.Range(min=1, max=720)),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
