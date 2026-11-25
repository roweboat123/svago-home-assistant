"""Verified neutral On packet, live-state adaptation and ordered activation."""

import asyncio

import pytest
from conftest import status_with

from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.memory_protocol import MemoryProfile
from custom_components.svago.protocol import Status, checksum

VERIFIED_ON = bytes.fromhex("7E0201001011000000000000000000000000000202020200002C7E")


def requested_control(chair, control):
    """Issue one representative control that needs activation."""
    actions = {
        "heat": lambda: chair.controller.set_heat(True),
        "massage": lambda: chair.controller.set_massage("Gentle"),
        "inflate": lambda: chair.controller.adjust_lumbar(True),
        "deflate": lambda: chair.controller.adjust_lumbar(False),
        "movement": lambda: chair.controller.step_position(0, 1),
        "slider": lambda: chair.controller.set_position(0, 8),
        "memory": lambda: chair.controller.recall_memory(
            1, MemoryProfile.from_status(status_with(positions=(2, 3, 4, 5)))
        ),
    }
    return actions[control]()


def test_activation_matches_physically_confirmed_neutral_packet():
    # Captured response to the exact message the user verified twice.
    status = Status(bytes.fromhex("7E0102001011000000000000000000000000000202020200002C7E"))
    assert status.activation_command() == VERIFIED_ON


@pytest.mark.parametrize("heat", [True, False])
@pytest.mark.parametrize("massage", [0, 1, 5])
def test_activation_preserves_reported_positions_and_settings(heat, massage):
    before = status_with(positions=(9, 6, 22, 18), heat=heat, massage=massage, **{"7": 1})
    packet = before.activation_command()
    assert packet[5] == 0x11
    assert packet[13:17] == bytes((9, 6, 22, 18))
    assert packet[18] == 0
    assert packet[6:18] == before.raw[6:18]
    assert packet[19:25] == before.raw[19:25]
    assert packet[25] == checksum(packet)


@pytest.mark.parametrize(
    "control", ["heat", "massage", "inflate", "deflate", "movement", "slider", "memory"]
)
async def test_activation_precedes_dependent_command_every_time(chair, control):
    for _ in range(2):
        # A physical remote can turn the chair Off without changing FFF1.
        # Restore a position below the target so this is another requested move.
        chair.controller.accept_status(status_with(**{"5": 0x11}))
        start = len(chair.writes)
        await requested_control(chair, control)
        packets = chair.writes[start:]
        assert packets[0][5] == 0x11
        assert len(packets) > 1
        assert all(p[5] == 0 for p in packets[1:])
        assert chair.controller.last_activation["outcome"] == "acknowledged"


@pytest.mark.parametrize("control", ["heat_off", "massage_off", "stop"])
async def test_off_and_stop_controls_do_not_activate(chair, control):
    actions = {
        "heat_off": lambda: chair.controller.set_heat(False),
        "massage_off": lambda: chair.controller.set_massage("Off"),
        "stop": chair.controller.stop_movement,
    }
    await actions[control]()
    assert all(p[5] == 0 for p in chair.writes)
    assert chair.controller.last_activation is None


@pytest.mark.parametrize("control", ["massage", "movement", "slider", "memory"])
async def test_old_activation_status_is_not_enough_without_new_response(chair, control):
    chair.controller.accept_status(status_with(**{"5": 0x11}))
    chair.activation_feedback = False
    with pytest.raises(ControlError, match="did not acknowledge activation"):
        await requested_control(chair, control)
    assert len(chair.writes) == 1
    assert chair.writes[0][5] == 0x11  # No dependent command followed failed activation.
    assert chair.controller.last_activation["outcome"] == "failed"


async def test_waits_for_activation_response_and_uses_latest_settings(chair):
    chair.feedback = False
    task = asyncio.create_task(chair.controller.set_massage("Relax"))
    await asyncio.sleep(0)
    assert len(chair.writes) == 1 and chair.writes[0][5] == 0x11
    # An unrelated fresh status is not the requested activation response.
    chair.controller.accept_status(status_with(heat=False))
    await asyncio.sleep(0)
    assert len(chair.writes) == 1
    chair.feedback = True
    chair.controller.accept_status(status_with(heat=False, **{"5": 0x11, "8": 0, "9": 0}))
    await task
    assert len(chair.writes) == 2
    assert chair.writes[-1][19] == 2 and chair.writes[-1][8:10] == b"\x00\x00"
    assert chair.controller.status.massage == "Relax"


@pytest.mark.parametrize("control", ["inflate", "movement", "slider"])
async def test_activation_failure_never_sends_dependent_command_or_retry(chair, control):
    chair.fail_at = 1
    with pytest.raises(ControlError, match="not retried"):
        await requested_control(chair, control)
    assert len(chair.writes) == 1
    assert chair.writes[0][5] == 0x11


@pytest.mark.parametrize("control", ["massage", "movement", "slider"])
async def test_stop_during_activation_prevents_requested_control(chair, control):
    chair.activation_feedback = False
    task = asyncio.create_task(requested_control(chair, control))
    await asyncio.sleep(0)
    await chair.controller.stop_movement()
    with pytest.raises(CommandSuperseded):
        await task
    assert all(p[17] == 0 for p in chair.writes)
    assert not any(value in (0x81, 0x82) for p in chair.writes for value in p[13:17])
    assert chair.writes[-1][13:17] == b"\x80" * 4


@pytest.mark.parametrize("control", ["massage", "movement", "slider"])
async def test_disconnect_during_activation_does_not_resume_on_new_connection(chair, control):
    chair.activation_feedback = False
    task = asyncio.create_task(requested_control(chair, control))
    await asyncio.sleep(0)
    chair.controller.set_connected(False)
    chair.controller.set_connected(True)
    chair.controller.accept_status(status_with(**{"5": 0x11}))
    with pytest.raises(ControlError, match="Fresh"):
        await task
    assert len(chair.writes) == 1
    # A late old acknowledgment cannot replay the cancelled request. A new
    # explicit request performs its own activation before any dependent write.
    chair.activation_feedback = True
    await requested_control(chair, control)
    assert chair.writes[1][5] == 0x11
    assert len(chair.writes) > 2


async def test_activation_refuses_non_numeric_motor_state(chair):
    chair.controller.accept_status(status_with(positions=(0x82, 6, 16, 28)))
    with pytest.raises(ControlError, match="not numeric"):
        await chair.controller.turn_on()
    assert not chair.writes


async def test_internal_turn_on_is_idempotent(chair):
    await chair.controller.turn_on()
    await chair.controller.turn_on()
    assert len(chair.writes) == 2
    assert chair.writes[0] == chair.writes[1]
    assert chair.writes[0][5] == 0x11
