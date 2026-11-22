"""Read-only connection validation before adding the chair."""

import logging
import re

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_ADDRESS

from .connection import Connection
from .const import DOMAIN, NAME, VERSION

_LOGGER = logging.getLogger(__name__)


class SvagoConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_bluetooth(self, discovery_info):
        self._address = discovery_info.address.upper()
        await self.async_set_unique_id(self._address)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": NAME}
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        errors = {}
        error_detail = ""
        if user_input is not None:
            address = str(user_input[CONF_ADDRESS]).strip().upper()
            if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                connection = Connection(self.hass, address)
                try:
                    # Subscribe and validate only. No FFF2 control writes in setup.
                    await connection.start(monitor=False)
                except Exception as exc:
                    errors["base"] = "cannot_connect"
                    error_detail = str(exc) or type(exc).__name__
                    _LOGGER.warning(
                        "Svago %s setup failed: %s", VERSION, error_detail, exc_info=True
                    )
                finally:
                    await connection.close()
                if not errors:
                    return self.async_create_entry(title=NAME, data={CONF_ADDRESS: address})
            self._address = address
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS, default=getattr(self, "_address", "")): str}
            ),
            errors=errors,
            description_placeholders={"error_detail": error_detail},
        )
