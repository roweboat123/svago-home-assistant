"""Command-based power estimates remain distinct from actual chair feedback."""

import asyncio

import pytest
from conftest import status_with
from test_home import CAPTURED_HOME, HomeChair

from custom_components.svago.controller import CommandSuperseded, ControlError


def power(chair):
    return chair.controller.wired_controller_power_estimate


@pytest.fixture
async def power_chair():
    chair = HomeChair()
    yield chair
    await chair.controller.cancel_active()


async def finish_home(chair):
    chair.on_sleep = chair.report
    await chair.controller.return_home()
    chair.on_sleep = lambda: chair.controller.accept_status(chair.controller.status)


@pytest.mark.parametrize("field", [0x00, 0x01, 0x11])
def test_unsolicited_status_never_establishes_controller_power(chair, field):
    chair.controller.accept_status(
        status_with(positions=(0, 0, 0, 0), heat=False, massage=0, **{"5": field})
    )

    assert power(chair)["state"] is None
    assert power(chair)["estimated"] is True
    assert power(chair)["physical_controls_observed"] is False
    assert not chair.writes


async def test_successful_wake_estimates_on_without_suppressing_later_wakes(chair):
    await chair.controller.set_massage("Gentle")
    assert power(chair)["state"] == "On"
    assert power(chair)["basis"] == "HA wake command acknowledged"
    first = len(chair.writes)

    await chair.controller.set_massage("Relax")

    assert power(chair)["state"] == "On"
    assert chair.writes[0][5] == chair.writes[first][5] == 0x11
    assert len(chair.writes) == 4


async def test_failed_dependent_action_does_not_erase_completed_wake(chair):
    chair.fail_at = 2
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.set_massage("Gentle")
    assert power(chair)["state"] == "On"


async def test_home_is_unknown_during_travel_and_off_only_after_completion(power_chair):
    chair = power_chair
    await chair.controller.turn_on()
    assert power(chair)["state"] == "On"
    during = []

    def report():
        during.append(power(chair)["state"])
        chair.report()

    chair.on_sleep = report
    await chair.controller.return_home()

    assert during and all(state is None for state in during)
    assert power(chair)["state"] == "Off"
    assert power(chair)["basis"] == "HA Return to neutral completed"
    assert chair.writes.count(CAPTURED_HOME) == 1


async def test_post_home_wake_stays_unknown_until_settling_finishes(power_chair):
    chair = power_chair
    await finish_home(chair)
    during = []

    def report():
        during.append(power(chair)["state"])
        chair.controller.accept_status(chair.controller.status)

    chair.on_sleep = report
    started = chair.time
    await chair.controller.turn_on()

    assert 1 <= chair.time - started <= 1.2
    assert during and all(state is None for state in during)
    assert power(chair)["state"] == "On"


@pytest.mark.parametrize("failure", ["write", "no_ack", "cancel"])
async def test_incomplete_wake_discards_previous_estimate(chair, failure):
    await chair.controller.turn_on()
    if failure == "write":
        chair.fail_at = len(chair.writes) + 1
    else:
        chair.activation_feedback = False

    task = asyncio.create_task(chair.controller.turn_on())
    await asyncio.sleep(0)
    assert power(chair)["state"] is None
    if failure == "cancel":
        task.cancel()
        expected = asyncio.CancelledError
    else:
        expected = ControlError
    with pytest.raises(expected):
        await task

    # A later echo is still not a newly completed HA activation.
    chair.controller.accept_status(status_with(**{"5": 0x11}))
    assert power(chair)["state"] is None


@pytest.mark.parametrize("failure", ["write", "no_reports", "cancel", "stop"])
async def test_incomplete_home_never_estimates_off(power_chair, failure):
    chair = power_chair
    await chair.controller.turn_on()
    if failure == "write":
        chair.fail_at = len(chair.writes) + 1

    task = asyncio.create_task(chair.controller.return_home())
    await asyncio.sleep(0)
    assert power(chair)["state"] is None
    if failure == "cancel":
        task.cancel()
        expected = asyncio.CancelledError
    elif failure == "stop":
        await chair.controller.stop_movement()
        expected = CommandSuperseded
    else:
        expected = ControlError
    with pytest.raises(expected):
        await task

    chair.report()
    assert power(chair)["state"] is None


async def test_failed_post_home_settling_does_not_estimate_on(power_chair):
    chair = power_chair
    await finish_home(chair)
    chair.on_sleep = None
    with pytest.raises(ControlError):
        await chair.controller.turn_on()

    chair.report(**{"5": 0x11})
    assert power(chair)["state"] is None


@pytest.mark.parametrize("completed", ["wake", "home"])
async def test_reconnect_does_not_restore_an_old_estimate(power_chair, completed):
    chair = power_chair
    if completed == "wake":
        await chair.controller.turn_on()
    else:
        await finish_home(chair)
    assert power(chair)["state"] in {"On", "Off"}

    chair.controller.set_connected(False)
    assert power(chair)["state"] is None
    chair.controller.set_connected(True)
    chair.report(**{"5": 0x11})
    assert power(chair)["state"] is None


async def test_old_wake_cannot_establish_power_after_reconnect(chair):
    chair.activation_feedback = False
    task = asyncio.create_task(chair.controller.turn_on())
    await asyncio.sleep(0)
    chair.controller.set_connected(False)
    chair.controller.set_connected(True)
    chair.controller.accept_status(status_with(**{"5": 0x11}))

    with pytest.raises(ControlError, match="Fresh"):
        await task
    assert power(chair)["state"] is None


@pytest.mark.parametrize("read_while_stale", [False, True])
async def test_stale_feedback_does_not_resurrect_estimate_when_reports_resume(
    chair, read_while_stale
):
    await chair.controller.turn_on()
    chair.time += 4
    if read_while_stale:
        assert power(chair)["state"] is None
    chair.controller.accept_status(status_with(**{"5": 0x11}))

    assert power(chair)["state"] is None
    await chair.controller.turn_on()
    assert power(chair)["state"] == "On"


async def test_unusable_feedback_discards_estimate_until_new_ha_action(chair):
    await chair.controller.turn_on()
    chair.controller.accept_status(status_with(**{"19": 0}))
    assert power(chair)["state"] is None
    chair.controller.accept_status(status_with())
    assert power(chair)["state"] is None


@pytest.mark.parametrize("action", ["wake", "home"])
async def test_preflight_rejection_does_not_change_existing_estimate(chair, action):
    await chair.controller.turn_on()
    before = power(chair)
    chair.controller.accept_status(status_with(positions=(0x82, 0, 0, 0)))
    count = len(chair.writes)

    with pytest.raises(ControlError):
        await (chair.controller.turn_on() if action == "wake" else chair.controller.return_home())

    assert power(chair) == before
    assert len(chair.writes) == count


async def test_other_off_commands_and_status_changes_do_not_estimate_controller_off(chair):
    await chair.controller.turn_on()
    await chair.controller.set_heat(False)
    await chair.controller.set_massage("Off")
    await chair.controller.stop_movement()
    chair.controller.accept_status(status_with(positions=(0, 0, 0, 0), heat=False, massage=0))

    assert power(chair)["state"] == "On"


async def test_unchanged_feedback_does_not_change_estimate_or_emit_extra_updates(chair):
    await chair.controller.turn_on()
    before = power(chair)
    updates = []
    chair.controller._changed = lambda: updates.append(power(chair))
    for _ in range(20):
        chair.controller.accept_status(chair.controller.status)

    assert power(chair) == before
    assert updates == []
