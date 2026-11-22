"""Chair settings and Home Assistant's continuous timer options."""

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError

from .entity import SvagoEntity
from .protocol import HEAT_LEVELS, HEAT_ZONES, MASSAGE_MODES, TIMER_OPTIONS

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    connection = entry.runtime_data
    async_add_entities(
        [
            MassageSelect(connection, "massage", "Massage"),
            *(HeatLevelSelect(connection, zone) for zone in HEAT_ZONES),
            *(TimerSelect(connection, kind) for kind in TIMER_OPTIONS),
        ]
    )


class MassageSelect(SvagoEntity, SelectEntity):
    _attr_options = list(MASSAGE_MODES)
    _attr_icon = "mdi:massage"

    @property
    def current_option(self):
        return self.controller.status.massage if self.controller.status else None

    async def async_select_option(self, option: str) -> None:
        await self.execute(self.controller.set_massage(option), request=option)


class HeatLevelSelect(SvagoEntity, SelectEntity):
    _attr_options = list(HEAT_LEVELS)
    _attr_icon = "mdi:radiator"

    def __init__(self, connection, zone: str):
        super().__init__(connection, f"{zone}_heat", f"{zone.title()} heat")
        self._zone = zone

    @property
    def current_option(self):
        status = self.controller.status
        return status.heat_level(self._zone) if status else None

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise HomeAssistantError("Choose Off, Low, Medium or High.")
        await self.execute(self.controller.set_heat_level(self._zone, option), request=option)


class TimerSelect(SvagoEntity, SelectEntity):
    _attr_icon = "mdi:timer-outline"

    def __init__(self, connection, kind: str):
        super().__init__(connection, f"{kind}_timer", f"{kind.title()} timer")
        self._kind = kind
        self._minutes = {f"{minutes} minutes": minutes for minutes in TIMER_OPTIONS[kind]}
        self._attr_options = [*self._minutes, "Continuous"]

    @property
    def current_option(self):
        continuous = (
            self.controller.heat_continuous
            if self._kind == "heat"
            else self.controller.massage_continuous
        )
        if continuous:
            return "Continuous"
        status = self.controller.status
        minutes = status.timer_minutes(self._kind) if status else None
        return f"{minutes} minutes" if minutes is not None else None

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise HomeAssistantError("Choose one of the listed timer durations.")
        if option == "Continuous":
            select_continuous = (
                self.controller.set_heat_continuous
                if self._kind == "heat"
                else self.controller.set_massage_continuous
            )
            await self.execute(select_continuous(), request=option)
            return
        await self.execute(
            self.controller.set_timer(self._kind, self._minutes[option]), request=option
        )
