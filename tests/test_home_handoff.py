"""Native Home completes before one latest follow-up, with immediate explicit Stop."""

import asyncio

import pytest
from conftest import status_with
from test_home import CAPTURED_HOME, HomeChair

from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.memory_protocol import MemoryProfile


class HeldHome(HomeChair):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.tasks = []
        self.dispatches = []

    def launch(self, action):
        task = asyncio.create_task(action)
        self.tasks.append(task)
        return task

    async def start_home(self):
        task = self.launch(self.controller.return_home())
        await asyncio.wait_for(self.entered.wait(), 1)
        return task

    async def write(self, payload):
        self.dispatches.append((payload, (self.controller.last_home_request or {}).get("outcome")))
        await super().write(payload)

    async def sleep(self, seconds):
        if self.controller._home_session is not None:
            self.entered.set()
            await self.release.wait()
            self.time += seconds
            self.report()
        else:
            self.time += seconds
            self.controller.accept_status(self.controller.status)
        await asyncio.sleep(0)


@pytest.fixture
async def held_home():
    chair = HeldHome()
    try:
        yield chair
    finally:
        for task in chair.tasks:
            task.cancel()
        await asyncio.gather(*chair.tasks, return_exceptions=True)
        await chair.controller.cancel_active()


def follow_up(controller, kind):
    actions = {
        "tap": lambda: controller.step_position(3, 1),
        "backrest_tap": lambda: controller.step_position(1, 1),
        "slider": lambda: controller.set_position(3, 2),
        "lumbar": lambda: controller.adjust_lumbar(True),
        "preset": lambda: controller.select_preset("zgr"),
        "memory": lambda: controller.recall_memory(
            2, MemoryProfile.from_status(status_with(positions=(1, 2, 3, 4)))
        ),
        "heat_on": lambda: controller.set_heat(True),
        "heat_off": lambda: controller.set_heat(False),
        "massage_on": lambda: controller.set_massage("Gentle"),
        "massage_off": lambda: controller.set_massage("Off"),
        "heat_continuous": controller.set_heat_continuous,
        "massage_continuous": controller.set_massage_continuous,
        "timer": lambda: controller.set_timer("heat", 120),
        "zone": lambda: controller.set_heat_level("footrest", "High"),
        "internal_on": controller.turn_on,
    }
    return actions[kind]()


@pytest.mark.parametrize(
    "kind",
    [
        "tap",
        "backrest_tap",
        "slider",
        "lumbar",
        "preset",
        "memory",
        "heat_on",
        "heat_off",
        "massage_on",
        "massage_off",
        "heat_continuous",
        "massage_continuous",
        "timer",
        "zone",
        "internal_on",
    ],
)
async def test_foreground_controls_wait_for_verified_home(held_home, kind):
    chair = held_home
    home = await chair.start_home()
    pending = chair.launch(follow_up(chair.controller, kind))
    await asyncio.sleep(0)
    assert not home.done() and not pending.done()
    assert chair.writes == [CAPTURED_HOME]
    assert not chair.controller._transition.locked()
    assert not chair.controller._settings_lock.locked()

    chair.release.set()
    await asyncio.wait_for(asyncio.gather(home, pending), 1)

    assert chair.writes.count(CAPTURED_HOME) == 1
    assert chair.controller.last_home_request["outcome"] == "reported_neutral"
    assert chair.controller.last_home_request["neutral_stable_seconds"] >= 2
    assert all(outcome == "reported_neutral" for _, outcome in chair.dispatches[1:])


@pytest.mark.parametrize("latest", ["tap", "backrest_tap", "zone"])
async def test_only_latest_of_mixed_waiting_controls_dispatches(held_home, latest):
    chair = held_home
    home = await chair.start_home()
    older = []
    for kind in ["tap", "timer", "memory", "heat_on"]:
        older.append(chair.launch(follow_up(chair.controller, kind)))
        await asyncio.sleep(0)
    pending = chair.launch(follow_up(chair.controller, latest))
    await asyncio.sleep(0)
    assert chair.writes == [CAPTURED_HOME]
    chair.release.set()
    await asyncio.wait_for(asyncio.gather(home, pending), 1)
    results = await asyncio.gather(*older, return_exceptions=True)
    assert all(isinstance(result, CommandSuperseded) for result in results)
    assert not chair.controller.memory_requests[-1].get("command_hex")
    if latest in ("tap", "backrest_tap"):
        index = 3 if latest == "tap" else 1
        assert any(packet[13 + index] == 0x82 for packet in chair.writes[1:])
        if latest == "backrest_tap":
            trace = chair.controller.tap_requests[-1]
            assert trace["movement_duration_seconds"] == pytest.approx(0.25)
            assert trace["startup_extended"] is False
    else:
        assert len(chair.writes) == 2
        assert chair.writes[-1][22] == 3
        assert not any(packet[5] == 0x11 for packet in chair.writes)


async def test_settings_wait_before_lock_and_only_latest_executes(held_home):
    chair = held_home
    home = await chair.start_home()
    settings = []
    for level in ["Low", "Medium", "High"]:
        settings.append(chair.launch(chair.controller.set_heat_level("footrest", level)))
        await asyncio.sleep(0)
    assert not chair.controller._settings_lock.locked()
    assert chair.writes == [CAPTURED_HOME]
    chair.release.set()
    await asyncio.wait_for(home, 1)
    results = await asyncio.gather(*settings, return_exceptions=True)
    assert all(isinstance(result, CommandSuperseded) for result in results[:-1])
    assert results[-1] is None
    assert len(chair.writes) == 2 and chair.writes[-1][22] == 3


