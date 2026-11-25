"""Expose a clearly labelled, read-only power estimate at the HA boundary."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import status_with
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import EntityCategory

from custom_components.svago.connection import Connection
from custom_components.svago.diagnostics import async_get_config_entry_diagnostics
from custom_components.svago.sensor import WiredControllerEstimatedSensor, async_setup_entry

ADDRESS = "00:11:22:33:44:55"


@pytest.fixture
async def power_connection(chair):
    connection = Connection(SimpleNamespace(), ADDRESS)
    connection.controller = chair.controller
    connection._client = SimpleNamespace(is_connected=True)
    connection._write_char = SimpleNamespace(max_write_without_response_size=27)
    connection._notifications_subscribed = True
    connection._setup_complete = True
    yield connection
    await chair.controller.cancel_active()


async def test_power_estimate_is_registered_as_default_diagnostic_without_writes(
    power_connection, chair
):
    add = Mock()
    await async_setup_entry(None, SimpleNamespace(runtime_data=power_connection), add)
    matches = [
        entity
        for entity in add.call_args.args[0]
        if isinstance(entity, WiredControllerEstimatedSensor)
    ]
    assert len(matches) == 1
    sensor = matches[0]
    assert isinstance(sensor, SensorEntity)
    assert sensor.name == "Wired controller (estimated)"
    assert sensor.unique_id == f"{ADDRESS}_wired_controller_estimated"
    assert WiredControllerEstimatedSensor(power_connection).unique_id == sensor.unique_id
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.entity_registry_enabled_default
    assert sensor.device_class is SensorDeviceClass.ENUM
    assert sensor.options == ["On", "Off"]
    assert sensor.icon == "mdi:power"
    assert sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["estimated"] is True
    assert sensor.extra_state_attributes["physical_controls_observed"] is False
    assert chair.writes == []


async def test_acknowledged_wake_and_completed_home_have_matching_ui_and_export(
    power_connection, chair
):
    sensor = WiredControllerEstimatedSensor(power_connection)
    await chair.controller.turn_on()
    assert sensor.native_value == "On"
    assert sensor.extra_state_attributes["basis"] == "HA wake command acknowledged"

    async def assert_snapshot(expected):
        writes = len(chair.writes)
        exported = await async_get_config_entry_diagnostics(
            None, SimpleNamespace(runtime_data=power_connection)
        )
        estimate = exported["wired_controller_power_estimate"]
        assert estimate == {"state": expected, **sensor.extra_state_attributes}
        assert set(estimate) == {"state", "estimated", "basis", "physical_controls_observed"}
        assert sensor.native_value == expected
        assert len(chair.writes) == writes
        estimate["state"] = "modified export"
        assert sensor.native_value == expected

    await assert_snapshot("On")
    chair.on_sleep = lambda: chair.controller.accept_status(
        status_with(positions=(0, 0, 0, 0), heat=False, massage=0)
    )
    await chair.controller.return_home()
    assert sensor.extra_state_attributes["basis"] == "HA Return to neutral completed"
    await assert_snapshot("Off")


@pytest.mark.parametrize(
    "unready",
    ["transport_disconnected", "setup_pending", "not_subscribed", "small_command", "closed"],
)
async def test_not_ready_masks_estimate_in_both_ui_and_export_before_callback(
    power_connection, chair, unready
):
    await chair.controller.turn_on()
    original = dict(chair.controller.wired_controller_power_estimate)
    assert original["state"] == "On"
    if unready == "transport_disconnected":
        power_connection._client.is_connected = False
    elif unready == "setup_pending":
        power_connection._setup_complete = False
    elif unready == "not_subscribed":
        power_connection._notifications_subscribed = False
    elif unready == "small_command":
        power_connection._write_char.max_write_without_response_size = 20
    else:
        power_connection._closed = True

    sensor = WiredControllerEstimatedSensor(power_connection)
    writes = len(chair.writes)
    assert sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["basis"] == "Connection is not ready"
    assert power_connection.diagnostics()["wired_controller_power_estimate"] == {
        "state": None,
        **sensor.extra_state_attributes,
    }
    assert chair.controller.wired_controller_power_estimate == original
    assert len(chair.writes) == writes


async def test_disconnect_reconnect_and_unsolicited_status_remain_unknown(power_connection, chair):
    sensor = WiredControllerEstimatedSensor(power_connection)
    await chair.controller.turn_on()
    assert sensor.native_value == "On"
    chair.controller.set_connected(False)
    assert sensor.available
    assert sensor.native_value is None
    chair.controller.set_connected(True)
    chair.controller.accept_status(status_with(**{"5": 0x11}))
    writes = len(chair.writes)
    assert power_connection.controls_ready
    assert sensor.native_value is None
    assert power_connection.diagnostics()["wired_controller_power_estimate"]["state"] is None
    assert len(chair.writes) == writes


@pytest.mark.parametrize("unusable", [False, True], ids=["stale", "unusable"])
async def test_bad_feedback_is_unknown_then_stays_unknown_after_recovery(
    power_connection, chair, unusable
):
    sensor = WiredControllerEstimatedSensor(power_connection)
    await chair.controller.turn_on()
    if unusable:
        chair.controller.accept_status(status_with(**{"19": 0}))
    else:
        chair.time += 4
    assert sensor.available
    assert sensor.native_value is None
    assert power_connection.diagnostics()["wired_controller_power_estimate"]["state"] is None
    chair.controller.accept_status(status_with(**{"5": 0x11}))
    assert power_connection.controls_ready
    assert sensor.native_value is None


async def test_ordinary_reports_do_not_change_attributes_or_send_commands(power_connection, chair):
    sensor = WiredControllerEstimatedSensor(power_connection)
    await chair.controller.turn_on()
    before = dict(sensor.extra_state_attributes)
    writes = len(chair.writes)
    for _ in range(20):
        chair.time += 0.1
        chair.controller.accept_status(chair.controller.status)
        assert sensor.native_value == "On"
        assert sensor.extra_state_attributes == before
    power_connection._memory_save_armed = True
    assert sensor.available and sensor.native_value == "On"
    assert sensor.extra_state_attributes == before
    assert len(chair.writes) == writes
