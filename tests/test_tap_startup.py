"""Short taps allow captured motor startup latency without replaying an action."""

import asyncio
from copy import deepcopy

import pytest
from conftest import status_with

from custom_components.svago import controller as controller_module
from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.protocol import OBSERVED_BOUNDS, Status, checksum


def prepare(chair, *, index=3, position=8):
    """Use the option context shared by the captured failed taps and working sliders."""
    positions = [1, 2, 19, 8]
    positions[index] = position
    chair.controller.accept_status(
        status_with(
            positions=positions,
            heat=True,
            massage=0,
            **{"6": 3, "7": 0x84, "8": 0x2A, "9": 0x30, "10": 2, "20": 1, "21": 0, "22": 1},
        )
    )
    chair.feedback = False
    chair.activation_feedback = True


def report(chair, index, position):
    raw = bytearray(chair.controller.status.raw)
    raw[5] = 0
    raw[13 + index] = position
    raw[25] = checksum(raw)
    chair.controller.accept_status(Status(bytes(raw)))


def directions(chair):
    return [
        packet for packet in chair.writes if any(value in (0x81, 0x82) for value in packet[13:17])
    ]


def stops(chair):
    return [packet for packet in chair.writes if packet[13:17] == b"\x80" * 4]


class StartupGate:
    """Drive fresh feedback while holding startup at each observation poll."""

    def __init__(self, chair):
        self.chair = chair
        self.ticks = asyncio.Queue()
        self.permits = asyncio.Queue()
        self.free = False
        self.feedback = lambda: chair.controller.accept_status(chair.controller.status)
        chair.controller._sleep = self.sleep

    async def sleep(self, seconds):
        self.chair.time += seconds
        self.feedback()
        self.ticks.put_nowait(self.chair.time)
        if not self.free:
            await self.permits.get()
        await asyncio.sleep(0)

    async def after_nominal(self):
        while True:
            tick = await asyncio.wait_for(self.ticks.get(), 1)
            if tick > 0.5:
                return tick
            self.permits.put_nowait(None)

    def finish(self):
        self.free = True
        self.permits.put_nowait(None)


@pytest.mark.parametrize(
    "index,direction,start,reports,expected_hex",
    [
        (
            3,
            -1,
            12,
            ((0.665, 13), (0.868, 12), (1.481, 11)),
            "7E020100100003842A300200008F8F8F810000010100010000277E",
        ),
        (
            3,
            1,
            8,
            ((0.627, 9),),
            "7E020100100003842A300200008F8F8F820000010100010000287E",
        ),
    ],
)
async def test_delayed_startup_continues_same_direction_until_first_net_progress(
    chair, index, direction, start, reports, expected_hex
):
    prepare(chair, index=index, position=start)

    def feedback():
        observed = start
        for second, position in reports:
            if chair.time >= second:
                observed = position
        report(chair, index, observed)

    chair.on_sleep = feedback
    await chair.controller.step_position(index, direction)

    first_progress = next(
        second for second, position in reports if (position - start) * direction > 0
    )
    assert first_progress <= chair.time < first_progress + 0.11
    packets = directions(chair)
    assert packets and all(packet == packets[0] for packet in packets)
    if expected_hex:
        assert packets[0].hex().upper() == expected_hex
    assert len([packet for packet in chair.writes if packet[5] == 0x11]) == 1
    assert len(stops(chair)) == 1
    assert chair.writes[-1] == stops(chair)[0]
    assert chair.controller.status.positions[index] == reports[-1][1]
    trace = chair.controller.tap_requests[-1]
    assert trace["outcome"] == "pulse_completed"
    assert trace["startup_allowance_seconds"] == 2
    assert trace["startup_extended"] is True
    assert first_progress <= trace["first_progress_seconds"] < first_progress + 0.11
    assert trace["startup_outcome"] == "progress_observed"
    assert len(trace["direction_write_log"]) <= 8


