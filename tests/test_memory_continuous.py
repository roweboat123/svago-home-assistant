"""Memories restore explicit Continuous choices without replaying their target."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from conftest import status_with
from test_memory_controller import TravellingChair, is_memory_write

from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.memory_protocol import MemoryProfile
from custom_components.svago.protocol import Status, checksum


def saved_profile(*, heat=True, massage="Gentle", heat_continuous=True, massage_continuous=True):
    modes = {"Off": 0, "Gentle": 1}
    return MemoryProfile.from_status(
        status_with(
            positions=(2, 3, 4, 5), heat=heat, massage=modes[massage], **{"6": 3, "7": 0x84}
        ),
        heat_continuous=heat_continuous,
        massage_continuous=massage_continuous,
    )


def test_stationary_snapshot_includes_selected_choices_without_writing(chair):
    chair.controller.restore_timer_preferences({"heat": True, "massage": True})
    chair.time = 2
    chair.controller.accept_status(chair.controller.status)
    profile = chair.controller.snapshot_memory()
    assert profile.heat_continuous and profile.massage_continuous
    assert profile.massage == "Off"
    assert profile.heat_timer_seconds == 3600
    assert profile.recall_heat_seconds == 10800
    assert chair.writes == []


def test_old_profile_defaults_to_finite_and_new_profile_roundtrips():
    profile = saved_profile()
    encoded = profile.to_dict()
    assert MemoryProfile.from_dict(encoded) == profile
    del encoded["heat_continuous"]
    del encoded["massage_continuous"]
    restored = MemoryProfile.from_dict(encoded)
    assert not restored.heat_continuous and not restored.massage_continuous
    assert restored.positions == profile.positions
    assert restored.heat_timer_seconds == profile.heat_timer_seconds


@pytest.mark.parametrize("field", ["heat_continuous", "massage_continuous"])
@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_continuous_flags_require_actual_booleans(field, value):
    encoded = saved_profile().to_dict()
    encoded[field] = value
    with pytest.raises(ValueError, match="booleans"):
        MemoryProfile.from_dict(encoded)


def test_incomplete_new_schema_is_rejected():
    encoded = saved_profile().to_dict()
    del encoded["heat_continuous"]
    with pytest.raises(ValueError, match="Invalid saved"):
        MemoryProfile.from_dict(encoded)


@pytest.mark.parametrize(
    "heat_continuous,massage_continuous",
    [(False, False), (True, False), (False, True), (True, True)],
)
@pytest.mark.parametrize(
    "heat,massage", [(False, "Off"), (True, "Off"), (False, "Gentle"), (True, "Gentle")]
)
async def test_recall_restores_choices_only_manages_on_functions_and_sends_target_once(
    chair, heat_continuous, massage_continuous, heat, massage
):
    profile = saved_profile(
        heat=heat,
        massage=massage,
        heat_continuous=heat_continuous,
        massage_continuous=massage_continuous,
    )
    await chair.controller.recall_memory(2, profile)
    assert len(chair.writes) == 2  # Activation, then one target; no timer rewrite.
    targets = [packet for packet in chair.writes if is_memory_write(packet)]
    assert len(targets) == 1
    assert int.from_bytes(targets[0][6:8], "big") == (
        2700 if massage_continuous and massage != "Off" else 900
    )
    assert int.from_bytes(targets[0][8:10], "big") == (10800 if heat_continuous and heat else 3600)
    assert chair.controller.timer_preferences == {
        "heat": heat_continuous,
        "massage": massage_continuous,
    }
    assert chair.controller.heat_renewal_diagnostics["active"] is (heat_continuous and heat)
    assert chair.controller.massage_renewal_diagnostics["active"] is (
        massage_continuous and massage != "Off"
    )
    if heat_continuous and heat and massage != "Off" and not massage_continuous:
        assert chair.controller.heat_renewal_diagnostics["outcome"] == "running"
        before = list(chair.writes)
        assert not await chair.controller._renew_heat_once(
            chair.controller._generation, chair.controller._heat_session
        )
        assert chair.writes == before
        assert chair.controller.heat_renewal_diagnostics["outcome"] == "deferred_massage"
    await chair.controller.cancel_active()


def test_inactive_zero_timers_are_preserved_with_continuous_choices():
    profile = MemoryProfile.from_status(
        status_with(heat=False, massage=0, **{"6": 0, "7": 1, "8": 0, "9": 0}),
        heat_continuous=True,
        massage_continuous=True,
    )
    packet = profile.command(status_with(), 1)
    assert packet[6:10] == b"\x00\x01\x00\x00"
    assert packet[17] == 0 and packet[19] == 2


async def test_memory_choices_persist_once_and_no_worker_starts_before_commit(chair):
    changed = []
    started, release = asyncio.Event(), asyncio.Event()
    chair.controller._preferences_changed = lambda choices: changed.append(dict(choices))

    async def flush():
        started.set()
        await release.wait()

    chair.controller._preferences_flush = flush
    task = asyncio.create_task(chair.controller.recall_memory(1, saved_profile()))
    await started.wait()
    assert changed[-1] == {"heat": True, "massage": True}
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert len(chair.writes) == 2
    release.set()
    await task
    assert chair.controller.heat_renewal_diagnostics["active"]
    assert chair.controller.massage_renewal_diagnostics["active"]
    await chair.controller.cancel_active()


@pytest.mark.parametrize("replacement", ["stop", "recall", "disconnect"])
async def test_supersession_while_persisting_never_starts_obsolete_workers(chair, replacement):
    started, release = asyncio.Event(), asyncio.Event()

    async def flush():
        started.set()
        await release.wait()

    chair.controller._preferences_flush = flush
    task = asyncio.create_task(chair.controller.recall_memory(1, saved_profile()))
    await started.wait()
    if replacement == "stop":
        await chair.controller.stop_movement()
    elif replacement == "recall":
        chair.controller.reserve_memory_recall()
    else:
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with())
    release.set()
    with pytest.raises((CommandSuperseded, ControlError)):
        await task
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert len([p for p in chair.writes if is_memory_write(p)]) == 1
    if replacement == "recall":
        # A reservation prevents obsolete workers from starting, but it has
        # not applied a new profile or changed the choices being persisted.
        assert chair.controller.timer_preferences == {"heat": True, "massage": True}
    await chair.controller.cancel_active()


@pytest.mark.parametrize("failure", ["activation", "superseded_before_target", "target_write"])
async def test_only_dispatch_of_valid_recall_retires_existing_workers(chair, failure):
    controller = chair.controller
    await controller.recall_memory(1, saved_profile())
    try:
        before = len(chair.writes)
        choices = []
        controller._preferences_changed = choices.append
        if failure == "superseded_before_target":
            chair.on_write = lambda _: controller.reserve_memory_recall()
            expected = CommandSuperseded
        else:
            chair.fail_at = before + (1 if failure == "activation" else 2)
            expected = ControlError
        target = saved_profile(massage_continuous=False, heat_continuous=False)
        with pytest.raises(expected):
            await controller.recall_memory(2, target, request=controller.reserve_memory_recall())
        dispatched = failure == "target_write"
        assert sum(is_memory_write(packet) for packet in chair.writes[before:]) == int(dispatched)
        assert controller.massage_renewal_diagnostics["active"] is not dispatched
        assert controller.heat_renewal_diagnostics["active"] is not dispatched
        assert controller.timer_preferences == {"heat": True, "massage": True}
        assert choices == []
    finally:
        await controller.cancel_active()


async def test_failed_commit_leaves_no_workers_or_extra_ble_write(chair):
    chair.controller._preferences_flush = AsyncMock(side_effect=ControlError("Disk failed"))
    with pytest.raises(ControlError, match="Disk failed"):
        await chair.controller.recall_memory(1, saved_profile())
    assert len(chair.writes) == 2
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert chair.controller.heat_renewal_diagnostics["outcome"] == "start_failed"


async def test_failed_motion_does_not_restore_choices_or_start_renewal():
    chair = TravellingChair()
    chair.on_sleep = lambda: chair.report((0, 0, 0, 0))
    with pytest.raises(ControlError, match="stopped changing"):
        await chair.controller.recall_memory(1, saved_profile())
    assert chair.controller.timer_preferences == {"heat": False, "massage": False}
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert len(chair.memory_writes) == 1


async def test_external_setting_change_during_commit_does_not_start_renewal(chair):
    async def flush():
        chair.controller.accept_status(status_with(positions=(2, 3, 4, 5), heat=False))

    chair.controller._preferences_flush = flush
    with pytest.raises(CommandSuperseded):
        await chair.controller.recall_memory(1, saved_profile())
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert len(chair.writes) == 2


async def test_external_selector_change_during_commit_does_not_start_renewal(chair):
    async def flush():
        raw = bytearray(chair.controller.status.raw)
        raw[10] = 0x12  # Another slot can have identical positions and settings.
        raw[25] = checksum(raw)
        chair.controller.accept_status(Status(bytes(raw)))

    chair.controller._preferences_flush = flush
    with pytest.raises(CommandSuperseded):
        await chair.controller.recall_memory(1, saved_profile())
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert len(chair.writes) == 2


async def test_first_renewal_delay_accounts_for_time_spent_travelling():
    chair = TravellingChair()
    profile = saved_profile()
    massage_loop = AsyncMock(side_effect=lambda *args, **kwargs: None)
    heat_loop = AsyncMock(side_effect=lambda *args, **kwargs: None)
    chair.controller._massage_renewal_loop = massage_loop
    chair.controller._heat_renewal_loop = heat_loop

    def progress():
        if chair.time >= 1:
            chair.report(profile.positions)
        else:
            chair.report((1, 1, 1, 1))

    chair.on_sleep = progress
    await chair.controller.recall_memory(1, profile)
    await asyncio.sleep(0)
    elapsed = chair.time
    assert elapsed >= 1
    assert massage_loop.call_args.kwargs["initial_delay"] == pytest.approx(2400 - elapsed)
    assert heat_loop.call_args.kwargs["initial_delay"] == pytest.approx(10500 - elapsed)
    assert len(chair.memory_writes) == 1
    await chair.controller.cancel_active()


async def test_reconnect_after_continuous_memory_retains_choices_without_restarting(chair):
    await chair.controller.recall_memory(1, saved_profile())
    before = list(chair.writes)
    chair.controller.set_connected(False)
    chair.controller.set_connected(True)
    chair.controller.accept_status(status_with(heat=True, massage=1))
    await asyncio.sleep(0)
    assert chair.controller.timer_preferences == {"heat": True, "massage": True}
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert chair.writes == before
    await chair.controller.cancel_active()


async def test_finite_memory_replaces_continuous_memory_and_restores_its_timers(chair):
    profile = saved_profile()
    await chair.controller.recall_memory(1, profile)
    finite = replace(profile, heat_continuous=False, massage_continuous=False)
    await chair.controller.recall_memory(2, finite)
    assert chair.controller.timer_preferences == {"heat": False, "massage": False}
    assert chair.controller.status.timer_minutes("heat") == 60
    assert chair.controller.status.timer_minutes("massage") == 15
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert len([p for p in chair.writes if is_memory_write(p)]) == 2
    await chair.controller.cancel_active()


async def test_new_recall_reserved_during_travel_prevents_old_timer_restore():
    chair = TravellingChair()
    saved = saved_profile()
    reservation = []

    def finish_old_move():
        reservation.append(chair.controller.reserve_memory_recall())
        chair.report(saved.positions)

    chair.on_sleep = finish_old_move
    with pytest.raises(CommandSuperseded):
        await chair.controller.recall_memory(1, saved)
    assert reservation
    assert chair.controller.timer_preferences == {"heat": False, "massage": False}
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert len(chair.memory_writes) == 1


async def test_explicit_on_after_off_memory_starts_saved_continuous_choices(chair):
    await chair.controller.recall_memory(1, saved_profile(heat=False, massage="Off"))
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    await chair.controller.set_heat(True)
    assert chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    await chair.controller.set_massage("Gentle")
    assert chair.controller.heat_renewal_diagnostics["active"]
    assert chair.controller.massage_renewal_diagnostics["active"]
    await chair.controller.cancel_active()


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_observed_off_then_passive_on_during_commit_cannot_revive_session(chair, kind):
    async def flush():
        original = chair.controller.status
        raw = bytearray(original.raw)
        raw[19 if kind == "heat" else 17] = 2 if kind == "heat" else 0
        raw[25] = checksum(raw)
        chair.controller.accept_status(Status(bytes(raw)))
        chair.controller.accept_status(original)

    chair.controller._preferences_flush = flush
    with pytest.raises(CommandSuperseded):
        await chair.controller.recall_memory(1, saved_profile())
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert len(chair.writes) == 2


async def test_changed_position_during_commit_stops_pending_renewal(chair):
    async def flush():
        raw = bytearray(chair.controller.status.raw)
        raw[13] += 1
        raw[25] = checksum(raw)
        chair.controller.accept_status(Status(bytes(raw)))

    chair.controller._preferences_flush = flush
    with pytest.raises(ControlError, match="settings changed"):
        await chair.controller.recall_memory(1, saved_profile())
    assert not chair.controller.heat_renewal_diagnostics["active"]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert len(chair.writes) == 2
