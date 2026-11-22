"""Continuous heat uses bounded leases and does not renew a finite massage."""

import asyncio

import pytest
from conftest import status_with

from custom_components.svago import controller as controller_module
from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.protocol import MASSAGE_MODES, Status, checksum


@pytest.fixture
async def heat_chair(chair):
    yield chair
    await chair.controller.cancel_active()


async def start_heat(chair):
    await chair.controller.set_heat_continuous()
    if not chair.controller.status.heat:
        await chair.controller.set_heat(True)
    return chair.controller._generation, chair.controller._heat_session


async def wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():  # noqa: ASYNC110 - bounded fake transport polling
            await asyncio.sleep(0.001)


def report(chair, **changes):
    raw = bytearray(chair.controller.status.raw)
    for offset, value in changes.items():
        raw[int(offset)] = value
    raw[25] = checksum(raw)
    chair.controller.accept_status(Status(bytes(raw)))


@pytest.mark.parametrize("mode", range(len(MASSAGE_MODES)))
def test_heat_packet_copies_actual_massage_and_zone_settings(mode):
    before = status_with(
        heat=False,
        massage=mode,
        positions=(7, 8, 19, 20),
        **{"5": 0x11, "6": 7, "7": 8, "18": 2, "20": 1, "21": 0, "22": 3, "23": 0x34},
    )
    packet = before.continuous_heat_command()
    assert len(packet) == 27 and packet[-1] == 0x7E
    assert packet[:6] == bytes.fromhex("7E0201001000")
    assert packet[6:8] == before.raw[6:8]
    assert packet[8:10] == bytes.fromhex("2A30")
    assert packet[10:13] == before.raw[10:13]
    assert packet[13:17] == b"\xff" * 4
    assert packet[17] == mode and packet[18] == 0 and packet[19] == 1
    assert packet[20:25] == before.raw[20:25]
    assert packet[25] == checksum(packet)


async def test_off_choice_arms_without_writes_or_passive_restart(heat_chair):
    chair = heat_chair
    report(chair, **{"19": 2})
    await chair.controller.set_heat_continuous()
    assert chair.controller.heat_continuous
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert chair.writes == []
    report(chair, **{"19": 1})
    await asyncio.sleep(0)
    assert chair.controller._heat_renewal_task is None
    assert chair.writes == []


async def test_explicit_on_activates_then_starts_standard_lease(heat_chair):
    chair = heat_chair
    report(chair, **{"19": 2})
    await start_heat(chair)
    assert len(chair.writes) == 2
    assert chair.writes[0][5] == 0x11
    assert chair.writes[1][5] == 0 and chair.writes[1][19] == 1
    assert chair.controller.status.timer_minutes("heat") == 180
    assert chair.controller.heat_renewal_diagnostics["active"]


async def test_current_heat_selection_does_not_activate_or_change_zones(heat_chair):
    chair = heat_chair
    report(chair, **{"20": 0, "21": 3, "22": 1})
    await start_heat(chair)
    assert len(chair.writes) == 1
    assert chair.writes[0][5] == 0
    assert chair.writes[0][20:23] == bytes((0, 3, 1))


@pytest.mark.parametrize("seconds", [900, 1800, 2700])
@pytest.mark.parametrize("selected", [False, True])
async def test_finite_or_unmanaged_massage_defers_initial_and_automatic_write(
    heat_chair, seconds, selected
):
    chair = heat_chair
    report(chair, **{"17": 2, "6": seconds >> 8, "7": seconds & 255})
    if selected:
        # An armed choice alone does not make a passive/external mode managed.
        chair.controller._massage_continuous = True
    generation, session = await start_heat(chair)
    assert chair.writes == []
    assert chair.controller.heat_renewal_diagnostics["outcome"] == "deferred_massage"
    report(chair)  # Ordinary reports of the existing 60-minute timer are valid.
    assert chair.controller.heat_continuous
    assert not await chair.controller._renew_heat_once(generation, session)
    assert chair.writes == []
    assert chair.controller.status.timer_minutes("heat") == 60
    report(chair, **{"17": 0})
    assert await chair.controller._renew_heat_once(generation, session)
    assert chair.controller.status.timer_minutes("heat") == 180
    assert chair.writes[-1][17] == 0


