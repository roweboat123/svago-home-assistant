"""Repeated button presses share a bounded gesture, not queued future travel."""

import asyncio

import pytest
from conftest import status_with

from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.memory_protocol import MemoryProfile
from custom_components.svago.protocol import Status, checksum


def report_one_step_per_burst(chair, reported_bursts):
    """Observe startup once, then keep reporting that position during the hold."""
    session = chair.controller._tap_session
    status = chair.controller.status
    if session and session["phase"] == "moving" and session["burst_id"] not in reported_bursts:
        reported_bursts.add(session["burst_id"])
        raw = bytearray(status.raw)
        raw[13 + session["index"]] += session["direction"]
        raw[25] = checksum(raw)
        status = Status(bytes(raw))
    chair.controller.accept_status(status)


class GatedPulse:
    """Pause each poll; report one useful step per burst without hitting a bound."""

    def __init__(self, chair, *, pause_after=0.1, report_progress=True):
        self.chair = chair
        self.pause_after = pause_after
        self.report_progress = report_progress
        self.ticks = asyncio.Queue()
        self.permits = asyncio.Queue()
        self.free = False
        self.reported_bursts = set()
        chair.feedback = False
        chair.activation_feedback = True
        chair.controller.accept_status(status_with(positions=(5, 3, 16, 10)))
        chair.controller._sleep = self.sleep

    async def sleep(self, seconds):
        finished = self.chair.time + seconds
        first_part = min(seconds, self.pause_after)
        self.chair.time += first_part
        if self.report_progress:
            report_one_step_per_burst(self.chair, self.reported_bursts)
        else:
            self.chair.controller.accept_status(self.chair.controller.status)
        self.ticks.put_nowait(self.chair.time)
        if not self.free:
            await self.permits.get()
        self.chair.time = max(self.chair.time, finished)
        await asyncio.sleep(0)

    async def tick(self):
        return await asyncio.wait_for(self.ticks.get(), 1)

    def release(self):
        self.permits.put_nowait(None)

    def finish(self):
        self.free = True
        self.release()


def directions(chair):
    return [packet for packet in chair.writes if any(v in (0x81, 0x82) for v in packet[13:17])]


def stops(chair):
    return [packet for packet in chair.writes if packet[13:17] == b"\x80" * 4]


@pytest.mark.parametrize("index,pulse", [(0, 0.5), (1, 0.25), (2, 0.25), (3, 0.5)])
async def test_same_direction_extends_one_gesture_without_extra_activation_or_stop(
    chair, index, pulse
):
    # Join while the first poll is pending, before the shortest pulse expires.
    joined_at = min(0.1, pulse / 2)
    gate = GatedPulse(chair, pause_after=joined_at)
    owner = asyncio.create_task(chair.controller.step_position(index, 1))
    assert await gate.tick() == pytest.approx(joined_at)
    joined = asyncio.create_task(chair.controller.step_position(index, 1))
    await asyncio.sleep(0)
    assert chair.controller._tap_session["deadline"] == pytest.approx(joined_at + pulse)
    assert len(chair.writes) == 2  # One activation and one direction so far.
    assert not stops(chair)
    assert not joined.done()
    gate.finish()
    assert await asyncio.gather(owner, joined) == [None, None]
    assert chair.time == pytest.approx(joined_at + pulse)
    assert len([p for p in chair.writes if p[5] == 0x11]) == 1
    assert len(stops(chair)) == 1
    assert chair.controller._tap_session is None
    first, second = chair.controller.tap_requests
    assert first["coalesced_presses"] == 1
    assert second["coalesced_into_burst"] == first["burst_id"]
    assert second["direction_writes"] == 0


async def test_backrest_join_without_progress_extends_only_the_timed_pulse(chair):
    gate = GatedPulse(chair, report_progress=False)
    owner = asyncio.create_task(chair.controller.step_position(1, 1))
    assert await gate.tick() == pytest.approx(0.1)
    joined = asyncio.create_task(chair.controller.step_position(1, 1))
    await asyncio.sleep(0)
    assert chair.controller._tap_session["deadline"] == pytest.approx(0.35)
    gate.finish()
    assert await asyncio.gather(owner, joined) == [None, None]

    assert chair.time == pytest.approx(0.35)
    assert len(directions(chair)) == 4
    assert len(stops(chair)) == 1
    first, second = chair.controller.tap_requests
    assert first["startup_extended"] is False
    assert first["startup_outcome"] == "not_observed_during_timed_pulse"
    assert first["feedback_changed"] is False
    assert second["coalesced_into_burst"] == first["burst_id"]
    assert second["shared_burst"]["startup_outcome"] == "not_observed_during_timed_pulse"

    count = len(chair.writes)
    await chair.controller.step_position(1, 1)
    assert chair.writes[count][5] == 0x11
    assert len(directions(chair)) == 7
    assert len(stops(chair)) == 2
    assert chair.controller.tap_requests[-1]["burst_id"] != first["burst_id"]