@pytest.mark.parametrize(
    "index,nominal,writes", [(0, 0.5, 5), (1, 0.25, 3), (2, 0.25, 3), (3, 0.5, 5)]
)
async def test_prompt_progress_keeps_existing_nominal_tap(chair, index, nominal, writes):
    prepare(chair, index=index, position=5)

    async def sleep_with_early_feedback(seconds):
        # A prompt report arrives before the first direction-repeat interval.
        finished = chair.time + seconds
        chair.time += seconds / 2
        report(chair, index, 6)
        chair.time = finished
        await asyncio.sleep(0)

    chair.controller._sleep = sleep_with_early_feedback
    await chair.controller.step_position(index, 1)

    assert chair.time == pytest.approx(nominal)
    assert len(directions(chair)) == writes
    assert len(stops(chair)) == 1
    trace = chair.controller.tap_requests[-1]
    assert trace["startup_extended"] is False
    assert trace["first_progress_seconds"] == pytest.approx(min(0.1, nominal) / 2)
    assert trace["startup_outcome"] == "progress_observed"


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("feedback", ["unchanged", "none", "late"])
async def test_timed_axis_stops_at_deadline_without_reported_progress(
    chair, index, direction, feedback
):
    prepare(chair, index=index, position=5)
    if feedback != "none":
        chair.on_sleep = lambda: report(chair, index, 5)

    await chair.controller.step_position(index, direction)

    assert chair.time == pytest.approx(0.25)
    assert len(directions(chair)) == 3
    assert len(stops(chair)) == 1
    assert chair.writes[-1] == stops(chair)[0]
    assert len([packet for packet in chair.writes if packet[5] == 0x11]) == 1
    trace = chair.controller.tap_requests[-1]
    assert trace["outcome"] == "pulse_completed"
    assert trace["feedback_changed"] is False
    assert trace["startup_allowance_seconds"] == 0
    assert trace["startup_extended"] is False
    assert trace["startup_outcome"] == "not_observed_during_timed_pulse"
    assert trace["first_progress_seconds"] is None
    assert [record["seconds"] for record in trace["direction_write_log"]] == [0, 0.1, 0.2]
    assert trace["stop"]["seconds"] == 0.25
    assert chair.controller._tap_session is None
    if feedback == "late":
        trace = deepcopy(trace)
        count = len(chair.writes)
        chair.time = 0.6
        report(chair, index, 5 + direction)
        await asyncio.sleep(0)
        assert len(chair.writes) == count
        assert chair.controller.tap_requests[-1] == trace


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize("blocked", ["write", "sleep"])
async def test_timed_axis_wall_deadline_releases_without_extending_for_progress(
    chair, index, blocked
):
    prepare(chair, index=index, position=5)

    if blocked == "write":

        async def write(packet):
            await chair.write(packet)
            if packet[13 + index] == 0x82:
                await asyncio.Event().wait()

        chair.controller._write = write
        with pytest.raises(ControlError, match="Movement write timed out"):
            await asyncio.wait_for(chair.controller.step_position(index, 1), 1)
    else:

        async def sleep(_seconds):
            await asyncio.Event().wait()

        chair.controller._sleep = sleep
        await asyncio.wait_for(chair.controller.step_position(index, 1), 1)

    assert len(directions(chair)) == 1
    assert len(stops(chair)) == 1
    trace = chair.controller.tap_requests[-1]
    assert trace["outcome"] == ("failed" if blocked == "write" else "pulse_completed")
    assert trace["startup_outcome"] == (
        "write_timed_out" if blocked == "write" else "not_observed_during_timed_pulse"
    )
    assert trace["startup_extended"] is False
    assert chair.controller._tap_session is None


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize("progress", [False, True])
async def test_early_sleep_return_cannot_add_or_accelerate_timed_axis_repeats(
    chair, index, progress
):
    prepare(chair, index=index, position=5)

    async def sleep(seconds):
        # Return early from both the normal repeat sleep and the shorter final
        # sleep near 0.25 s; neither permits an early or extra direction write.
        chair.time += seconds - 0.001 if seconds > 0.01 else seconds
        report(chair, index, 6 if progress else 5)
        await asyncio.sleep(0)

    chair.controller._sleep = sleep
    await chair.controller.step_position(index, 1)

    assert chair.time == pytest.approx(0.25)
    assert len(directions(chair)) == 3
    assert len(stops(chair)) == 1
    trace = chair.controller.tap_requests[-1]
    assert [record["seconds"] for record in trace["direction_write_log"]] == [0, 0.1, 0.2]
    assert trace["stop"]["seconds"] == 0.25
    assert trace["startup_extended"] is False


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("feedback", ["reverse", "out_of_bounds", "stale"])
async def test_timed_axis_preserves_feedback_guards(chair, index, direction, feedback):
    prepare(chair, index=index, position=5)
    if feedback == "stale":
        chair.controller._stale_after = 0.01
    else:
        chair.on_sleep = lambda: report(
            chair,
            index,
            5 - 2 * direction if feedback == "reverse" else OBSERVED_BOUNDS[index][1] + 1,
        )

    with pytest.raises(ControlError, match="Fresh|Unexpected position"):
        await chair.controller.step_position(index, direction)

    assert len(directions(chair)) == 1
    assert len(stops(chair)) == (0 if feedback == "stale" else 1)
    assert chair.controller.tap_requests[-1]["outcome"] == "failed"
    assert chair.controller._needs_stop is (feedback == "stale")