async def test_explicit_heat_on_during_finite_massage_keeps_existing_duration(heat_chair):
    chair = heat_chair
    report(chair, **{"19": 2, "17": 3})
    await chair.controller.set_heat_continuous()
    assert chair.writes == []
    await chair.controller.set_heat(True)
    assert len(chair.writes) == 2  # Existing activation and explicit On only.
    assert chair.writes[-1][8:10] == bytes.fromhex("0E10")
    assert chair.writes[-1][17] == 3
    assert chair.controller.heat_renewal_diagnostics["outcome"] == "deferred_massage"


async def test_deferred_session_resumes_after_massage_off_without_waiting_for_next_lease(
    heat_chair, monkeypatch
):
    chair = heat_chair
    monkeypatch.setattr(controller_module, "HEAT_DEFER_SECONDS", 0.005)
    report(chair, **{"17": 2})
    await start_heat(chair)
    assert chair.writes == []
    report(chair, **{"17": 0})
    await wait_until(lambda: chair.controller.heat_renewal_diagnostics["renewal_count"] == 1)
    assert len(chair.writes) == 1


@pytest.mark.parametrize("was_deferred", [False, True])
@pytest.mark.parametrize("observed", [False, True])
async def test_off_ends_only_heat_session_and_passive_on_never_restarts(
    heat_chair, was_deferred, observed
):
    chair = heat_chair
    if was_deferred:
        report(chair, **{"17": 2})
    generation, session = await start_heat(chair)
    if observed:
        report(chair, **{"19": 2})
    else:
        await chair.controller.set_heat(False)
    count = len(chair.writes)
    assert chair.controller.heat_continuous
    assert not chair.controller.heat_renewal_diagnostics["active"]
    report(chair, **{"19": 1, "17": 0})
    assert not await chair.controller._renew_heat_once(generation, session)
    await asyncio.sleep(0)
    assert len(chair.writes) == count
    assert not chair.controller.heat_renewal_diagnostics["active"]
    await chair.controller.set_heat(True)
    assert chair.controller.heat_renewal_diagnostics["active"]


async def test_dual_continuous_renews_current_modes_and_independent_off(heat_chair):
    chair = heat_chair
    await chair.controller.set_massage_continuous()
    await chair.controller.set_massage("Wave")
    generation, session = await start_heat(chair)
    massage_session = chair.controller._massage_session
    assert await chair.controller._renew_heat_once(generation, session)
    assert chair.writes[-1][17] == 4
    assert chair.writes[-1][6:8] == bytes.fromhex("0A8C")
    assert await chair.controller._renew_massage_once(generation, massage_session)
    assert chair.writes[-1][19] == 0x8F
    await chair.controller.set_heat(False)
    assert chair.controller.massage_renewal_diagnostics["active"]
    assert await chair.controller._renew_massage_once(generation, massage_session)
    assert chair.controller.status.heat is False
    await chair.controller.set_heat(True)
    new_heat_session = chair.controller._heat_session
    await chair.controller.set_massage("Off")
    assert chair.controller.heat_renewal_diagnostics["active"]
    assert await chair.controller._renew_heat_once(generation, new_heat_session)
    assert chair.controller.status.massage == "Off"


async def test_new_finite_massage_blocks_heat_without_cancelling_heat_session(heat_chair):
    chair = heat_chair
    await chair.controller.set_massage_continuous()
    await chair.controller.set_massage("Gentle")
    generation, session = await start_heat(chair)
    await chair.controller.set_timer("massage", 30)
    count = len(chair.writes)
    assert chair.controller.heat_renewal_diagnostics["active"]
    assert not await chair.controller._renew_heat_once(generation, session)
    assert len(chair.writes) == count
    assert not chair.controller.massage_continuous


