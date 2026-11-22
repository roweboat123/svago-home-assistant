"""Heat feedback and explicit one-shot arming for memory saves."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .entity import SvagoEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    connection = entry.runtime_data
    async_add_entities(
        [
            HeatSwitch(connection, "heat", "Heat"),
            MemorySetSwitch(connection),
        ]
    )


class HeatSwitch(SvagoEntity, SwitchEntity):
    _attr_icon = "mdi:radiator"

    @property
    def is_on(self):
        return self.controller.status.heat if self.controller.status else None

    async def async_turn_on(self, **kwargs) -> None:
        await self.execute(self.controller.set_heat(True), request="On")

    async def async_turn_off(self, **kwargs) -> None:
        await self.execute(self.controller.set_heat(False), request="Off")


class MemorySetSwitch(SvagoEntity, SwitchEntity):
    """Arm one save, then use a Memory button to choose its destination."""

    _attr_icon = "mdi:content-save"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, connection):
        super().__init__(connection, "set_memory", "Set memory")

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.connection.memories is not None
            and not self.connection.memory_save_in_progress()
            and (self.is_on or self.controller.can_save_memory)
        )

    @property
    def is_on(self) -> bool:
        return self.connection.memory_save_armed()

    async def async_turn_on(self, **kwargs) -> None:
        await self.execute(self.connection.set_memory_save_armed(True), request="On")

    async def async_turn_off(self, **kwargs) -> None:
        await self.execute(self.connection.set_memory_save_armed(False), request="Off")
