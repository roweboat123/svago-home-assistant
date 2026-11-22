"""Explicit first movement after standby, with fresh activation and no replay."""

import asyncio

import pytest
from conftest import status_with

from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.protocol import OBSERVED_BOUNDS


def motor_request(chair, control):
    if control == "movement":
        return chair.controller.step_position(0, 1)
    return chair.controller.set_position(0, 8)


async def waiting_for_activation(chair, control):
    """Leave the request waiting for the chair's reply, not a blocked transport."""
    entered = asyncio.Event()
    chair.activation_feedback = False
    chair.on_write = lambda packet: entered.set() if packet[5] == 0x11 else None
    task = asyncio.create_task(motor_request(chair, control))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert len(chair.writes) == 1
    assert not task.done()
    return task


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("direction", [-1, 1])
async def test_endpoint_and_matching_target_do_not_wake_standby_chair(chair, index, direction):
    positions = list(chair.controller.status.positions)
    positions[index] = OBSERVED_BOUNDS[index][1 if direction > 0 else 0]
    chair.controller.accept_status(status_with(positions=positions))

    assert await chair.controller.step_position(index, direction) == "at_observed_limit"
    await chair.controller.set_position(index, positions[index])

    assert chair.writes == []
    assert chair.controller.last_activation is None
    assert not chair.controller.busy


@pytest.mark.parametrize("control,position", [("movement", 17), ("slider", 8)])
async def test_new_activation_position_can_make_requested_move_unnecessary(
    chair, control, position
):
    task = await waiting_for_activation(chair, control)
    chair.controller.accept_status(status_with(positions=(position, 6, 16, 28), **{"5": 0x11}))

    result = await asyncio.wait_for(task, timeout=1)

    assert result == ("at_observed_limit" if control == "movement" else None)
    assert len(chair.writes) == 1  # Activation only; no direction or needless stop.
    assert chair.controller.last_activation["outcome"] == "acknowledged"
    assert not chair.controller.busy


async def test_slider_uses_latest_position_and_settings_after_activation(chair):
    task = await waiting_for_activation(chair, "slider")
    # The chair passed the target while activation was pending. The new
    # position determines direction; its latest heat setting must be retained.
    chair.controller.accept_status(
        status_with(positions=(10, 6, 16, 28), heat=False, **{"5": 0x11})
    )

    await asyncio.wait_for(task, timeout=1)

    assert chair.controller.status.positions[0] == 8
    assert len(chair.writes) == 4  # Activation, two decreases, one release.
    assert all(packet[13] == 0x81 for packet in chair.writes[1:-1])
    assert all(packet[19] == 2 for packet in chair.writes[1:])
    assert chair.writes[-1][13:17] == b"\x80" * 4


@pytest.mark.parametrize("control", ["movement", "slider"])
@pytest.mark.parametrize("position", [18, 0x82])
async def test_invalid_activation_position_blocks_direction_without_guessing(
    chair, control, position
):
    task = await waiting_for_activation(chair, control)
    chair.controller.accept_status(status_with(positions=(position, 6, 16, 28), **{"5": 0x11}))

    with pytest.raises(ControlError, match="outside|not numeric"):
        await asyncio.wait_for(task, timeout=1)

    assert len(chair.writes) == 1
    assert not chair.controller.busy


@pytest.mark.parametrize("control", ["movement", "slider"])
async def test_new_control_supersedes_movement_waiting_for_activation(chair, control):
    old = await waiting_for_activation(chair, control)
    chair.activation_feedback = True

    await chair.controller.set_position(1, 4)
    with pytest.raises(CommandSuperseded):
        await old

    assert sum(packet[5] == 0x11 for packet in chair.writes) == 2
    assert chair.writes[1][13:17] == b"\x80" * 4
    assert not any(packet[13] in (0x81, 0x82) for packet in chair.writes)
    assert chair.controller.status.positions == (5, 4, 16, 28)
    # A later response to the abandoned activation cannot revive its headrest request.
    count = len(chair.writes)
    chair.controller.accept_status(status_with(**{"5": 0x11}))
    await asyncio.sleep(0)
    assert len(chair.writes) == count


@pytest.mark.parametrize("control", ["movement", "slider"])
@pytest.mark.parametrize("connected_without_status", [False, True])
async def test_request_before_ready_is_not_saved_for_reconnect(
    chair, control, connected_without_status
):
    chair.controller.set_connected(False)
    if connected_without_status:
        chair.controller.set_connected(True)

    with pytest.raises(ControlError, match="Fresh"):
        await motor_request(chair, control)
    assert not chair.writes

    chair.controller.set_connected(True)
    chair.controller.accept_status(status_with())
    await asyncio.sleep(0)
    assert not chair.writes  # Becoming ready itself is read-only.

    await motor_request(chair, control)
    assert chair.writes[0][5] == 0x11
    assert len(chair.writes) > 1
