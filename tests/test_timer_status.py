"""Visible Continuous state distinguishes a choice from active renewal."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import status_with
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory

from custom_components.svago import controller as controller_module
from custom_components.svago.connection import Connection
from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.memory_protocol import MemoryProfile
from custom_components.svago.sensor import ContinuousStatusSensor, async_setup_entry


@pytest.fixture
async def timer_connection(chair):
    connection = Connection(SimpleNamespace(), "00:11:22:33:44:55")
    connection.controller = chair.controller
    connection._client = SimpleNamespace(is_connected=True)
    connection._write_char = SimpleNamespace(max_write_without_response_size=27)
    connection._notifications_subscribed = True
    connection._setup_complete = True
    yield connection
    await chair.controller.cancel_active()


def select_continuous(chair, kind):
    return getattr(chair.controller, f"set_{kind}_continuous")()


async def start(chair, kind):
    chair.controller.accept_status(status_with(heat=False, massage=0))
    await select_continuous(chair, kind)
    if kind == "heat":
        await chair.controller.set_heat(True)
    else:
        await chair.controller.set_massage("Gentle")


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_status_is_default_diagnostic_with_stable_identity(timer_connection, kind):
    add = Mock()
    await async_setup_entry(None, SimpleNamespace(runtime_data=timer_connection), add)
    entity = next(
        item for item in add.call_args.args[0] if item.name == f"{kind.title()} continuous status"
    )
    assert entity.unique_id == f"00:11:22:33:44:55_{kind}_continuous_status"
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    assert entity.entity_registry_enabled_default
    assert entity.device_class is SensorDeviceClass.ENUM
    assert entity.native_value == "Not selected"
    assert entity.native_value in entity.options


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_retained_off_choice_waits_for_start_without_any_writes(
    timer_connection, chair, kind
):
    chair.controller.accept_status(status_with(heat=False, massage=0))
    await select_continuous(chair, kind)
    entity = ContinuousStatusSensor(timer_connection, kind)
    assert entity.native_value == "Waiting for start"
    assert entity.extra_state_attributes["continuous_selected"] is True
    assert entity.extra_state_attributes["session_active"] is False
    assert chair.writes == []

    # An external On notification does not make the retained HA choice active.
    chair.controller.accept_status(status_with(heat=True, massage=1))
    assert entity.native_value == "Waiting for start"
    assert chair.writes == []


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_running_and_off_are_distinct_from_the_continuous_choice(
    timer_connection, chair, kind
):
    entity = ContinuousStatusSensor(timer_connection, kind)
    await start(chair, kind)
    assert entity.native_value == "Running"
    assert entity.extra_state_attributes["session_active"] is True
    expected_timer, expected_interval = (180, 175) if kind == "heat" else (45, 40)
    assert entity.extra_state_attributes["continuous_timer_minutes"] == expected_timer
    assert entity.extra_state_attributes["renewal_interval_minutes"] == expected_interval
    if kind == "heat":
        await chair.controller.set_heat(False)
    else:
        await chair.controller.set_massage("Off")
    assert entity.native_value == "Waiting for start"
    assert "turned off" in entity.extra_state_attributes["reason"]
    assert entity.extra_state_attributes["continuous_selected"] is True


async def test_heat_deferral_is_visible_and_does_not_claim_normal_renewal(timer_connection, chair):
    chair.controller.accept_status(status_with(heat=True, massage=2))
    await chair.controller.set_heat_continuous()
    entity = ContinuousStatusSensor(timer_connection, "heat")
    assert entity.native_value == "Waiting for massage"
    assert "finite or unmanaged massage" in entity.extra_state_attributes["reason"]
    assert chair.writes == []

    # Once permitted, the acknowledged renewal changes the visible session state.
    chair.controller.accept_status(status_with(heat=True, massage=0))
    assert await chair.controller._renew_heat_once(
        chair.controller._generation, chair.controller._heat_session
    )
    assert entity.native_value == "Running"
    assert entity.extra_state_attributes["renewals_completed"] == 1


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_disconnect_and_reconnect_do_not_show_a_retained_choice_as_running(
    timer_connection, chair, kind
):
    entity = ContinuousStatusSensor(timer_connection, kind)
    await start(chair, kind)
    timer_connection._client.is_connected = False
    # The UI must stop claiming readiness even before the disconnect callback.
    assert entity.available
    assert entity.native_value == "Stopped"
    assert entity.extra_state_attributes["session_active"] is False

    chair.controller.set_connected(False)
    chair.controller.set_connected(True)
    timer_connection._client.is_connected = True
    chair.controller.accept_status(status_with(heat=True, massage=1))
    writes = len(chair.writes)
    assert entity.native_value == "Waiting for start"
    assert "retained" in entity.extra_state_attributes["reason"]
    assert entity.available
    await asyncio.sleep(0)
    assert len(chair.writes) == writes


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_restoring_preferences_is_visible_without_connection_or_commands(
    timer_connection, chair, kind
):
    chair.controller.set_connected(False)
    chair.controller.restore_timer_preferences(
        {"heat": kind == "heat", "massage": kind == "massage"}
    )
    entity = ContinuousStatusSensor(timer_connection, kind)
    assert entity.available
    assert entity.native_value == "Waiting for start"
    assert "Connect to the chair" in entity.extra_state_attributes["reason"]
    assert chair.writes == []


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_failed_start_stays_visible_with_actionable_reason(timer_connection, chair, kind):
    chair.controller.accept_status(
        status_with(heat=kind == "heat", massage=1 if kind == "massage" else 0)
    )
    chair.fail_at = 1
    with pytest.raises(ControlError):
        await select_continuous(chair, kind)
    entity = ContinuousStatusSensor(timer_connection, kind)
    assert entity.native_value == "Stopped"
    assert "could not start" in entity.extra_state_attributes["reason"]
    assert entity.extra_state_attributes["continuous_selected"] is True
    assert entity.extra_state_attributes["session_active"] is False


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_failed_automatic_renewal_is_visible_without_a_user_action(
    timer_connection, chair, monkeypatch, kind
):
    monkeypatch.setattr(controller_module, f"{kind.upper()}_RENEW_SECONDS", 0.005)
    await start(chair, kind)
    chair.fail_at = len(chair.writes) + 1
    entity = ContinuousStatusSensor(timer_connection, kind)
    async with asyncio.timeout(1):
        while entity.native_value != "Stopped":  # noqa: ASYNC110 - bounded fake transport polling
            await asyncio.sleep(0.001)
    assert entity.extra_state_attributes["session_active"] is False
    assert "not retried" in entity.extra_state_attributes["reason"]
    writes = len(chair.writes)
    await asyncio.sleep(0.01)
    assert len(chair.writes) == writes


@pytest.mark.parametrize("kind,minutes", [("heat", 120), ("massage", 30)])
async def test_finite_timer_clears_only_its_continuous_status(
    timer_connection, chair, kind, minutes
):
    chair.controller.accept_status(status_with(heat=False, massage=0))
    await chair.controller.set_heat_continuous()
    await chair.controller.set_massage_continuous()
    await chair.controller.set_timer(kind, minutes)
    assert ContinuousStatusSensor(timer_connection, kind).native_value == "Not selected"
    other = "heat" if kind == "massage" else "massage"
    assert ContinuousStatusSensor(timer_connection, other).native_value == "Waiting for start"


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_reading_status_never_writes_or_creates_ticking_attributes(
    timer_connection, chair, kind
):
    await start(chair, kind)
    entity = ContinuousStatusSensor(timer_connection, kind)
    initial = entity.extra_state_attributes
    writes = len(chair.writes)
    for _ in range(20):
        chair.time += 0.1
        chair.controller.accept_status(chair.controller.status)
        assert entity.native_value == "Running"
        assert entity.extra_state_attributes == initial
    assert len(chair.writes) == writes
    initial["renewals_completed"] = 999
    assert entity.extra_state_attributes["renewals_completed"] == 0


def memory_profile(*, enabled=True, continuous=True):
    return MemoryProfile.from_status(
        status_with(positions=(2, 3, 4, 5), heat=enabled, massage=1 if enabled else 0),
        heat_continuous=continuous,
        massage_continuous=continuous,
    )


async def test_memory_status_starts_only_after_confirmed_recall_and_preference_commit(
    timer_connection, chair
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def flush():
        entered.set()
        await release.wait()

    chair.controller._preferences_flush = flush
    task = asyncio.create_task(chair.controller.recall_memory(1, memory_profile()))
    await entered.wait()
    sensors = [ContinuousStatusSensor(timer_connection, kind) for kind in ("heat", "massage")]
    assert len(chair.writes) == 2
    for sensor in sensors:
        assert sensor.native_value == "Starting"
        assert sensor.extra_state_attributes["reason"] == "Preparing Continuous renewal."
        assert sensor.extra_state_attributes["session_active"] is False
    release.set()
    await task
    for sensor in sensors:
        assert sensor.native_value == "Running"
        assert sensor.extra_state_attributes["session_active"] is True
    assert len(chair.writes) == 2


async def test_saved_off_functions_keep_the_choice_without_claiming_running(
    timer_connection, chair
):
    await chair.controller.recall_memory(1, memory_profile(enabled=False))
    for kind in ("heat", "massage"):
        sensor = ContinuousStatusSensor(timer_connection, kind)
        assert sensor.native_value == "Waiting for start"
        assert sensor.extra_state_attributes["continuous_selected"] is True
        assert sensor.extra_state_attributes["session_active"] is False
    assert len(chair.writes) == 2


async def test_failed_memory_preference_commit_shows_stopped_not_running(timer_connection, chair):
    chair.controller._preferences_flush = AsyncMock(side_effect=ControlError("Disk failed"))
    with pytest.raises(ControlError, match="Disk failed"):
        await chair.controller.recall_memory(1, memory_profile())
    for kind in ("heat", "massage"):
        sensor = ContinuousStatusSensor(timer_connection, kind)
        assert sensor.native_value == "Stopped"
        assert sensor.extra_state_attributes["session_active"] is False
    assert len(chair.writes) == 2


async def test_replacement_memory_status_cannot_be_overwritten_by_old_recall(
    timer_connection, chair
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def flush():
        entered.set()
        await release.wait()

    chair.controller._preferences_flush = flush
    older = asyncio.create_task(chair.controller.recall_memory(1, memory_profile()))
    await entered.wait()
    chair.controller._preferences_flush = AsyncMock()
    await chair.controller.recall_memory(2, memory_profile(continuous=False))
    release.set()
    with pytest.raises(CommandSuperseded):
        await older
    for kind in ("heat", "massage"):
        sensor = ContinuousStatusSensor(timer_connection, kind)
        assert sensor.native_value == "Not selected"
        assert sensor.extra_state_attributes["session_active"] is False


async def test_stop_during_memory_commit_does_not_claim_running(timer_connection, chair):
    entered, release = asyncio.Event(), asyncio.Event()

    async def flush():
        entered.set()
        await release.wait()

    chair.controller._preferences_flush = flush
    older = asyncio.create_task(chair.controller.recall_memory(1, memory_profile()))
    await entered.wait()
    await chair.controller.stop_movement()
    release.set()
    with pytest.raises(CommandSuperseded):
        await older
    for kind in ("heat", "massage"):
        sensor = ContinuousStatusSensor(timer_connection, kind)
        assert sensor.native_value == "Stopped"
        assert sensor.extra_state_attributes["session_active"] is False


async def test_recalled_heat_is_running_until_a_due_renewal_is_actually_deferred(
    timer_connection, chair
):
    profile = MemoryProfile.from_status(
        status_with(positions=(2, 3, 4, 5), heat=True, massage=1),
        heat_continuous=True,
        massage_continuous=False,
    )
    await chair.controller.recall_memory(1, profile)
    sensor = ContinuousStatusSensor(timer_connection, "heat")
    assert sensor.native_value == "Running"
    before = len(chair.writes)
    assert not await chair.controller._renew_heat_once(
        chair.controller._generation, chair.controller._heat_session
    )
    assert sensor.native_value == "Waiting for massage"
    assert len(chair.writes) == before
