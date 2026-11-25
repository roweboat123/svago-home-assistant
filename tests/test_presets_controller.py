"""Native preset selection, fresh feedback and interruption regression tests."""

import asyncio

import pytest
from conftest import status_with

from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.protocol import PRESET_COMMANDS, Status, checksum


def reply(payload, positions=(0, 0, 0, 0)):
    raw = bytearray(payload)
    raw[1:3] = b"\x01\x02"
    raw[13:17] = bytes(positions)
    raw[25] = checksum(raw)
    return Status(bytes(raw))


def acknowledge_activation(chair, payload):
    if payload[5] == 0x11:
        chair.controller.accept_status(reply(payload, chair.controller.status.positions))


@pytest.mark.parametrize("name", ["zgr", "lay_flat"])
async def test_native_preset_activates_then_sends_exact_capture_once(chair, name):
    chair.controller.accept_status(status_with(positions=(6, 6, 38, 17), heat=False))
    await chair.controller.select_preset(name)
    assert len(chair.writes) == 2
    assert chair.writes[0][5] == 0x11
    assert chair.writes[0][13:17] == bytes((6, 6, 38, 17))
    assert chair.writes[0][19] == 2
    assert chair.writes[1] == PRESET_COMMANDS[name]
    # The simulated initial acknowledgment holds the initial positions. Selection
    # returns now; it must neither fabricate a final posture nor wait for travel.
    assert chair.controller.status.positions == (6, 6, 38, 17)
    assert chair.time == 0
    assert not chair.controller.busy
    trace = chair.controller.preset_requests[-1]
    assert trace["outcome"] == "accepted"
    assert trace["command_hex"] == PRESET_COMMANDS[name].hex().upper()
    assert chair.controller.last_activation["reason"] == "preset"


@pytest.mark.parametrize("name", ["unknown", "memory_1", None, []])
async def test_invalid_preset_has_no_side_effects(chair, name):
    with pytest.raises(ControlError, match="Unknown chair preset"):
        await chair.controller.select_preset(name)
    assert not chair.writes


async def test_stale_status_blocks_preset_and_activation(chair):
    chair.time += 4
    with pytest.raises(ControlError, match="Fresh"):
        await chair.controller.select_preset("zgr")
    assert not chair.writes


async def test_preset_requires_activation_acknowledgment(chair):
    chair.feedback = False
    with pytest.raises(ControlError, match="acknowledge activation"):
        await chair.controller.select_preset("zgr")
    assert len(chair.writes) == 1
    assert chair.writes[0][5] == 0x11
    assert chair.controller.preset_requests[-1]["outcome"] == "failed"


@pytest.mark.parametrize("name", ["zgr", "lay_flat"])
@pytest.mark.parametrize("failure", ["wake_write", "wake_ack", "target_write"])
async def test_native_preset_retires_continuous_only_at_target_dispatch(chair, name, failure):
    controller = chair.controller
    await controller.set_massage_continuous()
    await controller.set_massage("Gentle")
    await controller.set_heat_continuous()
    try:
        heat, massage = controller._heat_renewal_task, controller._massage_renewal_task
        assert heat is not None and massage is not None
        start = len(chair.writes)
        if failure == "wake_ack":
            chair.activation_feedback = False
        else:
            chair.fail_at = start + (1 if failure == "wake_write" else 2)
        with pytest.raises(ControlError):
            await controller.select_preset(name)
        sent = failure == "target_write"
        assert chair.writes[start:].count(PRESET_COMMANDS[name]) == int(sent)
        for kind in ("heat", "massage"):
            assert controller.timer_preferences[kind] is not sent
            assert getattr(controller, f"{kind}_renewal_diagnostics")["active"] is not sent
        if sent:
            assert chair.writes[-1][13:17] == b"\x80" * 4
            assert controller._heat_renewal_task is controller._massage_renewal_task is None
        else:
            assert controller._heat_renewal_task is heat and not heat.cancelling()
            assert controller._massage_renewal_task is massage and not massage.cancelling()
    finally:
        await controller.cancel_active()


async def test_cached_matching_preset_is_not_new_confirmation(chair):
    chair.controller.accept_status(reply(PRESET_COMMANDS["zgr"]))
    chair.feedback = False
    chair.on_write = lambda payload: acknowledge_activation(chair, payload)
    with pytest.raises(ControlError, match="did not confirm preset"):
        await chair.controller.select_preset("zgr")
    assert chair.writes.count(PRESET_COMMANDS["zgr"]) == 1
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert chair.controller.preset_requests[-1]["outcome"] == "failed"


