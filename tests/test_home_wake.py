"""A single explicit wake after native Home settles before dependent commands."""

import asyncio

import pytest
from conftest import status_with
from test_home import CAPTURED_HOME, HomeChair

from custom_components.svago import controller as controller_module
from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.memory_protocol import MemoryProfile


@pytest.fixture
async def wake_chair():
    chair = HomeChair()
    yield chair
    await chair.controller.cancel_active()


def refresh(chair):
    chair.controller.accept_status(chair.controller.status)


async def complete_home(chair):
    chair.on_sleep = chair.report
    await chair.controller.return_home()
    chair.on_sleep = lambda: refresh(chair)


def memory_profile():
    return MemoryProfile.from_status(status_with(positions=(2, 3, 4, 5), heat=False, massage=0))


def memory_packets(chair):
    return [packet for packet in chair.writes if packet[5] == 0 and packet[10] == 0x12]


def record_writes(chair):
    observed = []
    original = chair.controller._write

    async def write(packet):
        observed.append((chair.time, packet))
        await original(packet)

    chair.controller._write = write
    return observed


async def test_home_then_memory_sends_one_on_then_one_target_after_settle(wake_chair):
    chair = wake_chair
    await complete_home(chair)
    events = record_writes(chair)
    started = chair.time

    await chair.controller.recall_memory(2, memory_profile())

    assert len(events) == 2
    assert events[0][1][5] == 0x11
    assert events[0][0] == started
    assert 1 - 1e-9 <= events[1][0] - started <= 1.2
    assert events[1][1] == memory_packets(chair)[0]
    assert len(memory_packets(chair)) == 1
    assert chair.writes.count(CAPTURED_HOME) == 1
    assert chair.controller.memory_requests[-1]["outcome"] == "target_reached"
    settling = chair.controller.last_activation["post_home_settle"]
    assert settling["required_seconds"] == 1
    assert 1 <= settling["duration_seconds"] <= 1.2
    assert settling["status_reports"] >= 1
    assert settling["outcome"] == "settled"
    assert chair.controller.last_activation["outcome"] == "acknowledged"

    started = chair.time
    count = len(chair.writes)
    await chair.controller.turn_on()
    assert chair.time == started
    assert len(chair.writes) == count + 1
    assert "post_home_settle" not in chair.controller.last_activation


async def test_home_then_movement_settles_once_before_direction_write(wake_chair):
    chair = wake_chair
    await complete_home(chair)
    events = record_writes(chair)
    started = chair.time

    await chair.controller.step_position(0, 1)

    activation = [event for event in events if event[1][5] == 0x11]
    direction = [event for event in events if event[1][13] == 0x82]
    assert len(activation) == 1
    assert direction and direction[0][0] - started >= 1 - 1e-9
    assert events[-1][1][13:17] == b"\x80" * 4
    assert chair.controller.tap_requests[-1]["outcome"] == "pulse_completed"


async def test_normal_movement_without_home_has_no_settle_delay(chair):
    events = record_writes(chair)
    await chair.controller.step_position(0, 1)
    assert events[0][1][5] == 0x11
    assert events[1][1][13] == 0x82
    assert events[1][0] == events[0][0]
    assert chair.time == pytest.approx(0.5)
    assert "post_home_settle" not in chair.controller.last_activation


@pytest.mark.parametrize("reconnect", [False, True])
async def test_uncertain_home_delivery_still_requires_wake_settle(wake_chair, reconnect):
    chair = wake_chair
    chair.fail_at = 1
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.return_home()
    chair.fail_at = None
    if reconnect:
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
    chair.report()
    chair.on_sleep = lambda: refresh(chair)
    started = chair.time
    count = len(chair.writes)

    await chair.controller.turn_on()

    assert chair.time - started >= 1 - 1e-9
    assert chair.controller.last_activation["post_home_settle"]["outcome"] == "settled"
    following = chair.writes[count:]
    assert len([packet for packet in following if packet[5] == 0x11]) == 1
    assert len(following) == (1 if reconnect else 2)
    assert chair.writes.count(CAPTURED_HOME) == 1


async def test_home_rejected_before_dispatch_does_not_add_wake_delay(wake_chair):
    chair = wake_chair
    chair.time = 4
    with pytest.raises(ControlError, match="Fresh"):
        await chair.controller.return_home()
    assert not chair.writes
    chair.report()
    started = chair.time

    await chair.controller.turn_on()

    assert chair.time == started
    assert len(chair.writes) == 1
    assert "post_home_settle" not in chair.controller.last_activation


@pytest.mark.parametrize("problem", ["no_new_status", "stale", "activation_failure"])
async def test_failed_wake_sends_no_memory_target_and_retains_pending_settle(wake_chair, problem):
    chair = wake_chair
    await complete_home(chair)
    chair.writes.clear()
    chair.on_sleep = None
    if problem == "stale":
        chair.controller._stale_after = 0.25
    elif problem == "activation_failure":
        chair.fail_at = 1

    with pytest.raises(ControlError):
        await chair.controller.recall_memory(2, memory_profile())

    assert len(chair.writes) == 1
    assert chair.writes[0][5] == 0x11
    assert not memory_packets(chair)
    chair.fail_at = None
    chair.controller._stale_after = 3
    chair.report()
    chair.on_sleep = lambda: refresh(chair)
    started = chair.time
    await chair.controller.turn_on()
    assert chair.time - started >= 1 - 1e-9
    assert chair.controller.last_activation["post_home_settle"]["outcome"] == "settled"


