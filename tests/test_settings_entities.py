"""Confirmed heat and timer choices exposed through actual HA select entities."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import status_with
from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError

from custom_components.svago.controller import ControlError
from custom_components.svago.select import HeatLevelSelect, TimerSelect, async_setup_entry

ADDRESS = "02:00:00:00:00:AB"


def connection_stub():
    controller = SimpleNamespace(
        status=status_with(),
        available=True,
        set_heat_level=AsyncMock(),
        set_timer=AsyncMock(),
        massage_continuous=False,
        set_massage_continuous=AsyncMock(),
        heat_continuous=False,
        set_heat_continuous=AsyncMock(),
    )
    return SimpleNamespace(controller=controller, address=ADDRESS)


async def test_select_platform_adds_five_settings_and_preserves_massage_identity():
    connection = connection_stub()
    add_entities = Mock()
    await async_setup_entry(None, SimpleNamespace(runtime_data=connection), add_entities)
    entities = add_entities.call_args.args[0]
    assert len(entities) == 6
    assert all(isinstance(entity, SelectEntity) for entity in entities)
    assert {entity.unique_id: entity.name for entity in entities} == {
        f"{ADDRESS}_massage": "Massage",
        f"{ADDRESS}_backrest_heat": "Backrest heat",
        f"{ADDRESS}_seat_heat": "Seat heat",
        f"{ADDRESS}_footrest_heat": "Footrest heat",
        f"{ADDRESS}_heat_timer": "Heat timer",
        f"{ADDRESS}_massage_timer": "Massage timer",
    }
    connection.controller.set_heat_level.assert_not_awaited()
    connection.controller.set_timer.assert_not_awaited()
    connection.controller.set_massage_continuous.assert_not_awaited()
    connection.controller.set_heat_continuous.assert_not_awaited()


@pytest.mark.parametrize("zone,offset", [("backrest", 20), ("seat", 21), ("footrest", 22)])
async def test_heat_select_reads_actual_zone_and_delegates_without_optimistic_update(zone, offset):
    connection = connection_stub()
    connection.controller.status = status_with(**{str(offset): 1})
    entity = HeatLevelSelect(connection, zone)
    assert entity.options == ["Off", "Low", "Medium", "High"]
    assert entity.current_option == "Low"
    assert entity.icon == "mdi:radiator"
    await entity.async_select_option("High")
    connection.controller.set_heat_level.assert_awaited_once_with(zone, "High")
    assert entity.current_option == "Low"
    connection.controller.status = status_with(**{str(offset): 3})
    assert entity.current_option == "High"


@pytest.mark.parametrize(
    "kind,options,initial,target,offset,seconds",
    [
        (
            "heat",
            ["60 minutes", "120 minutes", "180 minutes", "Continuous"],
            "60 minutes",
            120,
            8,
            7200,
        ),
        (
            "massage",
            ["15 minutes", "30 minutes", "45 minutes", "Continuous"],
            "45 minutes",
            30,
            6,
            1800,
        ),
    ],
)
async def test_timer_select_uses_minutes_and_waits_for_actual_readback(
    kind, options, initial, target, offset, seconds
):
    connection = connection_stub()
    entity = TimerSelect(connection, kind)
    assert entity.options == options
    assert entity.current_option == initial
    assert entity.icon == "mdi:timer-outline"
    await entity.async_select_option(f"{target} minutes")
    connection.controller.set_timer.assert_awaited_once_with(kind, target)
    assert entity.current_option == initial
    connection.controller.status = status_with(
        **{str(offset): seconds >> 8, str(offset + 1): seconds & 0xFF}
    )
    assert entity.current_option == f"{target} minutes"


@pytest.mark.parametrize("kind,initial", [("heat", "60 minutes"), ("massage", "15 minutes")])
async def test_continuous_selection_uses_controller_policy_not_timer_value(kind, initial):
    connection = connection_stub()
    connection.controller.status = status_with(massage=0, heat=False, **{"6": 3, "7": 132})
    entity = TimerSelect(connection, kind)
    assert entity.current_option == initial

    await entity.async_select_option("Continuous")
    getattr(connection.controller, f"set_{kind}_continuous").assert_awaited_once_with()
    other = "massage" if kind == "heat" else "heat"
    getattr(connection.controller, f"set_{other}_continuous").assert_not_awaited()
    connection.controller.set_timer.assert_not_awaited()
    # Selection is reported only once the controller has enabled it, including
    # when it is armed while that function is still Off.
    assert entity.current_option == initial
    setattr(connection.controller, f"{kind}_continuous", True)
    assert entity.current_option == "Continuous"


@pytest.mark.parametrize("kind,target,offset", [("heat", 120, 8), ("massage", 30, 6)])
async def test_selecting_standard_timer_delegates_exit_from_continuous(kind, target, offset):
    connection = connection_stub()
    setattr(connection.controller, f"{kind}_continuous", True)
    entity = TimerSelect(connection, kind)
    await entity.async_select_option(f"{target} minutes")
    connection.controller.set_timer.assert_awaited_once_with(kind, target)
    connection.controller.set_massage_continuous.assert_not_awaited()
    connection.controller.set_heat_continuous.assert_not_awaited()
    assert entity.current_option == "Continuous"
    setattr(connection.controller, f"{kind}_continuous", False)
    seconds = target * 60
    connection.controller.status = status_with(
        **{str(offset): seconds >> 8, str(offset + 1): seconds & 0xFF}
    )
    assert entity.current_option == f"{target} minutes"


@pytest.mark.parametrize("continuous_kind", ["heat", "massage"])
def test_continuous_display_is_independent_for_each_function(continuous_kind):
    connection = connection_stub()
    setattr(connection.controller, f"{continuous_kind}_continuous", True)
    heat = TimerSelect(connection, "heat")
    massage = TimerSelect(connection, "massage")
    assert heat.current_option == ("Continuous" if continuous_kind == "heat" else "60 minutes")
    assert massage.current_option == (
        "Continuous" if continuous_kind == "massage" else "45 minutes"
    )


@pytest.mark.parametrize("kind,initial", [("heat", "60 minutes"), ("massage", "45 minutes")])
async def test_continuous_failure_is_reported_without_changing_displayed_selection(kind, initial):
    connection = connection_stub()
    getattr(connection.controller, f"set_{kind}_continuous").side_effect = ControlError(
        "Setting not confirmed"
    )
    entity = TimerSelect(connection, kind)
    with pytest.raises(HomeAssistantError, match="Setting not confirmed"):
        await entity.async_select_option("Continuous")
    assert entity.current_option == initial


@pytest.mark.parametrize("zone,offset", [("backrest", 20), ("seat", 21), ("footrest", 22)])
@pytest.mark.parametrize("raw", [4, 0xFF])
def test_unknown_heat_level_does_not_invent_an_option(zone, offset, raw):
    connection = connection_stub()
    connection.controller.status = status_with(**{str(offset): raw})
    assert HeatLevelSelect(connection, zone).current_option is None


@pytest.mark.parametrize("kind,offset", [("heat", 8), ("massage", 6)])
@pytest.mark.parametrize("seconds", [0, 1, 36000, 65535])
def test_unmapped_timer_does_not_mean_off_or_unlimited(kind, offset, seconds):
    connection = connection_stub()
    connection.controller.status = status_with(
        **{str(offset): seconds >> 8, str(offset + 1): seconds & 0xFF}
    )
    entity = TimerSelect(connection, kind)
    assert entity.current_option is None
    assert "Off" not in entity.options and "Unlimited" not in entity.options


@pytest.mark.parametrize("option", ["Warm", "high", "4", ""])
async def test_invalid_heat_selection_never_reaches_controller(option):
    connection = connection_stub()
    with pytest.raises(HomeAssistantError, match="Choose Off"):
        await HeatLevelSelect(connection, "seat").async_select_option(option)
    connection.controller.set_heat_level.assert_not_called()


@pytest.mark.parametrize("option", ["240 minutes", "0 minutes", "Unlimited", "120", ""])
async def test_unconfirmed_timer_selection_never_reaches_controller(option):
    connection = connection_stub()
    with pytest.raises(HomeAssistantError, match="listed timer"):
        await TimerSelect(connection, "heat").async_select_option(option)
    connection.controller.set_timer.assert_not_called()


def test_setting_entities_follow_connection_freshness_and_missing_status(chair):
    connection = SimpleNamespace(controller=chair.controller, address=ADDRESS)
    entities = [HeatLevelSelect(connection, "backrest"), TimerSelect(connection, "heat")]
    assert all(entity.available for entity in entities)
    chair.time += 4
    assert all(not entity.available for entity in entities)
    chair.controller.set_connected(False)
    assert all(not entity.available and entity.current_option is None for entity in entities)


@pytest.mark.parametrize("kind", ["heat_level", "timer"])
async def test_controller_failure_is_reported_as_home_assistant_error(kind):
    connection = connection_stub()
    if kind == "heat_level":
        connection.controller.set_heat_level.side_effect = ControlError("Setting not confirmed")
        entity = HeatLevelSelect(connection, "seat")
        option = "High"
    else:
        connection.controller.set_timer.side_effect = ControlError("Setting not confirmed")
        entity = TimerSelect(connection, "heat")
        option = "120 minutes"
    with pytest.raises(HomeAssistantError, match="Setting not confirmed"):
        await entity.async_select_option(option)
