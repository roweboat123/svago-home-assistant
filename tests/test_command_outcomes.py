"""Quiet expected outcomes without concealing fresh hardware failures."""

import asyncio

import pytest
from conftest import status_with

from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.protocol import OBSERVED_BOUNDS


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("direction", [-1, 1])
async def test_directional_press_at_each_observed_limit_is_recorded_without_writes(
    chair, index, direction
):
    positions = list(chair.controller.status.positions)
    positions[index] = OBSERVED_BOUNDS[index][1 if direction > 0 else 0]
    chair.controller.accept_status(status_with(positions=positions))

    assert await chair.controller.step_position(index, direction) == "at_observed_limit"

    assert chair.writes == []
    trace = chair.controller.tap_requests[-1]
    assert trace["outcome"] == "at_observed_limit"
    assert trace["direction_writes"] == 0
    assert trace["stop"]["outcome"] == "not_attempted"
    assert trace["positions_before"] == trace["positions_after"] == positions
    assert trace["feedback_changed"] is False
    assert not chair.controller.busy


@pytest.mark.parametrize("index", range(4))
async def test_invalid_observed_position_remains_a_real_error(chair, index):
    positions = list(chair.controller.status.positions)
    positions[index] = OBSERVED_BOUNDS[index][1] + 1
    chair.controller.accept_status(status_with(positions=positions))
    with pytest.raises(ControlError, match="outside"):
        await chair.controller.step_position(index, 1)
    assert chair.writes == []
    assert chair.controller.tap_requests[-1]["outcome"] == "failed"


async def test_endpoint_noop_does_not_accept_unknown_other_motor_feedback(chair):
    chair.controller.accept_status(status_with(positions=(0, 0x8F, 16, 28)))
    with pytest.raises(ControlError, match="not numeric"):
        await chair.controller.step_position(0, -1)
    assert chair.writes == []


@pytest.mark.parametrize("index", range(4))
async def test_requested_slider_beyond_observed_bound_remains_invalid(chair, index):
    with pytest.raises(ControlError, match="outside"):
        await chair.controller.set_position(index, OBSERVED_BOUNDS[index][1] + 1)
    assert chair.writes == []


async def test_endpoint_press_still_releases_an_existing_lumbar_adjustment(chair):
    chair.controller.accept_status(status_with(positions=(0, 6, 16, 28)))
    entered = asyncio.Event()

    async def hold_after_first_pulse(_):
        entered.set()
        await asyncio.Event().wait()

    chair.controller._sleep = hold_after_first_pulse
    running = asyncio.create_task(chair.controller.adjust_lumbar(True))
    await entered.wait()
    assert await chair.controller.step_position(0, -1) == "at_observed_limit"
    with pytest.raises(CommandSuperseded):
        await running

    assert chair.writes[-2][18] == 0
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert chair.writes[-1][18] == 0
    assert not any(0x81 <= value <= 0x82 for p in chair.writes for value in p[13:17])
    assert chair.controller.tap_requests[-1]["outcome"] == "at_observed_limit"
    assert not chair.controller.busy


async def test_endpoint_handoff_does_not_hide_a_failed_required_stop(chair):
    chair.controller.accept_status(status_with(positions=(0, 6, 16, 28)))
    chair.controller._needs_stop = True
    chair.fail_at = 1
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.step_position(0, -1)
    assert chair.writes[0][13:17] == b"\x80" * 4
    assert chair.controller.tap_requests[-1]["outcome"] == "failed"


@pytest.mark.parametrize("reconnect", [False, True])
async def test_queued_settings_from_old_connection_are_quietly_superseded(chair, reconnect):
    await chair.controller._settings_lock.acquire()
    waiting = [
        asyncio.create_task(chair.controller.set_timer("heat", 120)),
        asyncio.create_task(chair.controller.set_heat_level("seat", "Low")),
        asyncio.create_task(chair.controller.set_timer("massage", 30)),
    ]
    await asyncio.sleep(0)
    chair.controller.set_connected(False)
    if reconnect:
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with())
    chair.controller._settings_lock.release()

    results = await asyncio.gather(*waiting, return_exceptions=True)
    assert all(isinstance(result, CommandSuperseded) for result in results)
    assert chair.writes == []

    if reconnect:
        await chair.controller.set_timer("heat", 120)
        assert len(chair.writes) == 1
    else:
        with pytest.raises(ControlError, match="Fresh"):
            await chair.controller.set_timer("heat", 120)
        assert chair.writes == []