async def test_deferred_heat_can_resume_when_massage_becomes_managed_continuous(heat_chair):
    chair = heat_chair
    report(chair, **{"17": 2})
    generation, session = await start_heat(chair)
    await chair.controller.set_massage_continuous()
    assert await chair.controller._renew_heat_once(generation, session)
    assert chair.controller.massage_renewal_diagnostics["active"]


async def test_finite_heat_timer_cancels_only_heat(heat_chair):
    chair = heat_chair
    await chair.controller.set_massage_continuous()
    await chair.controller.set_massage("Relax")
    await start_heat(chair)
    await chair.controller.set_timer("heat", 120)
    assert not chair.controller.heat_continuous
    assert chair.controller.massage_renewal_diagnostics["active"]
    assert chair.controller.status.timer_minutes("heat") == 120


@pytest.mark.parametrize("lock", ["_settings_lock", "_transition"])
async def test_renewal_defers_to_foreground_locks(heat_chair, lock):
    chair = heat_chair
    generation, session = await start_heat(chair)
    count = len(chair.writes)
    async with getattr(chair.controller, lock):
        assert not await chair.controller._renew_heat_once(generation, session)
    assert len(chair.writes) == count


@pytest.mark.parametrize("blocker", ["native_travel", "lumbar"])
async def test_renewal_defers_without_releasing_active_adjustments(heat_chair, blocker):
    chair = heat_chair
    generation, session = await start_heat(chair)
    if blocker == "native_travel":
        chair.controller._needs_stop = True
    else:
        report(chair, **{"18": 2})
    count = len(chair.writes)
    assert not await chair.controller._renew_heat_once(generation, session)
    assert len(chair.writes) == count


@pytest.mark.parametrize("change", [{"8": 0x1C, "9": 0x20}, {"10": 0x11}])
async def test_external_timer_or_preset_change_clears_continuous(heat_chair, change):
    chair = heat_chair
    generation, session = await start_heat(chair)
    report(chair, **change)
    assert not chair.controller.heat_continuous
    assert not await chair.controller._renew_heat_once(generation, session)


@pytest.mark.parametrize("action", ["preset", "memory", "disconnect", "shutdown"])
async def test_lifecycle_clears_both_sessions(heat_chair, action):
    chair = heat_chair
    await chair.controller.set_massage_continuous()
    await chair.controller.set_massage("Gentle")
    generation, session = await start_heat(chair)
    tasks = chair.controller._heat_tasks | chair.controller._massage_tasks
    if action == "preset":
        await chair.controller.select_preset("zgr")
    elif action == "memory":
        chair.controller.reserve_memory_recall()
    elif action == "disconnect":
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with())
    else:
        await chair.controller.cancel_active()
    assert chair.controller.heat_continuous is (action in ("disconnect", "shutdown"))
    assert chair.controller.massage_continuous is (action in ("disconnect", "shutdown"))
    count = len(chair.writes)
    assert not await chair.controller._renew_heat_once(generation, session)
    await asyncio.sleep(0)
    assert all(task.done() for task in tasks)
    assert len(chair.writes) == count


@pytest.mark.parametrize("failure", ["write", "feedback", "stale"])
async def test_worker_failure_stops_without_retry_or_passive_restart(
    heat_chair, monkeypatch, failure
):
    chair = heat_chair
    monkeypatch.setattr(controller_module, "HEAT_RENEW_SECONDS", 0.005)
    await start_heat(chair)
    if failure == "write":
        chair.fail_at = len(chair.writes) + 1
    elif failure == "feedback":
        chair.feedback = False
    else:
        chair.time += 4
    count = len(chair.writes)
    await wait_until(lambda: not chair.controller.heat_renewal_diagnostics["active"])
    assert len(chair.writes) == count + (failure != "stale")
    assert chair.controller.heat_renewal_diagnostics["outcome"] == "renewal_failed"
    chair.feedback = True
    report(chair)
    await asyncio.sleep(0.02)
    assert len(chair.writes) == count + (failure != "stale")


