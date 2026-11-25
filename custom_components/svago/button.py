"""Directional presses, lumbar adjustments and native chair programs."""

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory

from .entity import SvagoEntity

PARALLEL_UPDATES = 0  # The controller coordinates replacements; Stop stays callable.
BUTTONS = (
    ("turn_on", "Turn on", None, None),
    ("head_in", "Headrest inward", 0, 1),
    ("head_out", "Headrest outward", 0, -1),
    ("back_recline", "Backrest recline", 1, 1),
    ("back_upright", "Backrest upright", 1, -1),
    ("foot_raise", "Footrest raise", 3, 1),
    ("foot_lower", "Footrest lower", 3, -1),
    ("lumbar_inflate", "Lumbar inflate", None, 1),
    ("lumbar_deflate", "Lumbar deflate", None, -1),
    ("stop_movement", "Stop movement", None, 0),
)
PRESET_BUTTONS = (("zgr", "ZGR"), ("lay_flat", "Lay flat"))


async def async_setup_entry(hass, entry, async_add_entities):
    connection = entry.runtime_data
    async_add_entities(
        [
            *(SvagoButton(connection, *item) for item in BUTTONS),
            *(PresetButton(connection, *item) for item in PRESET_BUTTONS),
            HomeButton(connection),
            *(MemoryButton(connection, slot) for slot in range(1, 4)),
        ]
    )


class SvagoButton(SvagoEntity, ButtonEntity):
    def __init__(self, connection, key, name, index, direction):
        super().__init__(connection, key, name)
        self._attr_entity_category = EntityCategory.CONFIG if key == "turn_on" else None
        self._index = index
        self._direction = direction
        self._attr_icon = (
            "mdi:power"
            if direction is None
            else "mdi:stop-circle-outline"
            if direction == 0
            else "mdi:seat"
        )

    async def async_press(self) -> None:
        if self._direction is None:
            action = self.controller.turn_on()
        elif self._index is not None:
            action = self.controller.step_position(self._index, self._direction)
        elif self._direction:
            action = self.controller.adjust_lumbar(self._direction == 1)
        else:
            action = self.controller.stop_movement()
        await self.execute(action)


class PresetButton(SvagoEntity, ButtonEntity):
    _attr_icon = "mdi:seat"

    def __init__(self, connection, preset: str, name: str):
        super().__init__(connection, f"preset_{preset}", name)
        self._preset = preset

    async def async_press(self) -> None:
        await self.execute(self.controller.select_preset(self._preset))


class HomeButton(SvagoEntity, ButtonEntity):
    """Invoke the captured native shutdown/Home operation."""

    _attr_icon = "mdi:seat"

    def __init__(self, connection):
        super().__init__(connection, "return_home", "Return to neutral")

    async def async_press(self) -> None:
        await self.execute(self.controller.return_home())


class MemoryButton(SvagoEntity, ButtonEntity):
    _attr_icon = "mdi:seat"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, connection, slot: int):
        super().__init__(connection, f"memory_{slot}", f"Memory {slot}")
        self._slot = slot

    @property
    def available(self) -> bool:
        if (
            not super().available
            or self.connection.memories is None
            or self.connection.memory_save_in_progress()
        ):
            return False
        if self.connection.memory_save_armed():
            return self.controller.can_save_memory
        return self.connection.memories.get(self._slot) is not None

    async def async_press(self) -> None:
        save_intent = self.connection.memory_save_armed()
        await self.execute(
            self.connection.press_memory(self._slot, save_intent=save_intent),
            request="Save" if save_intent else "Recall",
        )
