"""Target movement, actual HA slider entities and command replacement."""

import asyncio
from types import SimpleNamespace

import pytest
from conftest import status_with
from homeassistant.components.number import NumberMode
from homeassistant.exceptions import HomeAssistantError

from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.entity import SvagoEntity
from custom_components.svago.number import PositionNumber
from custom_components.svago.protocol import OBSERVED_BOUNDS, POSITION_NAMES


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("direction", [-1, 1])
async def test_slider_continuous_direction_until_target_then_one_stop(chair, index, direction):
    chair.controller.accept_status(status_with(positions=(5, 3, 16, 15), heat=False))
    before = chair.controller.status.positions
    target = before[index] + direction * 3
    await chair.controller.set_position(index, target)
    assert len(chair.writes) == 5
    assert chair.writes[0][5] == 0x11
    assert all(p[13 + index] == (0x82 if direction > 0 else 0x81) for p in chair.writes[1:-1])
    assert all(p[13 + j] == 0x8F for p in chair.writes[1:-1] for j in range(4) if j != index)
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert all(p[19] == 2 for p in chair.writes)
    assert chair.controller.status.positions[index] == target
    assert chair.time == pytest.approx(0.3)


async def test_matching_position_is_noop(chair):
    await chair.controller.set_position(0, 5)
    assert not chair.writes


@pytest.mark.parametrize(
    "index,target", [(0, -1), (0, 18), (1, 23), (2, 41), (3, 29), (4, 1), (0, 5.5)]
)
async def test_slider_rejects_invalid_target_without_write(chair, index, target):
    with pytest.raises(ControlError):
        await chair.controller.set_position(index, target)
    assert not chair.writes


async def test_stall_with_fresh_status_stops_before_late_timeout(chair):
    chair.feedback = False
    chair.activation_feedback = True
    chair.on_sleep = lambda: chair.controller.accept_status(chair.controller.status)
    with pytest.raises(ControlError, match="Position stopped changing"):
        await chair.controller.set_position(0, 8)
    assert 2 <= chair.time < 2.2
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert not chair.controller.busy


async def test_slider_does_not_reverse_after_passing_target(chair):
    chair.feedback = False
    chair.activation_feedback = True
    chair.on_sleep = lambda: chair.controller.accept_status(status_with(positions=(9, 6, 16, 28)))
    await chair.controller.set_position(0, 8)
    assert len(chair.writes) == 3
    assert chair.writes[0][5] == 0x11
    assert chair.writes[1][13] == 0x82
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_slider_reverse_feedback_stops(chair):
    chair.feedback = False
    chair.activation_feedback = True
    chair.on_sleep = lambda: chair.controller.accept_status(status_with(positions=(3, 6, 16, 28)))
    with pytest.raises(ControlError, match="Unexpected"):
        await chair.controller.set_position(0, 8)
    assert len(chair.writes) == 3
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_slider_normal_release_failure_surfaces(chair):
    chair.fail_at = 5  # Activation, three direction commands, then release.
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.set_position(0, 8)
    assert len(chair.writes) == 5
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_slider_stale_feedback_prevents_further_writes(chair):
    def stale():
        chair.time += 4

    chair.on_sleep = stale
    with pytest.raises(ControlError, match="Fresh"):
        await chair.controller.set_position(0, 8)
    assert len(chair.writes) == 2
    assert chair.writes[0][5] == 0x11
    assert chair.writes[1][13] == 0x82


async def test_slider_time_limit_stops_even_with_progress(chair, monkeypatch):
    from custom_components.svago import controller

    monkeypatch.setattr(controller, "SLIDER_MAX_SECONDS", 0.25)
    with pytest.raises(ControlError, match="time limit"):
        await chair.controller.set_position(0, 15)
    assert len(chair.writes) == 5
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_slider_wall_clock_limit_cancels_blocked_write_and_stops(chair, monkeypatch):
    from custom_components.svago import controller

    monkeypatch.setattr(controller, "SLIDER_MAX_SECONDS", 0.01)
    writes = []

    async def blocked_write(packet):
        writes.append(packet)
        if packet[5] == 0x11:
            await chair.write(packet)
        if packet[13] == 0x82:
            await asyncio.Event().wait()

    chair.controller._write = blocked_write
    with pytest.raises(ControlError, match="time limit"):
        await chair.controller.set_position(0, 15)
    assert len(writes) == 3
    assert writes[0][5] == 0x11
    assert writes[-1][13:17] == b"\x80" * 4