@pytest.mark.parametrize("presses", [(0.02, 0.06), tuple(index / 40 for index in range(1, 120))])
async def test_joined_backrest_pulses_keep_repeat_interval_and_cap_without_progress(chair, presses):
    chair.feedback = False
    chair.activation_feedback = True
    chair.controller.accept_status(status_with(positions=(5, 3, 16, 10)))
    press_times = iter(presses)
    next_press = next(press_times)
    joiners = []
    owner_trace = None

    async def sleep(seconds):
        nonlocal next_press, owner_trace
        owner_trace = chair.controller._tap_session["trace"]
        finished = chair.time + seconds
        while next_press is not None and next_press < finished:
            chair.time = next_press
            chair.controller.accept_status(chair.controller.status)
            joiners.append(asyncio.create_task(chair.controller.step_position(1, 1)))
            await asyncio.sleep(0)
            next_press = next(press_times, None)
        chair.time = finished
        chair.controller.accept_status(chair.controller.status)
        await asyncio.sleep(0)

    chair.controller._sleep = sleep
    await chair.controller.step_position(1, 1)
    assert await asyncio.gather(*joiners) == [None] * len(presses)
    expected_duration = min(3, presses[-1] + 0.25)
    expected_writes = 4 if len(presses) == 2 else 30
    assert len(directions(chair)) == expected_writes
    assert [record["seconds"] for record in owner_trace["direction_write_log"]] == [
        round(index / 10, 1) for index in range(max(0, expected_writes - 8), expected_writes)
    ]
    assert owner_trace["stop"]["seconds"] == expected_duration
    assert owner_trace["startup_extended"] is False
    assert owner_trace["startup_outcome"] == "not_observed_during_timed_pulse"
    assert len([packet for packet in chair.writes if packet[5] == 0x11]) == 1
    assert len(stops(chair)) == 1


async def test_stop_interrupts_timed_backrest_and_its_joiner(chair):
    gate = GatedPulse(chair, pause_after=0.025, report_progress=False)
    owner = asyncio.create_task(chair.controller.step_position(1, 1))
    await gate.tick()
    joined = asyncio.create_task(chair.controller.step_position(1, 1))
    await asyncio.sleep(0)
    await chair.controller.stop_movement()
    results = await asyncio.gather(owner, joined, return_exceptions=True)
    assert all(isinstance(result, CommandSuperseded) for result in results)
    assert len(directions(chair)) == 1
    assert stops(chair)
    count = len(chair.writes)
    gate.finish()
    await asyncio.sleep(0)
    assert len(chair.writes) == count
    assert chair.controller._tap_session is None


async def test_many_repeated_presses_have_three_second_cap_and_bounded_diagnostics(chair):
    gate = GatedPulse(chair)
    owner = asyncio.create_task(chair.controller.step_position(0, 1))
    joined = []
    for _ in range(29):
        assert await gate.tick() < 3
        joined.append(asyncio.create_task(chair.controller.step_position(0, 1)))
        await asyncio.sleep(0)
        assert chair.controller._tap_session["deadline"] <= 3
        gate.release()
    gate.finish()
    assert await asyncio.gather(owner, *joined) == [None] * 30
    assert chair.time == pytest.approx(3)
    assert len(directions(chair)) == 30
    assert len(stops(chair)) == 1
    assert len([p for p in chair.writes if p[5] == 0x11]) == 1
    assert len(chair.controller.tap_requests) == 4
    assert all(len(t["direction_write_log"]) <= 8 for t in chair.controller.tap_requests)
    for trace in chair.controller.tap_requests:
        assert trace["shared_burst"] == {
            "direction_writes": 30,
            "coalesced_presses": 29,
            "effective_pulse_seconds": 3,
            "startup_extended": False,
            "startup_outcome": "progress_observed",
            "first_progress_seconds": 0.1,
            "stop_outcome": "write_returned",
        }
    assert chair.controller._tap_session is None
    count = len(chair.writes)
    await asyncio.sleep(0)
    assert len(chair.writes) == count


