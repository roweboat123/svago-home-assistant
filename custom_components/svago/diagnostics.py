"""Export recent chair status and writes without issuing any device commands."""

from .const import VERSION


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    connection = getattr(entry, "runtime_data", None)
    if connection is None:
        return {"version": VERSION, "connection_initialized": False}
    return connection.diagnostics()