@pytest.mark.parametrize("bad_field,bad_value", [(10, 3), (17, 0), (19, 2), (20, 2), (13, 0x82)])
async def test_unrelated_or_invalid_reply_does_not_confirm_preset(chair, bad_field, bad_value):
    chair.feedback = False

    def report(payload):
        acknowledge_activation(chair, payload)
        if payload == PRESET_COMMANDS["lay_flat"]:
            raw = bytearray(reply(payload).raw)
            raw[bad_field] = bad_value
            raw[25] = checksum(raw)
            chair.controller.accept_status(Status(bytes(raw)))

    chair.on_write = report
    with pytest.raises(ControlError, match="did not confirm preset"):
        await chair.controller.select_preset("lay_flat")
    assert chair.writes.count(PRESET_COMMANDS["lay_flat"]) == 1
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_transport_error_does_not_retry_preset_and_attempts_stop(chair):
    chair.fail_at = 2
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.select_preset("zgr")
    assert len(chair.writes) == 3
    assert chair.writes.count(PRESET_COMMANDS["zgr"]) == 1
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_stop_after_selection_stops_native_motion(chair):
    await chair.controller.select_preset("zgr")
    await chair.controller.stop_movement()
    assert all(p[13:17] == b"\x80" * 4 for p in chair.writes[2:])
    assert chair.writes.count(PRESET_COMMANDS["zgr"]) == 1
    assert not chair.controller.busy


async def test_manual_control_stops_accepted_native_program_before_direction(chair):
    await chair.controller.select_preset("zgr")
    await chair.controller.step_position(0, -1)
    assert chair.writes[2][13:17] == b"\x80" * 4
    assert chair.writes[3][5] == 0x11  # Activate only after the program was stopped.
    assert chair.writes[4][13:17] == b"\x81\x8f\x8f\x8f"
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_setting_after_accepted_preset_keeps_captured_other_options(chair):
    await chair.controller.select_preset("zgr")
    await chair.controller.set_heat_level("seat", "High")
    assert chair.writes[2][13:17] == b"\x80" * 4
    assert chair.controller.status.heat_level("seat") == "High"
    assert chair.controller.status.heat_level("backrest") == "Low"
    assert chair.controller.status.massage == "Relax"
    assert chair.controller.status.timer_minutes("heat") == 180


async def test_second_preset_stops_first_before_activation_and_selection(chair):
    await chair.controller.select_preset("zgr")
    await chair.controller.select_preset("lay_flat")
    assert len(chair.writes) == 5
    assert chair.writes[2][13:17] == b"\x80" * 4
    assert chair.writes[3][5] == 0x11
    assert chair.writes[4] == PRESET_COMMANDS["lay_flat"]


async def test_stop_interrupts_pending_selection_without_replay(chair):
    chair.feedback = False
    chair.on_write = lambda payload: acknowledge_activation(chair, payload)
    pending = asyncio.create_task(chair.controller.select_preset("zgr"))
    await asyncio.sleep(0)
    await chair.controller.stop_movement()
    with pytest.raises(CommandSuperseded):
        await pending
    assert chair.writes.count(PRESET_COMMANDS["zgr"]) == 1
    assert all(p[13:17] == b"\x80" * 4 for p in chair.writes[2:])
    assert chair.controller.preset_requests[-1]["outcome"] == "replaced_by_new_request"


async def test_external_cancellation_attempts_stop(chair):
    chair.feedback = False
    chair.on_write = lambda payload: acknowledge_activation(chair, payload)
    pending = asyncio.create_task(chair.controller.select_preset("zgr"))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert len(chair.writes) == 3
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert chair.controller.preset_requests[-1]["outcome"] == "cancelled"


async def test_reconnect_during_write_does_not_replay_or_send_old_stop(chair):
    chair.feedback = False

    def reconnect(payload):
        acknowledge_activation(chair, payload)
        if payload == PRESET_COMMANDS["zgr"]:
            chair.controller.set_connected(False)
            chair.controller.set_connected(True)
            chair.controller.accept_status(reply(payload))

    chair.on_write = reconnect
    with pytest.raises(ControlError, match="Fresh"):
        await chair.controller.select_preset("zgr")
    assert len(chair.writes) == 2
    assert chair.controller.preset_requests[-1]["outcome"] == "failed"


async def test_failed_handoff_stop_blocks_new_control(chair):
    await chair.controller.select_preset("zgr")
    chair.fail_at = 3
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.step_position(0, -1)
    assert len(chair.writes) == 3
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert not any(p[13] == 0x81 for p in chair.writes)


async def test_accepted_preset_is_not_replayed_on_reconnection(chair):
    await chair.controller.select_preset("zgr")
    before = list(chair.writes)
    chair.controller.set_connected(False)
    chair.controller.set_connected(True)
    chair.controller.accept_status(status_with(positions=(0, 11, 40, 22)))
    await asyncio.sleep(0)
    assert chair.writes == before
    await chair.controller.step_position(1, -1)
    # A pending stop from the old connection must not be replayed either.
    assert chair.writes[len(before)][5] == 0x11
    assert chair.writes[len(before) + 1][14] == 0x81
    assert chair.writes.count(PRESET_COMMANDS["zgr"]) == 1


async def test_diagnostic_history_is_bounded(chair):
    for name in ["zgr", "lay_flat", "zgr", "lay_flat", "zgr"]:
        await chair.controller.select_preset(name)
    assert [r["preset"] for r in chair.controller.preset_requests] == [
        "lay_flat",
        "zgr",
        "lay_flat",
        "zgr",
    ]
