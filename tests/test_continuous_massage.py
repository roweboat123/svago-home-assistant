"""Continuous sessions renew only fresh active state and yield to user controls."""

import asyncio

import pytest
from conftest import status_with

from custom_components.svago import controller as controller_module
from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.protocol import MASSAGE_MODES, Status, checksum


@pytest.fixture
async def continuous_chair(chair):
    yield chair
    await chair.controller.cancel_active()


async def start_continuous(chair, mode="Gentle"):
    await chair.controller.set_massage_continuous()
    await chair.controller.set_massage(mode)
    return chair.controller._generation, chair.controller._massage_session


async def wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():  # noqa: ASYNC110 - bounded polling of fake transport outcomes
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("mode", MASSAGE_MODES[1:])
@pytest.mark.parametrize("heat", [False, True])
def test_renewal_payload_preserves_heat_settings_and_other_context(mode, heat):
    before = status_with(
        heat=heat,
        positions=(7, 8, 19, 20),
        **{"5": 0x11, "8": 0x2A, "9": 0x30, "20": 1, "21": 0, "22": 3, "23": 0x34},
    )
    packet = before.continuous_massage_command(mode)
    assert len(packet) == 27 and packet[-1] == 0x7E
    assert packet[:6] == bytes.fromhex("7E0201001000")
    assert packet[6:8] == bytes.fromhex("0A8C")
    assert packet[8:13] == before.raw[8:13]
    assert packet[13:17] == b"\xff" * 4
    assert packet[17] == MASSAGE_MODES.index(mode)
    assert packet[18] == 0 and packet[19] == 0x8F
    assert packet[20:25] == before.raw[20:25]
    assert packet[25] == checksum(packet)


@pytest.mark.parametrize("mode", [None, "Off", "Unknown"])
def test_renewal_never_infers_a_mode_from_off(mode):
    with pytest.raises(ValueError, match="active massage"):
        status_with(massage=0).continuous_massage_command(mode)


async def test_selecting_continuous_while_off_arms_without_writes(continuous_chair):
    chair = continuous_chair
    await chair.controller.set_massage_continuous()
    assert chair.controller.massage_continuous
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert chair.writes == []
    # A physical On is observed but must not launch a managed session.
    chair.controller.accept_status(status_with(massage=2))
    await asyncio.sleep(0)
    assert chair.controller._massage_renewal_task is None
    assert chair.writes == []


async def test_explicit_start_uses_standard_timer_and_creates_session(continuous_chair):
    chair = continuous_chair
    await start_continuous(chair, "Wave")
    assert len(chair.writes) == 2
    assert chair.writes[0][5] == 0x11
    assert chair.writes[1][17] == 4 and chair.writes[1][19] == 0x8F
    assert chair.controller.status.timer_minutes("massage") == 45
    assert chair.controller.massage_renewal_diagnostics["active"]


async def test_selecting_continuous_while_running_keeps_mode_and_does_not_activate(
    continuous_chair,
):
    chair = continuous_chair
    chair.controller.accept_status(status_with(massage=5))
    await chair.controller.set_massage_continuous()
    assert len(chair.writes) == 1
    assert chair.writes[0][5] == 0 and chair.writes[0][17] == 5
    assert chair.controller.status.timer_minutes("massage") == 45
    assert chair.controller.massage_renewal_diagnostics["active"]


async def test_renewal_rebuilds_latest_status_without_heat_or_activation(continuous_chair):
    chair = continuous_chair
    generation, session = await start_continuous(chair)
    before = status_with(
        massage=1,
        heat=False,
        positions=(2, 3, 4, 5),
        **{"6": 0x0A, "7": 0x8C, "8": 0x1C, "9": 0x20, "20": 3, "21": 0, "22": 1},
    )
    chair.controller.accept_status(before)
    chair.writes.clear()
    assert await chair.controller._renew_massage_once(generation, session)
    assert len(chair.writes) == 1
    packet = chair.writes[0]
    assert packet[5] == 0 and packet[19] == 0x8F
    assert packet[8:10] == before.raw[8:10]
    assert packet[20:25] == before.raw[20:25]
    assert chair.controller.status.heat is False
    assert chair.controller.status.positions == before.positions
    assert chair.controller.massage_renewal_diagnostics["renewal_count"] == 1


