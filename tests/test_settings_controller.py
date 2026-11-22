"""Settings preserve live state and require fresh feedback across request handoffs."""

import asyncio

import pytest
from conftest import status_with

from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.protocol import HEAT_ZONES, Status, checksum


@pytest.mark.parametrize("zone", HEAT_ZONES)
@pytest.mark.parametrize("heat", [False, True])
async def test_zone_settings_preserve_other_levels_timers_and_master_heat(chair, zone, heat):
    before = status_with(
        heat=heat,
        massage=4,
        **{
            "5": 0x01,
            "8": 0x2A,
            "9": 0x30,
            "20": 1,
            "21": 2,
            "22": 3,
        },
    )
    chair.controller.accept_status(before)
    await chair.controller.set_heat_level(zone, "Off")
    assert len(chair.writes) == 1  # No activation or implicit heat enable.
    payload = chair.writes[0]
    assert payload[5] == 1
    assert payload[13:17] == before.raw[13:17]
    assert payload[19] == 0x8F
    assert chair.controller.status.heat is heat
    assert chair.controller.status.massage == "Wave"
    assert payload[6:10] == before.raw[6:10]
    assert chair.controller.status.heat_level(zone) == "Off"
    for other in HEAT_ZONES:
        if other != zone:
            assert chair.controller.status.heat_level(other) == before.heat_level(other)
    assert chair.controller.setting_requests[-1]["outcome"] == "confirmed"


@pytest.mark.parametrize(("kind", "minutes"), [("heat", 120), ("massage", 30)])
async def test_timer_update_while_function_off_preserves_other_settings(chair, kind, minutes):
    before = status_with(heat=False, massage=0, **{"20": 0, "21": 1, "22": 3})
    chair.controller.accept_status(before)
    await chair.controller.set_timer(kind, minutes)
    assert len(chair.writes) == 1
    after = chair.controller.status
    assert after.timer_minutes(kind) == minutes
    assert after.heat is False and after.massage == "Off"
    assert after.raw[20:23] == before.raw[20:23]
    other = "massage" if kind == "heat" else "heat"
    assert after.timer_minutes(other) == before.timer_minutes(other)


async def test_selected_heat_settings_survive_on_off_and_following_controls(chair):
    await chair.controller.set_timer("heat", 180)
    await chair.controller.set_heat_level("backrest", "Low")
    await chair.controller.set_heat_level("seat", "Off")
    await chair.controller.set_heat_level("footrest", "High")
    start = len(chair.writes)
    await chair.controller.set_heat(True)
    await chair.controller.set_heat(False)
    await chair.controller.set_massage("Gentle")
    await chair.controller.adjust_lumbar(True)
    assert all(packet[8:10] == bytes.fromhex("2a30") for packet in chair.writes[start:])
    assert all(packet[20:23] == bytes((1, 0, 3)) for packet in chair.writes[start:])


async def test_same_value_needs_a_new_report(chair):
    chair.feedback = False
    assert chair.controller.status.heat_level("seat") == "Medium"
    with pytest.raises(ControlError, match="did not confirm"):
        await chair.controller.set_heat_level("seat", "Medium")
    assert len(chair.writes) == 1
    assert chair.controller.setting_requests[-1]["outcome"] == "failed"


async def test_unrelated_new_report_does_not_confirm_target(chair):
    chair.feedback = False
    chair.on_write = lambda _: chair.controller.accept_status(status_with(massage=3))
    with pytest.raises(ControlError, match="did not confirm"):
        await chair.controller.set_timer("heat", 120)
    assert len(chair.writes) == 1


@pytest.mark.parametrize("condition", ["stale", "disconnected", "bad_positions"])
async def test_no_setting_write_without_usable_current_state(chair, condition):
    if condition == "stale":
        chair.time = 4
    elif condition == "disconnected":
        chair.controller.set_connected(False)
    else:
        chair.controller.accept_status(status_with(positions=(0x8F, 0, 0, 0)))
    with pytest.raises(ControlError):
        await chair.controller.set_heat_level("seat", "Low")
    assert chair.writes == []


@pytest.mark.parametrize(
    ("kind", "value"),
    [("heat", 0), ("heat", 240), ("massage", 60), ("heat", 60.0), ("heat", True), ("other", 60)],
)
async def test_unverified_timer_values_rejected_without_writes(chair, kind, value):
    with pytest.raises(ControlError):
        await chair.controller.set_timer(kind, value)
    assert chair.writes == []