async def test_startup_presses_join_activation_without_building_travel_backlog(chair):
    chair.feedback = False
    chair.activation_feedback = True
    reported_bursts = set()
    chair.on_sleep = lambda: report_one_step_per_burst(chair, reported_bursts)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_write(packet):
        if packet[5] == 0x11:
            entered.set()
            await release.wait()
        await chair.write(packet)

    chair.controller._write = delayed_write
    owner = asyncio.create_task(chair.controller.step_position(0, 1))
    await entered.wait()
    joined = [asyncio.create_task(chair.controller.step_position(0, 1)) for _ in range(5)]
    await asyncio.sleep(0)
    assert chair.controller._tap_session["phase"] == "activating"
    assert chair.controller._tap_session["trace"]["coalesced_presses"] == 5
    release.set()
    assert await asyncio.gather(owner, *joined) == [None] * 6
    assert chair.time == pytest.approx(0.5)
    assert len(directions(chair)) == 5
    assert len(stops(chair)) == 1
    assert len([p for p in chair.writes if p[5] == 0x11]) == 1


async def test_cancelled_joiner_does_not_cancel_shared_gesture(chair):
    gate = GatedPulse(chair)
    owner = asyncio.create_task(chair.controller.step_position(0, 1))
    await gate.tick()
    joined = asyncio.create_task(chair.controller.step_position(0, 1))
    await asyncio.sleep(0)
    joined.cancel()
    with pytest.raises(asyncio.CancelledError):
        await joined
    assert not owner.done()
    assert not stops(chair)
    gate.finish()
    await owner
    assert len(stops(chair)) == 1
    assert chair.controller._tap_session is None


async def test_joiner_finishes_when_gesture_finishes_not_when_owner_outer_task_finishes(chair):
    gate = GatedPulse(chair)
    unrelated = asyncio.Event()

    async def outer():
        await chair.controller.step_position(0, 1)
        await unrelated.wait()

    owner = asyncio.create_task(outer())
    await gate.tick()
    joined = asyncio.create_task(chair.controller.step_position(0, 1))
    await asyncio.sleep(0)
    gate.finish()
    await asyncio.wait_for(joined, 1)
    assert not owner.done()
    unrelated.set()
    await owner


@pytest.mark.parametrize("replacement", ["stop", "reverse", "axis"])
async def test_new_intent_supersedes_owner_and_all_joiners_without_resuming(chair, replacement):
    gate = GatedPulse(chair)
    owner = asyncio.create_task(chair.controller.step_position(0, 1))
    await gate.tick()
    joined = asyncio.create_task(chair.controller.step_position(0, 1))
    await asyncio.sleep(0)
    gate.free = True  # The old task remains blocked until it is cancelled.
    if replacement == "stop":
        await chair.controller.stop_movement()
    elif replacement == "reverse":
        await chair.controller.step_position(0, -1)
    else:
        await chair.controller.step_position(3, 1)
    results = await asyncio.gather(owner, joined, return_exceptions=True)
    assert all(isinstance(result, CommandSuperseded) for result in results)
    assert len([p for p in directions(chair) if p[13] == 0x82]) == 1
    assert chair.controller._tap_session is None
    count = len(chair.writes)
    gate.release()
    await asyncio.sleep(0)
    assert len(chair.writes) == count


async def test_post_activation_endpoint_is_quiet_and_shared(chair):
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_write(packet):
        entered.set()
        await release.wait()
        await chair.write(packet)
        chair.controller.accept_status(status_with(positions=(17, 6, 16, 10), **{"5": 0x11}))

    chair.controller._write = delayed_write
    owner = asyncio.create_task(chair.controller.step_position(0, 1))
    await entered.wait()
    joined = asyncio.create_task(chair.controller.step_position(0, 1))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(owner, joined) == ["at_observed_limit"] * 2
    assert len(chair.writes) == 1
    assert chair.writes[0][5] == 0x11
    assert not directions(chair) and not stops(chair)
    assert chair.controller._tap_session is None