async def test_queued_finite_selection_wins_over_older_continuous(heat_chair):
    chair = heat_chair
    chair.feedback = False
    first = asyncio.create_task(chair.controller.set_timer("massage", 30))
    await asyncio.sleep(0)
    continuous = asyncio.create_task(chair.controller.set_heat_continuous())
    await asyncio.sleep(0)
    finite = asyncio.create_task(chair.controller.set_timer("heat", 120))
    await asyncio.sleep(0)
    chair.feedback = True
    report(chair, **{"6": 7, "7": 8})
    await asyncio.gather(first, continuous, finite)
    assert not chair.controller.heat_continuous
    assert chair.controller.status.timer_minutes("heat") == 120
    assert len(chair.writes) == 2


async def test_finite_queued_during_initial_confirmation_is_not_discarded(heat_chair):
    chair = heat_chair
    chair.feedback = False
    continuous = asyncio.create_task(chair.controller.set_heat_continuous())
    await asyncio.sleep(0)
    finite = asyncio.create_task(chair.controller.set_timer("heat", 120))
    await asyncio.sleep(0)
    chair.feedback = True
    report(chair, **{"8": 0x2A, "9": 0x30})
    await asyncio.gather(continuous, finite)
    assert not chair.controller.heat_continuous
    assert chair.controller.status.timer_minutes("heat") == 120
    assert chair.controller._heat_renewal_task is None


async def test_heat_off_cancels_pending_renewal_write(heat_chair, monkeypatch):
    chair = heat_chair
    monkeypatch.setattr(controller_module, "HEAT_RENEW_SECONDS", 0.005)
    await start_heat(chair)
    entered, blocked = asyncio.Event(), asyncio.Event()
    original_write = chair.controller._write

    async def delayed_write(payload):
        if payload[19] == 1:
            entered.set()
            await blocked.wait()
        await original_write(payload)

    chair.controller._write = delayed_write
    await asyncio.wait_for(entered.wait(), timeout=1)
    await chair.controller.set_heat(False)
    assert chair.writes[-1][19] == 2
    count = len(chair.writes)
    blocked.set()
    await asyncio.sleep(0.02)
    assert len(chair.writes) == count
    assert chair.controller.status.heat is False


async def test_heat_off_cancels_pending_confirmation_without_touching_massage(
    heat_chair, monkeypatch
):
    chair = heat_chair
    monkeypatch.setattr(controller_module, "HEAT_RENEW_SECONDS", 0.005)
    await chair.controller.set_massage_continuous()
    await chair.controller.set_massage("Gentle")
    await start_heat(chair)
    massage_task = chair.controller._massage_renewal_task
    chair.feedback = False
    count = len(chair.writes)
    await wait_until(lambda: len(chair.writes) > count)
    chair.feedback = True
    await chair.controller.set_heat(False)
    assert not massage_task.done()
    assert chair.controller.massage_renewal_diagnostics["active"]
    assert not chair.controller.heat_renewal_diagnostics["active"]


async def test_workers_defer_to_each_others_write_and_shutdown_awaits_both(heat_chair, monkeypatch):
    chair = heat_chair
    await chair.controller.set_massage_continuous()
    await chair.controller.set_massage("Gentle")
    generation, session = await start_heat(chair)
    entered, blocked = asyncio.Event(), asyncio.Event()
    original_write = chair.controller._write

    async def delayed_write(payload):
        entered.set()
        await blocked.wait()
        await original_write(payload)

    chair.controller._write = delayed_write
    renewal = asyncio.create_task(chair.controller._renew_heat_once(generation, session))
    await entered.wait()
    assert not await chair.controller._renew_massage_once(
        generation, chair.controller._massage_session
    )
    blocked.set()
    assert await renewal
    chair.controller._write = original_write
    tasks = chair.controller._heat_tasks | chair.controller._massage_tasks
    await chair.controller.cancel_active()
    assert all(task.done() for task in tasks)
    assert chair.controller.heat_continuous
    assert chair.controller.massage_continuous