async def test_slider_uses_latest_heat_in_every_command_and_stop(chair):
    def change_heat():
        if len(chair.writes) == 3:
            chair.controller.accept_status(
                status_with(
                    positions=chair.controller.status.positions, heat=False, **{"8": 0, "9": 0}
                )
            )

    chair.on_sleep = change_heat
    await chair.controller.set_position(0, 9)
    assert all(p[19] == 1 for p in chair.writes[:3])
    assert all(p[19] == 2 and p[8:10] == b"\x00\x00" for p in chair.writes[3:])


async def test_slider_invalid_feedback_stops_and_native_value_is_unknown(chair):
    chair.feedback = False
    chair.activation_feedback = True
    chair.on_sleep = lambda: chair.controller.accept_status(
        status_with(positions=(0x82, 6, 16, 28))
    )
    number = PositionNumber(
        SimpleNamespace(controller=chair.controller, address="test"), 0, "headrest"
    )
    with pytest.raises(HomeAssistantError, match="Unexpected"):
        await number.async_set_native_value(9)
    assert number.native_value is None
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_slider_reconnect_does_not_replay(chair):
    def reconnect():
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with())

    chair.on_sleep = reconnect
    with pytest.raises(ControlError, match="Fresh"):
        await chair.controller.set_position(0, 8)
    assert len(chair.writes) == 2
    assert chair.writes[0][5] == 0x11


async def test_new_slider_target_replaces_old_and_suppresses_old_toast(chair):
    connection = SimpleNamespace(controller=chair.controller, address="test")
    number = PositionNumber(connection, 0, "headrest")
    old = asyncio.create_task(number.async_set_native_value(12))
    await asyncio.sleep(0)
    await number.async_set_native_value(3)
    await old  # Entity treats deliberate replacement as completion, not HA error.
    codes = [p[13] for p in chair.writes]
    first_decrease = codes.index(0x81)
    assert 0x80 in codes[1:first_decrease]
    assert 0x82 not in codes[first_decrease:]
    assert number.native_value == 3


async def test_stop_interrupts_slider_without_stale_resumption(chair):
    task = asyncio.create_task(chair.controller.set_position(0, 15))
    await asyncio.sleep(0)
    await chair.controller.stop_movement()
    with pytest.raises(CommandSuperseded):
        await task
    assert chair.writes[0][5] == 0x11
    assert chair.writes[1][13] == 0x82
    assert all(p[13] == 0x80 for p in chair.writes[2:])
    assert not chair.controller.busy


async def test_heat_off_supersedes_heat_on_feedback_wait(chair):
    connection = SimpleNamespace(controller=chair.controller, address="test")
    entity = SvagoEntity(connection, "test", "Test")
    chair.controller.accept_status(status_with(heat=False))
    chair.feedback = False

    def activation_response(payload):
        if payload[5] == 0x11:
            chair.controller.accept_status(status_with(heat=False, **{"5": 0x11}))

    chair.on_write = activation_response
    pending_on = asyncio.create_task(entity.execute(chair.controller.set_heat(True)))
    await asyncio.sleep(0)
    await entity.execute(chair.controller.set_heat(False))
    await pending_on
    assert [p[19] for p in chair.writes] == [2, 1, 2, 2]
    assert chair.controller.last_heat_request["requested"] is False
    assert chair.controller.last_heat_request["outcome"] == "confirmed"
    assert chair.controller.last_heat_request["command_hex"] == chair.writes[-1].hex().upper()
    assert not chair.controller.busy


async def test_heat_diagnostic_records_timeout_and_exact_attempt(chair):
    chair.feedback = False
    before = chair.controller.status.raw.hex().upper()
    with pytest.raises(ControlError, match="did not confirm"):
        await chair.controller.set_heat(False)
    trace = chair.controller.last_heat_request
    assert trace["requested"] is False and trace["outcome"] == "failed"
    assert trace["command_hex"] == chair.writes[0].hex().upper()
    assert trace["status_before_hex"] == trace["status_after_hex"] == before
    assert "did not confirm" in trace["error"]


async def test_heat_diagnostic_distinguishes_replaced_request(chair):
    chair.feedback = False
    entity = SvagoEntity(
        SimpleNamespace(controller=chair.controller, address="test"), "test", "Test"
    )
    heat = asyncio.create_task(entity.execute(chair.controller.set_heat(False)))
    await asyncio.sleep(0)
    await chair.controller.stop_movement()
    await heat
    assert chair.controller.last_heat_request["outcome"] == "replaced_by_new_request"