@pytest.mark.parametrize("index", [1, 2])
async def test_timed_axis_failed_stop_requires_handoff_before_next_wake(chair, index):
    prepare(chair, index=index, position=5)

    def fail_stop(packet):
        if packet[13:17] == b"\x80" * 4:
            raise OSError("stop delivery is uncertain")

    chair.on_write = fail_stop
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.step_position(index, 1)
    assert chair.controller._needs_stop
    assert len(directions(chair)) == 3
    count = len(chair.writes)
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.turn_on()
    assert len(chair.writes) == count + 1
    assert chair.writes[-1][13:17] == b"\x80" * 4

    chair.on_write = None
    count = len(chair.writes)
    await chair.controller.turn_on()
    assert chair.writes[count][13:17] == b"\x80" * 4
    assert chair.writes[count + 1][5] == 0x11


@pytest.mark.parametrize("other_axis_moves", [False, True])
async def test_no_requested_progress_stops_and_fails_at_startup_bound(chair, other_axis_moves):
    prepare(chair)
    chair.on_sleep = lambda: report(
        chair, 2 if other_axis_moves else 3, 20 if other_axis_moves else 8
    )

    with pytest.raises(ControlError, match="did not report movement"):
        await chair.controller.step_position(3, 1)

    assert chair.time == pytest.approx(2)
    assert 1 <= len(directions(chair)) <= 21
    assert len(stops(chair)) == 1
    assert len([packet for packet in chair.writes if packet[5] == 0x11]) == 1
    trace = chair.controller.tap_requests[-1]
    assert trace["outcome"] == "failed"
    assert trace["startup_outcome"] == "no_progress"
    assert trace["first_progress_seconds"] is None
    assert not chair.controller.busy
    assert chair.controller._tap_session is None


@pytest.mark.parametrize("positions", [(6,), (9, 8), (29,)])
async def test_startup_does_not_relax_reverse_or_bounds_checks(chair, positions):
    prepare(chair)
    reports = iter(positions)
    chair.on_sleep = lambda: report(chair, 3, next(reports))

    with pytest.raises(ControlError, match="Unexpected position"):
        await chair.controller.step_position(3, 1)

    assert chair.time < 0.5
    assert len(stops(chair)) == 1
    assert len(directions(chair)) == len(positions)


async def test_startup_stops_at_observed_endpoint_without_waiting_full_allowance(chair):
    prepare(chair, position=27)
    chair.on_sleep = lambda: report(chair, 3, 28 if chair.time >= 0.6 else 27)

    await chair.controller.step_position(3, 1)

    assert chair.time == pytest.approx(0.6)
    assert len(stops(chair)) == 1
    assert chair.controller.status.positions[3] == 28


async def test_stale_status_during_startup_prevents_more_direction_or_stop_packets(chair):
    prepare(chair)
    chair.controller._stale_after = 0.25

    with pytest.raises(ControlError, match="Fresh"):
        await chair.controller.step_position(3, 1)

    assert chair.time == pytest.approx(0.3)
    assert len(directions(chair)) == 3
    assert not stops(chair)