async def test_setting_queued_before_home_cannot_interrupt_it(held_home):
    chair = held_home
    async with chair.controller._settings_lock:
        old = chair.launch(chair.controller.set_heat_level("footrest", "High"))
        await asyncio.sleep(0)
        home = await chair.start_home()
    with pytest.raises(CommandSuperseded):
        await old
    assert chair.writes == [CAPTURED_HOME] and not home.done()
    chair.release.set()
    await asyncio.wait_for(home, 1)
    assert chair.writes == [CAPTURED_HOME]


async def test_new_setting_after_home_replaces_follow_up_still_waiting_for_lock(held_home):
    chair = held_home
    home = await chair.start_home()
    older = chair.launch(chair.controller.set_heat_level("footrest", "Low"))
    await asyncio.sleep(0)
    async with chair.controller._settings_lock:
        chair.release.set()
        await asyncio.wait_for(home, 1)
        # The original follow-up passed Home but still cannot dispatch. This
        # normal setting registers its newer intent before obtaining the lock.
        newer = chair.launch(chair.controller.set_heat_level("footrest", "High"))
        await asyncio.sleep(0)
    with pytest.raises(CommandSuperseded):
        await older
    await asyncio.wait_for(newer, 1)
    assert len(chair.writes) == 2 and chair.writes[-1][22] == 3


async def test_dispatched_home_follow_up_failure_invalidates_newer_queued_setting(held_home):
    chair = held_home
    home = await chair.start_home()
    dispatched = asyncio.Event()
    chair.feedback = False
    chair.on_write = lambda packet: dispatched.set() if packet != CAPTURED_HOME else None
    first = chair.launch(chair.controller.set_heat_level("footrest", "High"))
    await asyncio.sleep(0)
    chair.release.set()
    await asyncio.wait_for(home, 1)
    await asyncio.wait_for(dispatched.wait(), 1)
    assert chair.controller._settings_lock.locked()
    newer = chair.launch(chair.controller.set_heat_level("footrest", "Low"))
    await asyncio.sleep(0)
    with pytest.raises(ControlError, match="did not confirm"):
        await first
    with pytest.raises(CommandSuperseded):
        await newer
    assert len(chair.writes) == 2 and chair.writes[-1][22] == 3


@pytest.mark.parametrize("cancel_latest", [False, True])
async def test_cancelled_waiter_cannot_cancel_home_or_revive_older(held_home, cancel_latest):
    chair = held_home
    home = await chair.start_home()
    older = chair.launch(chair.controller.step_position(0, 1))
    await asyncio.sleep(0)
    latest = chair.launch(chair.controller.step_position(3, 1))
    await asyncio.sleep(0)
    cancelled = latest if cancel_latest else older
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not home.done() and chair.writes == [CAPTURED_HOME]
    chair.release.set()
    await asyncio.wait_for(home, 1)
    if cancel_latest:
        with pytest.raises(CommandSuperseded):
            await older
        assert chair.writes == [CAPTURED_HOME]
    else:
        await asyncio.wait_for(latest, 1)
        assert any(packet[16] == 0x82 for packet in chair.writes[1:])


async def test_cancellation_at_home_completion_never_revives_old_control(held_home):
    chair = held_home
    home = await chair.start_home()
    old = chair.launch(chair.controller.step_position(0, 1))
    await asyncio.sleep(0)
    latest = chair.launch(chair.controller.step_position(3, 1))
    await asyncio.sleep(0)

    def cancel_at_completion():
        if chair.controller.last_home_request["outcome"] == "reported_neutral":
            latest.cancel()

    chair.controller._changed = cancel_at_completion
    chair.release.set()
    await asyncio.wait_for(home, 1)
    results = await asyncio.gather(old, latest, return_exceptions=True)
    assert isinstance(results[0], CommandSuperseded)
    assert isinstance(results[1], asyncio.CancelledError)
    assert chair.writes == [CAPTURED_HOME]


async def test_stop_interrupts_home_and_discards_pending_controls_immediately(held_home):
    chair = held_home
    home = await chair.start_home()
    pending = chair.launch(chair.controller.step_position(3, 1))
    await asyncio.sleep(0)
    await asyncio.wait_for(chair.controller.stop_movement(), 1)
    results = await asyncio.gather(home, pending, return_exceptions=True)
    assert all(isinstance(result, CommandSuperseded) for result in results)
    assert not chair.release.is_set()
    assert all(packet[13:17] == b"\x80" * 4 for packet in chair.writes[1:])
    # A later explicit action is allowed, without replaying Home. Its physical
    # reliability after interrupting native Home is not established by this fake.
    await chair.controller.step_position(3, 1)
    assert chair.writes.count(CAPTURED_HOME) == 1
    assert any(packet[16] == 0x82 for packet in chair.writes[1:])


async def test_repeated_home_joins_cycle_and_replaces_pending_action(held_home):
    chair = held_home
    home = await chair.start_home()
    pending = chair.launch(chair.controller.step_position(3, 1))
    await asyncio.sleep(0)
    repeated = chair.launch(chair.controller.return_home())
    await asyncio.sleep(0)
    assert chair.writes == [CAPTURED_HOME]
    chair.release.set()
    await asyncio.wait_for(asyncio.gather(home, repeated), 1)
    with pytest.raises(CommandSuperseded):
        await pending
    assert chair.writes == [CAPTURED_HOME]