async def test_setting_write_failure_is_not_retried(chair):
    chair.fail_at = 1
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.set_timer("heat", 120)
    assert len(chair.writes) == 1
    assert chair.controller.setting_requests[-1]["outcome"] == "failed"


async def test_disconnect_during_settings_does_not_resume(chair):
    chair.feedback = False

    def reconnect(_):
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with())

    chair.on_write = reconnect
    with pytest.raises(ControlError, match="Fresh"):
        await chair.controller.set_timer("heat", 120)
    assert len(chair.writes) == 1


async def test_setting_replaces_lumbar_after_release_and_motor_stop(chair):
    running = asyncio.create_task(chair.controller.adjust_lumbar(True))
    await asyncio.sleep(0)
    await chair.controller.set_heat_level("seat", "Low")
    with pytest.raises(CommandSuperseded):
        await running
    assert chair.writes[-2][13:17] == b"\x80" * 4
    assert chair.writes[-3][18] == 0
    assert chair.controller.status.heat_level("seat") == "Low"
    assert not chair.controller.busy


async def test_independent_settings_wait_for_confirmed_state_without_undoing_each_other(chair):
    chair.feedback = False
    old = asyncio.create_task(chair.controller.set_timer("heat", 180))
    await asyncio.sleep(0)
    newer = asyncio.create_task(chair.controller.set_heat_level("seat", "Low"))
    await asyncio.sleep(0)
    assert len(chair.writes) == 1
    chair.feedback = True
    chair.controller.accept_status(status_with(**{"8": 0x2A, "9": 0x30}))
    await asyncio.gather(old, newer)
    assert len(chair.writes) == 2
    assert chair.controller.status.timer_minutes("heat") == 180
    assert chair.controller.status.heat_level("seat") == "Low"
    assert chair.writes[-1][8:10] == bytes.fromhex("2a30")
    assert chair.controller.setting_requests[-2]["outcome"] == "confirmed"
    assert chair.controller.setting_requests[-1]["outcome"] == "confirmed"


async def test_heat_on_waits_for_zone_setting_and_preserves_it(chair):
    chair.controller.accept_status(status_with(heat=False))
    chair.feedback = False
    setting = asyncio.create_task(chair.controller.set_heat_level("seat", "Low"))
    await asyncio.sleep(0)
    heat = asyncio.create_task(chair.controller.set_heat(True))
    await asyncio.sleep(0)
    assert len(chair.writes) == 1
    chair.feedback = True
    chair.controller.accept_status(status_with(heat=False, **{"21": 1}))
    await asyncio.gather(setting, heat)
    assert len(chair.writes) == 3  # Zone, activation, Heat On.
    assert all(payload[21] == 1 for payload in chair.writes)
    assert chair.controller.status.heat is True


async def test_stop_discards_waiting_settings_without_replay(chair):
    chair.feedback = False
    first = asyncio.create_task(chair.controller.set_timer("heat", 180))
    await asyncio.sleep(0)
    waiting = asyncio.create_task(chair.controller.set_heat_level("seat", "Low"))
    await asyncio.sleep(0)
    assert len(chair.writes) == 1
    await chair.controller.stop_movement()
    results = await asyncio.gather(first, waiting, return_exceptions=True)
    assert all(isinstance(result, CommandSuperseded) for result in results)
    assert all(payload[21] == 2 for payload in chair.writes)
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert not chair.controller.busy


async def test_failed_confirmation_discards_waiting_settings(chair):
    chair.feedback = False
    first = asyncio.create_task(chair.controller.set_timer("heat", 180))
    await asyncio.sleep(0)
    waiting = asyncio.create_task(chair.controller.set_heat_level("seat", "Low"))
    results = await asyncio.gather(first, waiting, return_exceptions=True)
    assert isinstance(results[0], ControlError)
    assert isinstance(results[1], CommandSuperseded)
    assert len(chair.writes) == 1


async def test_settings_diagnostics_are_bounded_and_retain_exact_packet(chair):
    for _ in range(6):
        await chair.controller.set_timer("heat", 120)
    assert len(chair.controller.setting_requests) == 5
    last = chair.controller.setting_requests[-1]
    assert bytes.fromhex(last["command_hex"]) == chair.writes[-1]
    assert Status(bytes.fromhex(last["status_after_hex"])).timer_minutes("heat") == 120
    assert chair.writes[-1][25] == checksum(chair.writes[-1])
