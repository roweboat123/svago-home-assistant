"""Reported-position sliders driven by the captured directional commands."""

import math

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.exceptions import HomeAssistantError

from .entity import SvagoEntity
from .protocol import OBSERVED_BOUNDS, POSITION_NAMES

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        PositionNumber(entry.runtime_data, index, name) for index, name in enumerate(POSITION_NAMES)
    )


class PositionNumber(SvagoEntity, NumberEntity):
    _attr_mode = NumberMode.SLIDER
    _attr_native_step = 1
    _attr_icon = "mdi:seat"

    def __init__(self, connection, index, name):
        super().__init__(connection, f"{name}_position", f"{name.title()} position")
        self._index = index
        self._attr_native_min_value, self._attr_native_max_value = OBSERVED_BOUNDS[index]

    @property
    def native_value(self):
        if self.controller.status is None:
            return None
        value = self.controller.status.positions[self._index]
        if self.native_min_value <= value <= self.native_max_value:
            return value
        return None

    async def async_set_native_value(self, value: float) -> None:
        if not math.isfinite(value) or value != int(value):
            raise HomeAssistantError("Choose a whole position step.")
        await self.execute(
            self.controller.set_position(self._index, int(value)), request=int(value)
        )
