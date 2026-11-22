"""Visible, truthful diagnostics and memory-lock behavior at the HA boundary."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import status_with
from homeassistant.exceptions import HomeAssistantError

from custom_components.svago.activity import ActionActivity
from custom_components.svago.button import BUTTONS, MemoryButton, PresetButton, SvagoButton
from custom_components.svago.connection import Connection
from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.entity import SvagoEntity
from custom_components.svago.number import PositionNumber
from custom_components.svago.select import HeatLevelSelect, MassageSelect, TimerSelect
from custom_components.svago.sensor import (
    ConnectionStatusSensor,
    LastActionSensor,
    LastProblemSensor,
    LastResponseSensor,
    PositionSensor,
    async_setup_entry,
)
from custom_components.svago.switch import HeatSwitch, MemorySetSwitch


@pytest.fixture
def live_connection(chair):
    connection = Connection(SimpleNamespace(), "02:00:00:00:00:AB")
    connection.controller = chair.controller
    connection._client = SimpleNamespace(is_connected=True)
    connection._write_char = SimpleNamespace(max_write_without_response_size=27)
    connection._notifications_subscribed = True
    connection._setup_complete = True
    connection._last_status_received = datetime.now(UTC)
    connection.memories = SimpleNamespace(get=lambda slot: object())
    chair.time += 2.1
    chair.controller.accept_status(status_with())
    assert connection.controls_ready and connection.controller.can_save_memory
    return connection


def test_newest_action_wins_even_if_older_action_finishes_later():
    activity = ActionActivity()
    older = activity.begin("Backrest upright")
    newer = activity.begin("Stop movement")
    activity.finish(newer, "completed")
    activity.finish(older, "superseded")
    assert activity.last_action["action"] == "Stop movement"
    assert activity.last_action["outcome"] == "completed"
    assert activity.snapshot()["history"][0]["outcome"] == "superseded"


def test_problem_survives_bounded_history_rollover_and_exports_are_independent():
    activity = ActionActivity()
    failed = activity.begin("Heat", "On")
    activity.finish(failed, "failed", "The chair did not confirm heat.")
    for _ in range(30):
        action = activity.begin("Footrest raise")
        activity.finish(action, "completed")
    exported = activity.snapshot()
    assert len(exported["history"]) == 20
    assert exported["requests_total"] == 31
    assert exported["outcomes"]["failed"] == 1
    assert exported["last_problem"]["action"] == "Heat"
    exported["last_problem"]["error"] = "edited"
    exported["history"][-1]["action"] = "edited"
    assert activity.last_problem["error"] == "The chair did not confirm heat."
    assert activity.last_action["action"] == "Footrest raise"
    assert "_started_monotonic" not in str(activity.snapshot())


async def test_limit_press_has_no_toast_or_write_and_visible_result(live_connection, chair):
    chair.controller.accept_status(status_with(positions=(5, 0, 16, 28)))
    button = SvagoButton(live_connection, "back_upright", "Backrest upright", 1, -1)
    await button.async_press()
    assert chair.writes == []
    assert LastActionSensor(live_connection).native_value == "Already at limit"
    assert LastActionSensor(live_connection).extra_state_attributes["action"] == "Backrest upright"
    assert LastProblemSensor(live_connection).native_value == "None"


async def test_real_failure_remains_a_service_error_and_durable_problem(live_connection):
    entity = SvagoEntity(live_connection, "heat", "Heat")
    action = AsyncMock(side_effect=ControlError("The chair did not confirm heat."))
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await entity.execute(action(), request="On")
    assert LastActionSensor(live_connection).native_value == "Failed"
    problem = LastProblemSensor(live_connection)
    assert problem.native_value == "The chair did not confirm heat."
    assert problem.extra_state_attributes["request"] == "On"
    assert ConnectionStatusSensor(live_connection).native_value == "Ready"


async def test_superseded_and_cancelled_are_recorded_without_inventing_success(live_connection):
    entity = SvagoEntity(live_connection, "back_recline", "Backrest recline")
    await entity.execute(AsyncMock(side_effect=CommandSuperseded)())
    assert LastActionSensor(live_connection).native_value == "Replaced by newer request"
    with pytest.raises(asyncio.CancelledError):
        await entity.execute(AsyncMock(side_effect=asyncio.CancelledError)())
    assert LastActionSensor(live_connection).native_value == "Cancelled"
    assert LastProblemSensor(live_connection).native_value == "None"


async def test_memory_arm_locks_all_other_controls_and_cancel_unlocks(live_connection, chair):
    connection = live_connection
    controls = [
        *(SvagoButton(connection, *item) for item in BUTTONS),
        PresetButton(connection, "zgr", "ZGR"),
        *(
            PositionNumber(connection, i, name)
            for i, name in enumerate(("headrest", "backrest", "seat", "footrest"))
        ),
        HeatSwitch(connection, "heat", "Heat"),
        MassageSelect(connection, "massage", "Massage"),
        HeatLevelSelect(connection, "seat"),
        TimerSelect(connection, "heat"),
        TimerSelect(connection, "massage"),
    ]
    switch = MemorySetSwitch(connection)
    assert all(control.available for control in controls)
    await switch.async_turn_on()
    assert all(not control.available for control in controls)
    assert all(MemoryButton(connection, slot).available for slot in (1, 2, 3))
    assert switch.available and switch.is_on
    assert PositionSensor(connection, 0, "headrest").available
    assert ConnectionStatusSensor(connection).available
    with pytest.raises(HomeAssistantError, match="Finish or cancel Set memory"):
        await controls[0].async_press()
    assert chair.writes == []
    await switch.async_turn_off()
    assert all(control.available for control in controls)


async def test_memory_lock_remains_during_save_and_unlocks_afterward(live_connection):
    connection = live_connection
    switch = MemorySetSwitch(connection)
    heat = HeatSwitch(connection, "heat", "Heat")
    await switch.async_turn_on()
    entered, release = asyncio.Event(), asyncio.Event()

    async def save(_slot):
        entered.set()
        await release.wait()

    connection.save_memory = save
    task = asyncio.create_task(MemoryButton(connection, 1).async_press())
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert not switch.is_on and not heat.available
    release.set()
    await task
    assert heat.available and not connection.controls_locked


async def test_default_diagnostics_and_optional_last_response_stay_readable_offline(
    live_connection,
):
    connection = live_connection
    add = Mock()
    await async_setup_entry(None, SimpleNamespace(runtime_data=connection), add)
    entities = add.call_args.args[0]
    enabled = [entity for entity in entities if entity.entity_registry_enabled_default]
    assert {entity.name for entity in enabled} == {
        "Connection status",
        "Last action",
        "Last problem",
        "Heat continuous status",
        "Massage continuous status",
    }
    timestamp = LastResponseSensor(connection).native_value
    connection._client.is_connected = False
    for entity in entities[:6]:
        assert entity.available
    assert ConnectionStatusSensor(connection).native_value == "Disconnected"
    assert LastResponseSensor(connection).native_value == timestamp
    assert not HeatSwitch(connection, "heat", "Heat").available
    attrs = ConnectionStatusSensor(connection).extra_state_attributes
    assert "current_status_age_seconds" not in attrs and "last_status_received" not in attrs