async def test_first_failed_setting_reports_error_and_discards_its_waiting_batch(chair):
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def fail_write(payload):
        writes.append(payload)
        entered.set()
        await release.wait()
        raise OSError("link lost")

    chair.controller._write = fail_write
    first = asyncio.create_task(chair.controller.set_timer("heat", 120))
    await entered.wait()
    waiting = [
        asyncio.create_task(chair.controller.set_heat_level("seat", "Low")),
        asyncio.create_task(chair.controller.set_timer("massage", 30)),
    ]
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(ControlError, match="not retried"):
        await first
    results = await asyncio.gather(*waiting, return_exceptions=True)
    assert all(isinstance(result, CommandSuperseded) for result in results)
    assert len(writes) == 1
    assert chair.controller.setting_requests[0]["outcome"] == "failed"
    assert all(
        trace["outcome"] == "replaced_by_new_request"
        for trace in list(chair.controller.setting_requests)[1:]
    )


@pytest.mark.parametrize("reconnect", [False, True])
async def test_connection_invalidated_during_handoff_discards_waiter(chair, reconnect):
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending_handoff(*, superseded=False):
        entered.set()
        await release.wait()

    chair.controller._cancel_active = pending_handoff
    waiting = asyncio.create_task(chair.controller.stop_movement())
    await entered.wait()
    chair.controller.set_connected(False)
    if reconnect:
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with())
    release.set()
    with pytest.raises(CommandSuperseded):
        await waiting
    assert chair.writes == []
    assert not chair.controller.busy


async def test_status_age_is_local_to_the_current_connection(chair):
    assert chair.controller.status_age_seconds == 0
    assert chair.controller.status_freshness_limit == 3
    chair.time = 1.5
    assert chair.controller.status_age_seconds == 1.5
    chair.controller.set_connected(False)
    assert chair.controller.status_age_seconds is None
    chair.controller.set_connected(True)
    assert chair.controller.status_age_seconds is None
    chair.controller.accept_status(status_with())
    assert chair.controller.status_age_seconds == 0


def make_stationary(chair):
    chair.time += 2
    chair.controller.accept_status(chair.controller.status)
    assert chair.controller.can_save_memory


async def test_memory_arming_discards_settings_already_waiting_for_a_lock(chair):
    make_stationary(chair)
    await chair.controller._settings_lock.acquire()
    waiting = [
        asyncio.create_task(chair.controller.set_timer("heat", 120)),
        asyncio.create_task(chair.controller.set_heat_level("seat", "Low")),
    ]
    await asyncio.sleep(0)
    chair.controller.arm_memory_save()
    chair.controller._settings_lock.release()

    results = await asyncio.gather(*waiting, return_exceptions=True)
    assert all(isinstance(result, CommandSuperseded) for result in results)
    assert chair.writes == []
    assert chair.controller.can_save_memory


async def test_memory_arming_invalidates_a_recall_waiting_for_storage(chair):
    make_stationary(chair)
    profile = chair.controller.snapshot_memory()
    request = chair.controller.reserve_memory_recall()
    chair.controller.arm_memory_save()
    with pytest.raises(CommandSuperseded):
        await chair.controller.recall_memory(1, profile, request=request)
    assert chair.writes == []


async def test_memory_arming_rejects_motion_without_cancelling_it(chair):
    entered = asyncio.Event()

    async def hold_pulse(_):
        entered.set()
        await asyncio.Event().wait()

    chair.controller._sleep = hold_pulse
    moving = asyncio.create_task(chair.controller.adjust_lumbar(True))
    await entered.wait()
    writes_before = len(chair.writes)
    intent_before = chair.controller.intent_sequence
    with pytest.raises(ControlError, match="chair to stop"):
        chair.controller.arm_memory_save()
    assert chair.controller.busy
    assert not moving.done()
    assert chair.controller.intent_sequence == intent_before
    assert len(chair.writes) == writes_before
    await chair.controller.cancel_active()
    assert moving.cancelled()


async def test_memory_arming_preserves_background_continuous_selection_and_worker(chair):
    await chair.controller.set_massage_continuous()
    await chair.controller.set_heat_continuous()
    make_stationary(chair)
    worker = chair.controller._heat_renewal_task
    writes_before = len(chair.writes)
    try:
        chair.controller.arm_memory_save()
        assert chair.controller.massage_continuous
        assert chair.controller.heat_continuous
        assert chair.controller._heat_renewal_task is worker
        assert worker is not None and not worker.done()
        assert len(chair.writes) == writes_before
    finally:
        await chair.controller.cancel_active()