@pytest.mark.parametrize("lock", ["_settings_lock", "_transition"])
async def test_renewal_defers_behind_foreground_locks(continuous_chair, lock):
    chair = continuous_chair
    generation, session = await start_continuous(chair)
    count = len(chair.writes)
    async with getattr(chair.controller, lock):
        assert not await chair.controller._renew_massage_once(generation, session)
    assert len(chair.writes) == count
    assert chair.controller.massage_renewal_diagnostics["active"]


async def test_renewal_does_not_interrupt_lumbar_and_can_resume_after_release(continuous_chair):
    chair = continuous_chair
    generation, session = await start_continuous(chair)
    reached = asyncio.Event()
    release = asyncio.Event()
    original_sleep = chair.controller._sleep

    async def held_sleep(seconds):
        reached.set()
        await release.wait()
        await original_sleep(seconds)

    chair.controller._sleep = held_sleep
    movement = asyncio.create_task(chair.controller.adjust_lumbar(True))
    await reached.wait()
    count = len(chair.writes)
    assert not await chair.controller._renew_massage_once(generation, session)
    assert len(chair.writes) == count
    assert not movement.done()
    release.set()
    await movement
    assert await chair.controller._renew_massage_once(generation, session)
    assert chair.writes[-2][18] == 0  # Normal lumbar release still completed.


async def test_pending_native_travel_blocks_renewal(continuous_chair):
    chair = continuous_chair
    generation, session = await start_continuous(chair)
    chair.controller._needs_stop = True
    count = len(chair.writes)
    assert not await chair.controller._renew_massage_once(generation, session)
    assert len(chair.writes) == count


async def test_mode_change_invalidates_old_session_and_renews_new_mode(continuous_chair):
    chair = continuous_chair
    generation, old_session = await start_continuous(chair)
    old_task = chair.controller._massage_renewal_task
    await chair.controller.set_massage("Vigorous")
    session = chair.controller._massage_session
    assert session != old_session
    count = len(chair.writes)
    assert not await chair.controller._renew_massage_once(generation, old_session)
    assert len(chair.writes) == count
    assert await chair.controller._renew_massage_once(generation, session)
    assert chair.writes[-1][17] == 5
    await asyncio.sleep(0)
    assert old_task.cancelled()


@pytest.mark.parametrize("observed", [False, True])
async def test_off_cancels_session_keeps_choice_and_passive_on_does_not_restart(
    continuous_chair, observed
):
    chair = continuous_chair
    generation, session = await start_continuous(chair)
    if observed:
        raw = bytearray(chair.controller.status.raw)
        raw[17] = 0
        raw[25] = checksum(raw)
        chair.controller.accept_status(Status(bytes(raw)))
    else:
        await chair.controller.set_massage("Off")
    count = len(chair.writes)
    assert chair.controller.massage_continuous
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert not await chair.controller._renew_massage_once(generation, session)
    assert len(chair.writes) == count
    chair.controller.accept_status(status_with(massage=3, **{"6": 0x0A, "7": 0x8C}))
    await asyncio.sleep(0)
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert len(chair.writes) == count
    await chair.controller.set_massage("Relax")
    assert chair.controller.massage_renewal_diagnostics["active"]


async def test_finite_timer_disables_continuous_and_wins_over_queued_continuous(continuous_chair):
    chair = continuous_chair
    chair.feedback = False
    heat = asyncio.create_task(chair.controller.set_timer("heat", 180))
    await asyncio.sleep(0)
    continuous = asyncio.create_task(chair.controller.set_massage_continuous())
    await asyncio.sleep(0)
    finite = asyncio.create_task(chair.controller.set_timer("massage", 30))
    await asyncio.sleep(0)
    chair.feedback = True
    chair.controller.accept_status(status_with(**{"8": 0x2A, "9": 0x30}))
    await asyncio.gather(heat, continuous, finite)
    assert not chair.controller.massage_continuous
    assert chair.controller.status.timer_minutes("massage") == 30
    assert len(chair.writes) == 2


