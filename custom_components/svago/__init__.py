"""Svago Motion Mastery BLE controls."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .connection import Connection
from .entity_migration import async_remove_retired_entities
from .memory import MemoryStore
from .preferences import TimerPreferences

PLATFORMS = [Platform.BUTTON, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    connection = Connection(hass, entry.data[CONF_ADDRESS])
    # Keep the failed attempt available to read-only diagnostics after cleanup.
    entry.runtime_data = connection
    try:
        await connection.load_preferences(entry.entry_id)
        connection.memories = MemoryStore(hass, entry.entry_id, connection.notify_listeners)
        # New installations begin with empty slots. Existing profiles and any
        # legacy migration markers are read as stored, never replaced by defaults.
        await connection.memories.async_load()
        await connection.start()
    except Exception as exc:
        await connection.close()
        raise ConfigEntryNotReady(str(exc)) from exc

    async def shutdown(_event):
        await connection.close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown))
    try:
        async_remove_retired_entities(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await connection.close()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.close()
        return True
    return False


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove saved profiles and choices only when the entry is deleted."""
    connection = getattr(entry, "runtime_data", None)
    memories = getattr(connection, "memories", None)
    if memories is None:
        memories = MemoryStore(hass, entry.entry_id, lambda: None)
    await memories.async_close()
    await memories.async_remove()
    preferences = getattr(connection, "preferences", None)
    if preferences is None:
        preferences = TimerPreferences(hass, entry.entry_id)
    # Removal drains pending writes itself; a failed save must not prevent deletion.
    await preferences.async_remove()