async def test_same_direction_press_during_extended_startup_joins_existing_gesture(chair):
    prepare(chair)
    gate = StartupGate(chair)
    owner = asyncio.create_task(chair.controller.step_position(3, 1))
    joined = None
    try:
        joined_at = await gate.after_nominal()
        joined = asyncio.create_task(chair.controller.step_position(3, 1))
        await asyncio.sleep(0)
        assert not owner.done()
        assert chair.controller.tap_requests[-1]["coalesced_into_burst"]
        gate.feedback = lambda: report(chair, 3, 9)
        gate.finish()
        assert await asyncio.gather(owner, joined) == [None, None]
        assert chair.time == pytest.approx(joined_at + 0.5)
        assert len(stops(chair)) == 1
        assert len([packet for packet in chair.writes if packet[5] == 0x11]) == 1
    finally:
        tasks = [task for task in (owner, joined) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_repeated_startup_presses_cannot_extend_no_progress_limit(chair):
    prepare(chair)
    gate = StartupGate(chair)
    owner = asyncio.create_task(chair.controller.step_position(3, 1))
    joined = []
    try:
        for _ in range(19):
            tick = await asyncio.wait_for(gate.ticks.get(), 1)
            assert tick < 2
            joined.append(asyncio.create_task(chair.controller.step_position(3, 1)))
            await asyncio.sleep(0)
            gate.permits.put_nowait(None)
        gate.finish()
        results = await asyncio.gather(owner, *joined, return_exceptions=True)
        assert all(isinstance(result, ControlError) for result in results)
        assert all("did not report movement" in str(result) for result in results)
        assert chair.time == pytest.approx(2)
        assert len(stops(chair)) == 1
        assert len(directions(chair)) <= 21
        assert len([packet for packet in chair.writes if packet[5] == 0x11]) == 1
        assert chair.controller._tap_session is None
        count = len(chair.writes)
        await asyncio.sleep(0)
        assert len(chair.writes) == count
    finally:
        for task in (owner, *joined):
            task.cancel()
        await asyncio.gather(owner, *joined, return_exceptions=True)


@pytest.mark.parametrize("interruption", ["stop", "cancel", "reconnect"])
async def test_extended_startup_remains_interruptible_without_replay(chair, interruption):
    prepare(chair)
    gate = StartupGate(chair)
    owner = asyncio.create_task(chair.controller.step_position(3, 1))
    try:
        await gate.after_nominal()
        count = len(chair.writes)
        if interruption == "stop":
            await chair.controller.stop_movement()
            error = CommandSuperseded
        elif interruption == "cancel":
            owner.cancel()
            error = asyncio.CancelledError
        else:
            chair.controller.set_connected(False)
            chair.controller.set_connected(True)
            prepare(chair)
            gate.finish()
            error = ControlError
        with pytest.raises(error):
            await owner
        if interruption == "reconnect":
            assert len(chair.writes) == count
        else:
            assert all(packet[13:17] == b"\x80" * 4 for packet in chair.writes[count:])
            assert stops(chair)
        count = len(chair.writes)
        gate.finish()
        await asyncio.sleep(0)
        assert len(chair.writes) == count
        assert chair.controller._tap_session is None
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.parametrize("blocked", ["write", "sleep"])
async def test_wall_deadline_bounds_startup_when_clock_stops(chair, monkeypatch, blocked):
    prepare(chair)
    monkeypatch.setattr(controller_module, "MOTOR_PULSE_SECONDS", (0.01,) * 4)
    monkeypatch.setattr(controller_module, "STARTUP_FEEDBACK_SECONDS", 0.03)
    monkeypatch.setattr(controller_module, "MOTOR_BURST_MAX_SECONDS", 0.05)

    if blocked == "write":

        async def write(packet):
            await chair.write(packet)
            if packet[16] == 0x82:
                await asyncio.Event().wait()

        chair.controller._write = write
    else:

        async def sleep(_seconds):
            await asyncio.Event().wait()

        chair.controller._sleep = sleep

    with pytest.raises(ControlError):
        await asyncio.wait_for(chair.controller.step_position(3, 1), 1)

    assert len(directions(chair)) == 1
    assert len(stops(chair)) == 1
    assert chair.controller.tap_requests[-1]["outcome"] == "failed"
    assert chair.controller._tap_session is None


async def test_write_count_ceiling_cannot_report_success_without_progress(chair):
    prepare(chair)

    async def frozen_clock_sleep(_seconds):
        chair.controller.accept_status(chair.controller.status)
        await asyncio.sleep(0)

    chair.controller._sleep = frozen_clock_sleep
    with pytest.raises(ControlError, match="did not report movement"):
        await chair.controller.step_position(3, 1)

    assert len(directions(chair)) == 30
    assert len(stops(chair)) == 1
    assert chair.controller.tap_requests[-1]["startup_outcome"] == "no_progress"
    assert chair.controller._tap_session is None


async def test_progress_during_blocked_write_restores_nominal_wall_timeout(chair, monkeypatch):
    prepare(chair)
    monkeypatch.setattr(controller_module, "MOTOR_PULSE_SECONDS", (0.03,) * 4)
    monkeypatch.setattr(controller_module, "STARTUP_FEEDBACK_SECONDS", 0.2)
    monkeypatch.setattr(controller_module, "MOTOR_BURST_MAX_SECONDS", 0.3)

    async def write(packet):
        await chair.write(packet)
        if packet[16] == 0x82:
            report(chair, 3, 9)
            await asyncio.Event().wait()

    chair.controller._write = write
    with pytest.raises(ControlError, match="timed out"):
        await asyncio.wait_for(chair.controller.step_position(3, 1), 0.12)

    assert len(directions(chair)) == 1
    assert len(stops(chair)) == 1
    assert chair.controller.tap_requests[-1]["first_progress_seconds"] == 0
    assert chair.controller._tap_session is None


async def test_queued_join_cannot_extend_invalid_feedback_during_blocked_write(chair):
    prepare(chair)
    entered = asyncio.Event()
    direction_count = 0

    async def block_first_direction(packet):
        nonlocal direction_count
        await chair.write(packet)
        if packet[16] == 0x82:
            direction_count += 1
            if direction_count == 1:
                entered.set()
                await asyncio.Event().wait()

    chair.controller._write = block_first_direction
    owner = asyncio.create_task(chair.controller.step_position(3, 1))
    joined = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        # Queue the join before the observer schedules its immediate timeout.
        # It must not turn that invalid-feedback abort into another 2s hold.
        joined = asyncio.create_task(chair.controller.step_position(3, 1))
        report(chair, 3, 29)
        report(chair, 3, 8)  # Fresh recovery permits the new request after Stop.
        chair.feedback = True
        results = await asyncio.wait_for(asyncio.gather(owner, joined, return_exceptions=True), 0.5)

        assert isinstance(results[0], (CommandSuperseded, ControlError))
        assert results[1] is None
        activations = [i for i, packet in enumerate(chair.writes) if packet[5] == 0x11]
        assert len(activations) == 2
        old_direction = next(i for i, packet in enumerate(chair.writes) if packet[16] == 0x82)
        before_replacement = chair.writes[old_direction + 1 : activations[1]]
        assert before_replacement
        assert all(packet[13:17] == b"\x80" * 4 for packet in before_replacement)
        assert "coalesced_into_burst" not in chair.controller.tap_requests[-1]
        assert chair.controller._tap_session is None
    finally:
        tasks = [owner, *([joined] if joined is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("reconnect", [False, True])
async def test_failed_startup_stop_requires_handoff_only_on_original_connection(chair, reconnect):
    prepare(chair)
    chair.on_sleep = lambda: chair.controller.accept_status(chair.controller.status)

    def fail_stop(packet):
        if packet[13:17] == b"\x80" * 4:
            raise OSError("stop delivery is uncertain")

    chair.on_write = fail_stop
    with pytest.raises(ControlError, match="did not report movement"):
        await chair.controller.step_position(3, 1)

    count = len(chair.writes)
    if reconnect:
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        prepare(chair)
    else:
        with pytest.raises(ControlError, match="not retried"):
            await chair.controller.turn_on()
        assert len(chair.writes) == count + 1
        assert chair.writes[-1][13:17] == b"\x80" * 4
    chair.on_write = None
    count = len(chair.writes)
    await chair.controller.turn_on()
    following = chair.writes[count:]
    assert len(following) == (1 if reconnect else 2)
    if not reconnect:
        assert following[0][13:17] == b"\x80" * 4
    assert following[-1][5] == 0x11
