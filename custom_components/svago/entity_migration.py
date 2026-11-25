"""Remove only integration-owned controls retired by interface updates."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

RETIRED_BUTTON_KEYS = frozenset(
    {
        "turn_on",
        "seat_vertical",
        "seat_horizontal",
        "memory_set_1",
        "memory_set_2",
        "memory_set_3",
    }
)
RETIRED_SWITCH_KEYS = frozenset({"memory_set_1", "memory_set_2", "memory_set_3"})


@callback
def async_remove_retired_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove obsolete controls without touching replacements or other devices."""
    registry = er.async_get(hass)
    identities = {
        (domain, f"{entry.data[CONF_ADDRESS]}_{key}")
        for domain, keys in (
            ("button", RETIRED_BUTTON_KEYS),
            ("switch", RETIRED_SWITCH_KEYS),
        )
        for key in keys
    }
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.platform == DOMAIN and (entity.domain, entity.unique_id) in identities:
            registry.async_remove(entity.entity_id)