@pytest.mark.parametrize("interruption", ["stop", "cancel", "reconnect"])
async def test_interrupted_post_home_wake_cannot_send_old_memory_target(wake_chair, interruption):
    chair = wake_chair
    await complete_home(chair)
    chair.writes.clear()
    waiting, release = asyncio.Event(), asyncio.Event()

    async def wait(_seconds):
        waiting.set()
        await release.wait()

    chair.controller._sleep = wait
    task = asyncio.create_task(chair.controller.recall_memory(2, memory_profile()))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert chair.controller.last_activation["outcome"] == "waiting_after_home"
        assert chair.controller.last_activation["post_home_settle"]["outcome"] == "waiting"
        assert len(chair.writes) == 1 and chair.writes[0][5] == 0x11
        if interruption == "stop":
            await chair.controller.stop_movement()
            error = CommandSuperseded
        elif interruption == "cancel":
            task.cancel()
            error = asyncio.CancelledError
        else:
            chair.controller.set_connected(False)
            chair.controller.set_connected(True)
            chair.report()
            release.set()
            error = ControlError
        with pytest.raises(error):
            await task
        assert not memory_packets(chair)
        if interruption == "reconnect":
            assert len(chair.writes) == 1
        chair.controller._sleep = chair.sleep
        chair.report()
        chair.on_sleep = lambda: refresh(chair)
        started = chair.time
        await chair.controller.turn_on()
        assert chair.time - started >= 1 - 1e-9
        assert chair.controller.last_activation["post_home_settle"]["outcome"] == "settled"
        assert not memory_packets(chair)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_new_home_during_pending_wake_requires_its_own_later_settle(wake_chair):
    chair = wake_chair
    await complete_home(chair)
    waiting = asyncio.Event()

    async def wait(_seconds):
        waiting.set()
        await asyncio.Event().wait()

    chair.controller._sleep = wait
    task = asyncio.create_task(chair.controller.recall_memory(2, memory_profile()))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        chair.controller._sleep = chair.sleep
        chair.on_sleep = chair.report
        await chair.controller.return_home()
        with pytest.raises(CommandSuperseded):
            await task
        assert chair.writes.count(CAPTURED_HOME) == 2
        assert not memory_packets(chair)
        chair.on_sleep = lambda: refresh(chair)
        started = chair.time
        await chair.controller.turn_on()
        assert chair.time - started >= 1 - 1e-9
        assert chair.controller.last_activation["post_home_settle"]["outcome"] == "settled"
        assert not memory_packets(chair)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_early_status_during_settle_cannot_replace_a_report_after_full_wait(wake_chair):
    chair = wake_chair
    await complete_home(chair)
    chair.writes.clear()
    reported = False

    def one_early_report():
        nonlocal reported
        if not reported:
            reported = True
            refresh(chair)

    chair.on_sleep = one_early_report
    started = chair.time
    with pytest.raises(ControlError):
        await chair.controller.recall_memory(2, memory_profile())

    assert reported
    assert 1 < chair.time - started <= 4
    assert len(chair.writes) == 1 and chair.writes[0][5] == 0x11
    assert not memory_packets(chair)
    settling = chair.controller.last_activation["post_home_settle"]
    assert settling["outcome"] == "failed"
    assert settling["status_reports"] == 1


async def test_final_status_after_one_second_releases_one_memory_target(wake_chair):
    chair = wake_chair
    await complete_home(chair)
    started = chair.time
    events = record_writes(chair)
    chair.on_sleep = lambda: refresh(chair) if chair.time - started >= 1.3 else None

    await chair.controller.recall_memory(2, memory_profile())

    assert len(events) == 2
    assert events[0][1][5] == 0x11
    assert 1.3 <= events[1][0] - started <= 1.5
    assert len(memory_packets(chair)) == 1
    assert chair.controller.last_activation["post_home_settle"]["outcome"] == "settled"


async def test_real_clock_bounds_failed_settle_when_injected_clock_stops(wake_chair, monkeypatch):
    chair = wake_chair
    await complete_home(chair)
    monkeypatch.setattr(controller_module, "POST_HOME_WAKE_SECONDS", 0.03)
    chair.controller._stale_after = 0.02
    count = len(chair.writes)

    async def blocked_sleep(_seconds):
        refresh(chair)
        await asyncio.Event().wait()

    chair.controller._sleep = blocked_sleep
    with pytest.raises(ControlError):
        await asyncio.wait_for(chair.controller.turn_on(), 1)

    assert len(chair.writes) == count + 1
    assert chair.writes[-1][5] == 0x11
    assert chair.controller.last_activation["post_home_settle"]["outcome"] == "failed"
