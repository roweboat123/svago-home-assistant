"""Recall a single saved slot without ever arming or saving a memory."""

import probatio
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service

from .const import DOMAIN, SUPPORT_RECALL_MEMORY

# A chair has three slot buttons. An area, device or wildcard must not dispatch
# competing motor targets. HA still handles entity resolution and permissions.
RECALL_SCHEMA = probatio.All(
    cv.make_entity_service_schema(None),
    {
        probatio.Required(ATTR_ENTITY_ID): probatio.All(
            list, probatio.Length(min=1, max=1), [cv.entity_domain(Platform.BUTTON)]
        )
    },
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the entity action once, not per chair or Bluetooth connection."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "recall_memory",
        entity_domain=Platform.BUTTON,
        required_features=[SUPPORT_RECALL_MEMORY],
        schema=RECALL_SCHEMA,
        func="async_recall_only",
    )