async def test_repeating_worker_rebuilds_fresh_context_and_leaves_no_work_after_shutdown(
    heat_chair, monkeypatch
):
    chair = heat_chair
    monkeypatch.setattr(controller_module, "HEAT_RENEW_SECONDS", 0.005)
    await start_heat(chair)
    report(chair, **{"20": 3, "21": 0, "22": 1})
    await wait_until(lambda: chair.controller.heat_renewal_diagnostics["renewal_count"] >= 2)
    assert chair.writes[-1][20:23] == bytes((3, 0, 1))
    assert all(payload[5] == 0 for payload in chair.writes)
    await chair.controller.cancel_active()
    count = len(chair.writes)
    await asyncio.sleep(0.02)
    assert len(chair.writes) == count


async def test_shutdown_invalidates_foreground_before_waiting_for_heat_cleanup(heat_chair):
    chair = heat_chair
    await start_heat(chair)
    waiting, cleanup = asyncio.Event(), asyncio.Event()

    async def slow_cleanup():
        try:
            await waiting.wait()
        except asyncio.CancelledError:
            await cleanup.wait()
            raise

    background = asyncio.create_task(slow_cleanup())
    chair.controller._heat_tasks.add(background)
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


async def test_initial_confirmation_failure_never_creates_worker(heat_chair):
    chair = heat_chair
    chair.feedback = False
    with pytest.raises(ControlError):
        await chair.controller.set_heat_continuous()
    assert len(chair.writes) == 1
    assert chair.controller._heat_renewal_task is None
    assert not chair.controller.heat_renewal_diagnostics["active"]


async def test_diagnostics_are_independent_snapshots(heat_chair):
    chair = heat_chair
    await start_heat(chair)
    snapshot = chair.controller.heat_renewal_diagnostics
    snapshot["renewal_count"] = 999
    assert chair.controller.heat_renewal_diagnostics["renewal_count"] == 0
    assert chair.controller.massage_renewal_diagnostics["renewal_count"] == 0


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_observed_off_during_initial_confirmation_prevents_passive_restart(heat_chair, kind):
    chair = heat_chair
    if kind == "massage":
        report(chair, **{"17": 1})
    chair.feedback = False
    start = asyncio.create_task(getattr(chair.controller, f"set_{kind}_continuous")())
    await asyncio.sleep(0)
    if kind == "heat":
        report(chair, **{"19": 2})
        report(chair, **{"19": 1, "8": 0x2A, "9": 0x30})
    else:
        report(chair, **{"17": 0})
        report(chair, **{"17": 1, "6": 0x0A, "7": 0x8C})
    await start
    assert getattr(chair.controller, f"{kind}_continuous")
    assert not getattr(chair.controller, f"{kind}_renewal_diagnostics")["active"]
    assert getattr(chair.controller, f"_{kind}_renewal_task") is None
    assert len(chair.writes) == 1


@pytest.mark.parametrize("kind", ["heat", "massage"])
@pytest.mark.parametrize("stop_after_first_on", [False, True])
async def test_explicit_start_allows_old_off_report_until_first_on_acknowledgment(
    heat_chair, kind, stop_after_first_on
):
    chair = heat_chair
    report(chair, **{"19": 2, "17": 0})
    await getattr(chair.controller, f"set_{kind}_continuous")()
    chair.feedback = False
    start = asyncio.create_task(
        chair.controller.set_heat(True)
        if kind == "heat"
        else chair.controller.set_massage("Gentle")
    )
    await asyncio.sleep(0)
    report(chair, **{"5": 0x11})  # Activation completes with the function still Off.
    await wait_until(lambda: len(chair.writes) == 2)
    report(chair)  # Periodic old-state feedback before the first On confirmation.
    if kind == "heat":
        report(chair, **{"19": 1, "8": 0x2A, "9": 0x30})
        if stop_after_first_on:
            report(chair, **{"19": 2})
            report(chair, **{"19": 1})
    else:
        report(chair, **{"17": 1, "6": 0x0A, "7": 0x8C})
        if stop_after_first_on:
            report(chair, **{"17": 0})
            report(chair, **{"17": 1})
    await start
    assert getattr(chair.controller, f"{kind}_renewal_diagnostics")["active"] is (
        not stop_after_first_on
    )
    assert len(chair.writes) == 2