async def test_finite_timer_queued_during_initial_confirmation_is_not_discarded(continuous_chair):
    chair = continuous_chair
    chair.controller.accept_status(status_with(massage=2))
    chair.feedback = False
    continuous = asyncio.create_task(chair.controller.set_massage_continuous())
    await asyncio.sleep(0)
    finite = asyncio.create_task(chair.controller.set_timer("massage", 45))
    await asyncio.sleep(0)
    chair.feedback = True
    chair.controller.accept_status(status_with(massage=2, **{"6": 0x0A, "7": 0x8C}))
    await asyncio.gather(continuous, finite)
    assert not chair.controller.massage_continuous
    assert chair.controller.status.timer_minutes("massage") == 45
    assert chair.controller._massage_renewal_task is None


@pytest.mark.parametrize("change", [{"6": 7, "7": 8}, {"10": 0x11}])
async def test_external_timer_or_preset_change_clears_choice(continuous_chair, change):
    chair = continuous_chair
    generation, session = await start_continuous(chair)
    raw = bytearray(chair.controller.status.raw)
    for offset, value in change.items():
        raw[int(offset)] = value
    raw[25] = checksum(raw)
    chair.controller.accept_status(Status(bytes(raw)))
    assert not chair.controller.massage_continuous
    assert not await chair.controller._renew_massage_once(generation, session)


@pytest.mark.parametrize("action", ["preset", "memory", "finite", "disconnect", "shutdown"])
async def test_overrides_and_lifecycle_clear_session(continuous_chair, action):
    chair = continuous_chair
    generation, session = await start_continuous(chair)
    task = chair.controller._massage_renewal_task
    if action == "preset":
        await chair.controller.select_preset("zgr")
    elif action == "memory":
        chair.controller.reserve_memory_recall()
    elif action == "finite":
        await chair.controller.set_timer("massage", 30)
    elif action == "disconnect":
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with(massage=1))
    else:
        await chair.controller.cancel_active()
    assert chair.controller.massage_continuous is (action in ("disconnect", "shutdown"))
    count = len(chair.writes)
    assert not await chair.controller._renew_massage_once(generation, session)
    assert len(chair.writes) == count
    await asyncio.sleep(0)
    assert task.done()


@pytest.mark.parametrize("failure", ["write", "feedback", "stale"])
async def test_worker_stops_after_failure_without_retry_or_passive_restart(
    continuous_chair, monkeypatch, failure
):
    chair = continuous_chair
    monkeypatch.setattr(controller_module, "MASSAGE_RENEW_SECONDS", 0.005)
    await start_continuous(chair)
    if failure == "write":
        chair.fail_at = len(chair.writes) + 1
    elif failure == "feedback":
        chair.feedback = False
    else:
        chair.time += 4
    count = len(chair.writes)
    await wait_until(lambda: not chair.controller.massage_renewal_diagnostics["active"])
    assert len(chair.writes) == count + (failure != "stale")
    assert chair.controller.massage_renewal_diagnostics["outcome"] == "renewal_failed"
    assert chair.controller.massage_continuous
    chair.feedback = True
    chair.controller.accept_status(status_with(massage=1, **{"6": 0x0A, "7": 0x8C}))
    await asyncio.sleep(0.02)
    assert len(chair.writes) == count + (failure != "stale")


async def test_background_worker_repeats_and_shutdown_awaits_all_tasks(
    continuous_chair, monkeypatch
):
    chair = continuous_chair
    monkeypatch.setattr(controller_module, "MASSAGE_RENEW_SECONDS", 0.005)
    await start_continuous(chair)
    await wait_until(lambda: chair.controller.massage_renewal_diagnostics["renewal_count"] >= 2)
    assert all(packet[19] == 0x8F and packet[5] == 0 for packet in chair.writes[1:])
    tasks = set(chair.controller._massage_tasks)
    await chair.controller.cancel_active()
    assert all(task.done() for task in tasks)
    count = len(chair.writes)
    await asyncio.sleep(0.02)
    assert len(chair.writes) == count