async def test_shared_write_failure_is_propagated_and_does_not_replay(chair):
    gate = GatedPulse(chair)
    owner = asyncio.create_task(chair.controller.step_position(0, 1))
    await gate.tick()
    joined = asyncio.create_task(chair.controller.step_position(0, 1))
    await asyncio.sleep(0)
    chair.fail_at = 3  # Activation and first direction have already succeeded.
    gate.finish()
    results = await asyncio.gather(owner, joined, return_exceptions=True)
    assert all(isinstance(result, ControlError) for result in results)
    assert results[0] is results[1]
    assert len(directions(chair)) == 2 and len(stops(chair)) == 1
    assert chair.controller._tap_session is None


@pytest.mark.parametrize("failure", ["stale", "reconnect"])
async def test_connection_loss_ends_all_waiters_without_late_writes(chair, failure):
    gate = GatedPulse(chair)
    owner = asyncio.create_task(chair.controller.step_position(0, 1))
    await gate.tick()
    joined = asyncio.create_task(chair.controller.step_position(0, 1))
    await asyncio.sleep(0)
    if failure == "stale":
        chair.time += 4
    else:
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with())
    gate.finish()
    results = await asyncio.gather(owner, joined, return_exceptions=True)
    assert all(isinstance(result, ControlError) for result in results)
    assert len(chair.writes) == 2  # Never release using stale/new-connection state.
    assert chair.controller._tap_session is None


async def test_memory_recall_supersedes_shared_gesture(chair):
    gate = GatedPulse(chair)
    profile = MemoryProfile.from_status(chair.controller.status)
    owner = asyncio.create_task(chair.controller.step_position(0, 1))
    await gate.tick()
    joined = asyncio.create_task(chair.controller.step_position(0, 1))
    await asyncio.sleep(0)
    gate.free = True
    chair.feedback = True
    await chair.controller.recall_memory(2, profile)
    results = await asyncio.gather(owner, joined, return_exceptions=True)
    assert all(isinstance(result, CommandSuperseded) for result in results)
    assert len(directions(chair)) == 1
    assert chair.controller.memory_requests[-1]["outcome"] == "target_reached"
    assert chair.controller._tap_session is None


async def test_later_press_replaces_pending_memory_reservation_instead_of_joining_old_burst(chair):
    gate = GatedPulse(chair)
    profile = MemoryProfile.from_status(chair.controller.status)
    owner = asyncio.create_task(chair.controller.step_position(0, 1))
    await gate.tick()
    reservation = chair.controller.reserve_memory_recall()
    gate.free = True
    await chair.controller.step_position(0, 1)
    with pytest.raises(CommandSuperseded):
        await owner
    with pytest.raises(CommandSuperseded):
        await chair.controller.recall_memory(2, profile, request=reservation)
    assert len([p for p in chair.writes if p[5] == 0x11]) == 2
    assert chair.controller._tap_session is None
    assert not chair.controller.memory_requests


async def test_shutdown_cancels_owner_and_joiners_and_clears_session(chair):
    gate = GatedPulse(chair)
    owner = asyncio.create_task(chair.controller.step_position(0, 1))
    await gate.tick()
    joined = asyncio.create_task(chair.controller.step_position(0, 1))
    await asyncio.sleep(0)
    await chair.controller.cancel_active()
    results = await asyncio.gather(owner, joined, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert len(directions(chair)) == 1 and len(stops(chair)) == 1
    assert chair.controller._tap_session is None


async def test_extended_blocked_write_still_honors_wall_clock_cap(chair, monkeypatch):
    from custom_components.svago import controller

    monkeypatch.setattr(controller, "MOTOR_PULSE_SECONDS", (0.03, 0.25, 0.25, 0.5))
    monkeypatch.setattr(controller, "MOTOR_BURST_MAX_SECONDS", 0.04)
    entered = asyncio.Event()

    async def blocked_direction(packet):
        await chair.write(packet)
        if packet[13] == 0x82:
            entered.set()
            await asyncio.Event().wait()

    chair.controller._write = blocked_direction
    owner = asyncio.create_task(chair.controller.step_position(0, 1))
    await entered.wait()
    joined = [asyncio.create_task(chair.controller.step_position(0, 1)) for _ in range(10)]
    results = await asyncio.wait_for(asyncio.gather(owner, *joined, return_exceptions=True), 0.5)
    assert all(isinstance(result, ControlError) for result in results)
    assert all("timed out" in str(result) for result in results)
    assert len(directions(chair)) == 1 and len(stops(chair)) == 1
    assert chair.controller._tap_session is None
