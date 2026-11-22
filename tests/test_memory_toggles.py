"""One Set memory switch chooses save mode for all three Memory buttons."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import status_with
from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from custom_components.svago.button import MemoryButton
from custom_components.svago.connection import Connection
from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.switch import MemorySetSwitch, async_setup_entry

ADDRESS = "02:00:00:00:00:AB"


def connection_stub():
    connection = Connection(SimpleNamespace(), ADDRESS)
    ready_transport(connection)
    connection.controller = SimpleNamespace(
        available=True,
        connected=True,
        status=status_with(),
        can_save_memory=True,
        arm_memory_save=Mock(),
    )
    connection.memories = {}
    connection.save_memory = AsyncMock()
    connection.recall_memory = AsyncMock()
    connection._write = AsyncMock()
    return connection


def ready_transport(connection):
    connection._client = SimpleNamespace(is_connected=True)
    connection._write_char = SimpleNamespace(max_write_without_response_size=256)
    connection._notifications_subscribed = True
    connection._setup_complete = True


async def test_one_set_memory_switch_starts_off_and_arming_does_not_save_or_move():
    connection = connection_stub()
    add_entities = Mock()
    await async_setup_entry(None, SimpleNamespace(runtime_data=connection), add_entities)
    entities = add_entities.call_args.args[0]
    assert len(entities) == 2
    entity = entities[1]
    assert isinstance(entity, SwitchEntity)
    assert entity.name == "Set memory"
    assert entity.unique_id == f"{ADDRESS}_set_memory"
    assert not entity.is_on
    assert not connection.controls_locked
    await entity.async_turn_on()
    assert entity.is_on
    assert connection.controls_locked
    connection.controller.arm_memory_save.assert_called_once_with()
    await entity.async_turn_off()
    assert not entity.is_on
    assert not connection.controls_locked
    connection.save_memory.assert_not_called()
    connection.recall_memory.assert_not_called()
    connection._write.assert_not_called()


@pytest.mark.parametrize("slot", [1, 2, 3])
async def test_armed_empty_slot_saves_once_and_following_press_recalls(slot):
    connection = connection_stub()
    switch = MemorySetSwitch(connection)
    button = MemoryButton(connection, slot)
    assert not button.available
    await switch.async_turn_on()
    assert switch.is_on and button.available
    await button.async_press()
    assert not switch.is_on
    connection.save_memory.assert_awaited_once_with(slot)
    connection.recall_memory.assert_not_called()
    connection.memories[slot] = object()
    assert button.available
    await button.async_press()
    connection.recall_memory.assert_awaited_once_with(slot)
    connection.save_memory.assert_awaited_once_with(slot)


async def test_turning_arm_off_cancels_saving_intent_without_touching_profile():
    connection = connection_stub()
    profile = object()
    connection.memories[2] = profile
    switch = MemorySetSwitch(connection)
    await switch.async_turn_on()
    await switch.async_turn_off()
    await MemoryButton(connection, 2).async_press()
    connection.recall_memory.assert_awaited_once_with(2)
    connection.save_memory.assert_not_called()
    assert connection.memories[2] is profile


async def test_save_attempt_disarms_and_requires_rearming_after_failure():
    connection = connection_stub()
    connection.memories[3] = object()
    connection.save_memory.side_effect = ControlError("Wait for stable chair feedback")
    switch = MemorySetSwitch(connection)
    await switch.async_turn_on()
    with pytest.raises(HomeAssistantError, match="stable chair"):
        await MemoryButton(connection, 3).async_press()
    assert not switch.is_on
    assert not connection.memory_save_in_progress()
    connection.recall_memory.assert_not_called()
    assert switch.available


@pytest.mark.parametrize("second_slot", [1, 2, 3])
async def test_any_memory_press_cannot_recall_or_resave_during_persistence(second_slot):
    connection = connection_stub()
    connection.memories = {1: object(), 2: object(), 3: object()}
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def save(_slot):
        entered.set()
        await gate.wait()

    connection.save_memory.side_effect = save
    switch = MemorySetSwitch(connection)
    button = MemoryButton(connection, 1)
    await switch.async_turn_on()
    first = asyncio.create_task(button.async_press())
    await entered.wait()
    assert not switch.is_on
    assert connection.controls_locked
    assert not switch.available
    assert all(not MemoryButton(connection, slot).available for slot in (1, 2, 3))
    await MemoryButton(connection, second_slot).async_press()
    await switch.async_turn_off()
    with pytest.raises(HomeAssistantError, match="still being saved"):
        await switch.async_turn_on()
    connection.save_memory.assert_awaited_once_with(1)
    connection.recall_memory.assert_not_called()
    gate.set()
    await first
    assert not switch.is_on
    assert not connection.controls_locked
    assert switch.available and button.available


async def test_cancelled_save_intent_cannot_fall_through_to_recall():
    connection = connection_stub()
    with pytest.raises(ControlError, match="Set memory was cancelled"):
        await connection.press_memory(1, save_intent=True)
    connection.save_memory.assert_not_awaited()
    connection.recall_memory.assert_not_awaited()


async def test_new_save_arm_discards_prior_recall_intent():
    connection = connection_stub()
    await connection.set_memory_save_armed(True)
    with pytest.raises(CommandSuperseded):
        await connection.press_memory(1, save_intent=False)
    assert connection.memory_save_armed() and connection.controls_locked
    connection.save_memory.assert_not_awaited()
    connection.recall_memory.assert_not_awaited()


async def test_cancelled_attempt_leaves_slot_disarmed_and_not_busy():
    connection = connection_stub()
    entered = asyncio.Event()

    async def save(_slot):
        entered.set()
        await asyncio.Event().wait()

    connection.save_memory.side_effect = save
    switch = MemorySetSwitch(connection)
    await switch.async_turn_on()
    task = asyncio.create_task(MemoryButton(connection, 1).async_press())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not switch.is_on
    assert not connection.memory_save_in_progress()


async def test_unavailable_connection_clears_global_arm_and_cannot_rearm():
    connection = connection_stub()
    switch = MemorySetSwitch(connection)
    await switch.async_turn_on()
    connection.controller.available = False
    connection.notify_listeners()
    assert not switch.is_on and not switch.available
    with pytest.raises(HomeAssistantError, match="Connect to the chair"):
        await switch.async_turn_on()
    connection.controller.available = True
    connection.notify_listeners()
    assert not switch.is_on
    connection.save_memory.assert_not_called()


async def test_global_arm_makes_all_empty_slots_save_destinations_and_requires_stationarity():
    connection = connection_stub()
    buttons = [MemoryButton(connection, slot) for slot in (1, 2, 3)]
    assert all(not button.available for button in buttons)
    switch = MemorySetSwitch(connection)
    await switch.async_turn_on()
    assert all(button.available for button in buttons)
    connection.controller.can_save_memory = False
    assert all(not button.available for button in buttons)
    assert switch.available  # User can still cancel the arm.
    connection.controller.can_save_memory = True
    await buttons[1].async_press()
    connection.save_memory.assert_awaited_once_with(2)
    connection.recall_memory.assert_not_called()
    assert not switch.is_on
    assert all(not button.available for button in buttons)  # Fake save did not fill a slot.


async def test_reconnect_during_store_load_cannot_save_new_session_status():
    connection = Connection(SimpleNamespace(), ADDRESS)
    ready_transport(connection)
    connection.controller.set_connected(True)
    connection.controller.accept_status(status_with())
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def load():
        entered.set()
        await gate.wait()

    connection.memories = SimpleNamespace(async_load=load, async_save=AsyncMock())
    connection.controller.snapshot_memory = Mock()
    await MemorySetSwitch(connection).async_turn_on()
    task = asyncio.create_task(MemoryButton(connection, 2).async_press())
    await entered.wait()
    connection.controller.set_connected(False)
    connection.controller.set_connected(True)
    connection.controller.accept_status(status_with())
    connection.controller.snapshot_memory.reset_mock()
    gate.set()
    with pytest.raises(HomeAssistantError, match="connection changed"):
        await task
    connection.controller.snapshot_memory.assert_not_called()
    connection.memories.async_save.assert_not_called()
    assert not connection.memory_save_armed()


async def test_newer_completed_control_during_store_load_invalidates_older_save(chair):
    connection = Connection(SimpleNamespace(), ADDRESS)
    ready_transport(connection)
    connection.controller = chair.controller
    connection.controller._changed = connection.notify_listeners
    chair.time += 2
    chair.controller.accept_status(chair.controller.status)
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def load():
        entered.set()
        await gate.wait()

    connection.memories = SimpleNamespace(async_load=load, async_save=AsyncMock())
    await MemorySetSwitch(connection).async_turn_on()
    task = asyncio.create_task(MemoryButton(connection, 2).async_press())
    await entered.wait()
    await chair.controller.set_heat(False)
    chair.time += 2
    chair.controller.accept_status(chair.controller.status)
    assert chair.controller.can_save_memory  # A later stable state must not replace the old save.
    chair.controller.snapshot_memory = Mock(wraps=chair.controller.snapshot_memory)
    gate.set()
    with pytest.raises(HomeAssistantError, match="newer chair adjustment"):
        await task
    chair.controller.snapshot_memory.assert_not_called()
    connection.memories.async_save.assert_not_called()
    assert not connection.memory_save_armed()