async def test_failed_handoff_stop_prevents_replacement_action(chair):
    task = asyncio.create_task(chair.controller.adjust_lumbar(True))
    await asyncio.sleep(0)
    chair.fail_at = 4  # Activation, lumbar, cancellation release, mandatory stop.
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.set_heat(False)
    with pytest.raises(CommandSuperseded):
        await task
    assert len(chair.writes) == 4
    assert all(p[19] == 1 for p in chair.writes)


async def test_cancelled_replacement_finishes_prior_release_without_sending_new_action(chair):
    release_started = asyncio.Event()
    finish_release = asyncio.Event()
    write = chair.controller._write

    async def delayed_release(payload):
        if payload[13:17] == b"\x8f" * 4 and payload[18] == 0:
            release_started.set()
            await finish_release.wait()
        await write(payload)

    chair.controller._write = delayed_release
    active = asyncio.create_task(chair.controller.adjust_lumbar(True))
    await asyncio.sleep(0)
    replacement = asyncio.create_task(chair.controller.set_heat(False))
    await asyncio.wait_for(release_started.wait(), timeout=1)
    before_cancel = list(chair.writes)
    assert before_cancel[0][5] == 0x11
    assert all(p[18] == 2 for p in before_cancel[1:])
    replacement.cancel()
    with pytest.raises(asyncio.CancelledError):
        await replacement
    finish_release.set()
    with pytest.raises(CommandSuperseded):
        await active
    assert chair.writes[:-1] == before_cancel
    assert chair.writes[-1][18] == 0
    assert all(p[19] == 1 for p in chair.writes)
    assert not chair.controller.busy


async def test_only_newest_of_pending_replacements_runs(chair):
    entity = SvagoEntity(
        SimpleNamespace(controller=chair.controller, address="test"), "test", "Test"
    )
    initial = asyncio.create_task(entity.execute(chair.controller.adjust_lumbar(True)))
    await asyncio.sleep(0)
    first = asyncio.create_task(entity.execute(chair.controller.set_position(0, 12)))
    last = asyncio.create_task(entity.execute(chair.controller.set_position(0, 3)))
    await asyncio.gather(initial, first, last)
    assert 0x82 not in [p[13] for p in chair.writes]
    assert chair.controller.status.positions[0] == 3
    assert any(p[13:17] == b"\x80" * 4 for p in chair.writes)


async def test_stop_invalidates_pending_slider_request(chair):
    entity = SvagoEntity(
        SimpleNamespace(controller=chair.controller, address="test"), "test", "Test"
    )
    initial = asyncio.create_task(entity.execute(chair.controller.adjust_lumbar(True)))
    await asyncio.sleep(0)
    slider = asyncio.create_task(entity.execute(chair.controller.set_position(0, 12)))
    stop = asyncio.create_task(entity.execute(chair.controller.stop_movement()))
    await asyncio.gather(initial, slider, stop)
    assert 0x82 not in [p[13] for p in chair.writes]
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_disconnect_during_handoff_never_sends_new_action(chair):
    entity = SvagoEntity(
        SimpleNamespace(controller=chair.controller, address="test"), "test", "Test"
    )
    old = asyncio.create_task(entity.execute(chair.controller.adjust_lumbar(True)))
    await asyncio.sleep(0)
    chair.feedback = False

    def disconnect_on_release(packet):
        if packet[18] == 0:
            chair.controller.set_connected(False)

    chair.on_write = disconnect_on_release
    with pytest.raises(CommandSuperseded):
        await chair.controller.set_position(0, 12)
    await old
    assert len(chair.writes) == 3


@pytest.mark.parametrize("index", range(4))
async def test_native_slider_bounds_feedback_and_action(chair, index):
    connection = SimpleNamespace(controller=chair.controller, address="test")
    number = PositionNumber(connection, index, POSITION_NAMES[index])
    assert number.mode == NumberMode.SLIDER
    assert (number.native_min_value, number.native_max_value) == OBSERVED_BOUNDS[index]
    assert number.native_step == 1
    before = number.native_value
    await number.async_set_native_value(before - 1)
    assert number.native_value == before - 1
    chair.controller.set_connected(False)
    assert number.native_value is None and not number.available


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 4.5])
async def test_native_slider_rejects_non_integer_targets(chair, value):
    number = PositionNumber(
        SimpleNamespace(controller=chair.controller, address="test"), 0, "headrest"
    )
    with pytest.raises(HomeAssistantError, match="whole"):
        await number.async_set_native_value(value)
    assert not chair.writes
