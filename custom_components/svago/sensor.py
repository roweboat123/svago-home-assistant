"""Visible connection and action diagnostics, plus optional raw positions."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import EntityCategory

from .activity import OUTCOME_LABELS
from .entity import SvagoEntity
from .protocol import POSITION_NAMES
from .timer_status import CONTINUOUS_STATES, continuous_status

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    connection = entry.runtime_data
    async_add_entities(
        [
            ConnectionStatusSensor(connection),
            LastResponseSensor(connection),
            LastActionSensor(connection),
            LastProblemSensor(connection),
            ContinuousStatusSensor(connection, "heat"),
            ContinuousStatusSensor(connection, "massage"),
            *(PositionSensor(connection, index, name) for index, name in enumerate(POSITION_NAMES)),
        ]
    )


class DiagnosticSensor(SvagoEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        # Diagnostic information remains readable when controls cannot be used.
        return True


class ConnectionStatusSensor(DiagnosticSensor):
    _attr_icon = "mdi:bluetooth-connect"

    def __init__(self, connection):
        super().__init__(connection, "connection_status", "Connection status")

    @property
    def native_value(self):
        return self.connection.connection_status

    @property
    def extra_state_attributes(self):
        health = self.connection.health_snapshot()
        # Keep per-notification timestamps and ages in the diagnostic export,
        # avoiding a new recorder row every second for an otherwise idle chair.
        keys = (
            "bluetooth_connected",
            "controls_ready",
            "notifications_subscribed",
            "status_usable",
            "effective_command_capacity",
            "connect_attempts",
            "successful_connections",
            "disconnect_count",
            "last_disconnect_reason",
            "last_reconnect_reason",
        )
        return {key: health[key] for key in keys}


class LastResponseSensor(DiagnosticSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:message-check-outline"

    def __init__(self, connection):
        super().__init__(connection, "last_response", "Last response")

    @property
    def native_value(self):
        return self.connection.last_status_received


class LastActionSensor(DiagnosticSensor):
    _attr_icon = "mdi:clipboard-text-clock-outline"

    def __init__(self, connection):
        super().__init__(connection, "last_action", "Last action")

    @property
    def native_value(self):
        action = self.connection.activity.last_action
        return OUTCOME_LABELS[action["outcome"]] if action else "No actions yet"

    @property
    def extra_state_attributes(self):
        return self.connection.activity.last_action or {}


class LastProblemSensor(DiagnosticSensor):
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, connection):
        super().__init__(connection, "last_problem", "Last problem")

    @property
    def native_value(self):
        problem = self.connection.activity.last_problem
        return problem.get("error", "Failed")[:255] if problem else "None"

    @property
    def extra_state_attributes(self):
        return self.connection.activity.last_problem or {}


class ContinuousStatusSensor(DiagnosticSensor):
    """Show whether a retained timer choice has an active HA renewal session."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CONTINUOUS_STATES
    _attr_icon = "mdi:timer-sync-outline"

    def __init__(self, connection, kind: str):
        if kind not in {"heat", "massage"}:
            raise ValueError("Choose heat or massage")
        super().__init__(
            connection, f"{kind}_continuous_status", f"{kind.title()} continuous status"
        )
        self._kind = kind

    def _snapshot(self) -> tuple[dict, bool]:
        diagnostics = (
            self.controller.heat_renewal_diagnostics
            if self._kind == "heat"
            else self.controller.massage_renewal_diagnostics
        )
        ready = bool(getattr(self.connection, "controls_ready", self.controller.available))
        return diagnostics, ready

    @property
    def native_value(self):
        diagnostics, ready = self._snapshot()
        return continuous_status(diagnostics, ready=ready)[0]

    @property
    def extra_state_attributes(self):
        diagnostics, ready = self._snapshot()
        # No packets or ticking timestamps: ordinary notifications leave these
        # attributes unchanged, avoiding per-second recorder entries.
        return {
            "continuous_selected": diagnostics["selected"],
            "session_active": bool(diagnostics["active"] and ready),
            "reason": continuous_status(diagnostics, ready=ready)[1],
            "continuous_timer_minutes": diagnostics["native_minutes"],
            "renewal_interval_minutes": diagnostics["interval_seconds"] / 60,
            "renewals_completed": diagnostics.get("renewal_count", 0),
        }


class PositionSensor(SvagoEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:seat"

    def __init__(self, connection, index, name):
        super().__init__(connection, f"{name}_raw", f"{name.title()} raw position")
        self._index = index

    @property
    def available(self) -> bool:
        return bool(self.controller.available and getattr(self.connection, "controls_ready", True))

    @property
    def native_value(self):
        return self.controller.status.positions[self._index] if self.controller.status else None
