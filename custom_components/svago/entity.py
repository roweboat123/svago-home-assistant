"""Shared push-state entity behavior."""

import asyncio
from collections.abc import Awaitable
from inspect import iscoroutine

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, Entity

from .activity import ActionActivity
from .connection import Connection
from .const import DOMAIN, NAME
from .controller import CommandSuperseded, ControlError


class SvagoEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, connection: Connection, key: str, name: str) -> None:
        self.connection = connection
        self.controller = connection.controller
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{connection.address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, connection.address)},
            name=NAME,
            manufacturer="Svago",
            model="Motion Mastery BLE",
        )

    @property
    def available(self) -> bool:
        return bool(
            self.controller.available
            and getattr(self.connection, "controls_ready", True)
            and (self._is_memory_control or not getattr(self.connection, "controls_locked", False))
        )

    @property
    def _is_memory_control(self) -> bool:
        return self._key in {"set_memory", "memory_1", "memory_2", "memory_3"}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.connection.subscribe(self.async_write_ha_state))

    async def execute(self, action: Awaitable, *, request=None) -> None:
        activity = getattr(self.connection, "activity", None)
        if not isinstance(activity, ActionActivity):
            activity = None
        record = activity.begin(self._attr_name, request) if activity else None
        try:
            if not self._is_memory_control and getattr(self.connection, "controls_locked", False):
                if iscoroutine(action):
                    action.close()
                message = "Finish or cancel Set memory before using another control."
                if activity:
                    activity.finish(record, "blocked", message)
                raise HomeAssistantError(message)
            result = await action
        except CommandSuperseded:
            if activity:
                activity.finish(record, "superseded")
        except asyncio.CancelledError:
            if activity:
                activity.finish(record, "cancelled")
            raise
        except ControlError as exc:
            if activity:
                activity.finish(record, "failed", str(exc))
            raise HomeAssistantError(str(exc)) from exc
        except Exception as exc:
            if activity and record["outcome"] == "in_progress":
                activity.finish(record, "failed", str(exc) or type(exc).__name__)
            raise
        else:
            if activity:
                activity.finish(
                    record, "at_observed_limit" if result == "at_observed_limit" else "completed"
                )