async def test_off_interrupts_pending_renewal_confirmation(continuous_chair, monkeypatch):
    chair = continuous_chair
    monkeypatch.setattr(controller_module, "MASSAGE_RENEW_SECONDS", 0.005)
    await start_continuous(chair)
    chair.feedback = False
    count = len(chair.writes)
    await wait_until(lambda: len(chair.writes) > count)
    chair.feedback = True
    await chair.controller.set_massage("Off")
    assert chair.controller.status.massage == "Off"
    count = len(chair.writes)
    await asyncio.sleep(0.02)
    assert len(chair.writes) == count
    assert not chair.controller.massage_renewal_diagnostics["active"]


async def test_off_cancels_pending_renewal_write_before_any_later_on(continuous_chair, monkeypatch):
    chair = continuous_chair
    monkeypatch.setattr(controller_module, "MASSAGE_RENEW_SECONDS", 0.005)
    await start_continuous(chair)
    entered = asyncio.Event()
    blocked = asyncio.Event()
    original_write = chair.controller._write

    async def delayed_write(payload):
        if payload[17] != 0:
            entered.set()
            await blocked.wait()
        await original_write(payload)

    chair.controller._write = delayed_write
    await asyncio.wait_for(entered.wait(), timeout=1)
    await chair.controller.set_massage("Off")
    assert chair.writes[-1][17] == 0
    count = len(chair.writes)
    blocked.set()
    await asyncio.sleep(0.02)
    assert len(chair.writes) == count
    assert chair.controller.status.massage == "Off"


async def test_new_mode_replaces_pending_renewal_confirmation(continuous_chair, monkeypatch):
    chair = continuous_chair
    monkeypatch.setattr(controller_module, "MASSAGE_RENEW_SECONDS", 0.005)
    await start_continuous(chair)
    old_task = chair.controller._massage_renewal_task
    chair.feedback = False
    count = len(chair.writes)
    await wait_until(lambda: len(chair.writes) > count)
    chair.feedback = True
    await chair.controller.set_massage("Stretch")
    new_task = chair.controller._massage_renewal_task
    assert new_task is not old_task
    await wait_until(lambda: chair.controller.massage_renewal_diagnostics["renewal_count"] >= 1)
    assert old_task.cancelled()
    assert not new_task.done()
    assert chair.writes[-1][17] == 3
    assert chair.controller.status.massage == "Stretch"


async def test_shutdown_invalidates_queued_settings_before_background_cleanup_wait(
    continuous_chair,
):
    chair = continuous_chair
    await start_continuous(chair)
    waiting = asyncio.Event()
    cleanup = asyncio.Event()

    async def slow_cleanup():
        try:
            await waiting.wait()
        except asyncio.CancelledError:
            await cleanup.wait()
            raise

    background = asyncio.create_task(slow_cleanup())
    chair.controller._massage_tasks.add(background)
    await asyncio.sleep(0)
    await chair.controller._settings_lock.acquire()
    count = len(chair.writes)
    queued = asyncio.create_task(chair.controller.set_timer("heat", 120))
    await asyncio.sleep(0)
    shutdown = asyncio.create_task(chair.controller.cancel_active())
    await asyncio.sleep(0)
    chair.controller._settings_lock.release()
    with pytest.raises(CommandSuperseded):
        await queued
    assert len(chair.writes) == count
    cleanup.set()
    await shutdown
    assert background.done()


async def test_failed_initial_confirmation_never_creates_background_task(continuous_chair):
    chair = continuous_chair
    chair.controller.accept_status(status_with(massage=1))
    chair.feedback = False
    with pytest.raises(ControlError):
        await chair.controller.set_massage_continuous()
    assert len(chair.writes) == 1
    assert chair.controller._massage_renewal_task is None
    assert not chair.controller.massage_renewal_diagnostics["active"]


async def test_diagnostics_returns_a_snapshot(continuous_chair):
    chair = continuous_chair
    await start_continuous(chair)
    snapshot = chair.controller.massage_renewal_diagnostics
    snapshot["renewal_count"] = 999
    assert chair.controller.massage_renewal_diagnostics["renewal_count"] == 0
