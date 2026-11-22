"""Stationary snapshots and monitored, non-replayed HA memory recalls."""

import asyncio

import pytest
from conftest import FakeChair, status_with

from custom_components.svago import controller as controller_module
from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.memory_protocol import MemoryProfile
from custom_components.svago.protocol import Status, checksum


def profile(positions=(2, 3, 4, 5), **changes):
    return MemoryProfile.from_status(status_with(positions=positions, **changes))


def is_memory_write(payload):
    return payload[5] == 0 and payload[10] in (0x11, 0x12, 0x13) and max(payload[13:17]) < 0x80


class TravellingChair(FakeChair):
    """Accept options immediately, but let tests drive actual position reports."""

    def __init__(self, positions=(0, 0, 0, 0)):
        super().__init__()
        self.controller.accept_status(status_with(positions=positions))
        self.memory_payload = None
        self.acknowledge = True
        self.ack_changes = {}

    async def write(self, payload):
        if not is_memory_write(payload):
            await super().write(payload)
            return
        self.writes.append(payload)
        self.memory_payload = payload
        if self.on_write:
            self.on_write(payload)
        if self.acknowledge:
            self.report(self.controller.status.positions, **self.ack_changes)

    def report(self, positions, **changes):
        raw = bytearray(self.memory_payload)
        raw[1:3] = b"\x01\x02"
        raw[13:17] = bytes(positions)
        for key, value in changes.items():
            raw[int(key)] = value
        raw[25] = checksum(raw)
        self.controller.accept_status(Status(bytes(raw)))

    @property
    def memory_writes(self):
        return [packet for packet in self.writes if is_memory_write(packet)]


def test_snapshot_requires_observed_stationarity_without_writes(chair):
    controller = chair.controller
    with pytest.raises(ControlError, match="stable"):
        controller.snapshot_memory()
    chair.time = 1.9
    controller.accept_status(controller.status)
    assert not controller.can_save_memory
    chair.time = 2.0
    # Time passing without another status is not stationary evidence.
    assert not controller.can_save_memory
    controller.accept_status(controller.status)
    saved = controller.snapshot_memory()
    assert saved == MemoryProfile.from_status(controller.status)
    assert chair.writes == []


def test_identical_status_announces_save_availability_transition(chair):
    changes = []
    chair.controller._changed = lambda: changes.append(chair.controller.can_save_memory)
    chair.time = 1
    chair.controller.accept_status(chair.controller.status)
    assert changes == []
    chair.time = 2
    chair.controller.accept_status(chair.controller.status)
    assert changes == [True]
    chair.time = 2.2
    chair.controller.accept_status(chair.controller.status)
    assert changes == [True]
    chair.controller.accept_status(status_with(positions=(5, 7, 16, 28)))
    assert changes == [True, False]


def test_external_quantized_movement_cannot_enable_save_between_steps(chair):
    for second in range(1, 6):
        chair.time = second - 0.1
        chair.controller.accept_status(chair.controller.status)
        assert not chair.controller.can_save_memory
        chair.time = second
        chair.controller.accept_status(status_with(positions=(5, second, 16, 28)))
    chair.time = 7
    chair.controller.accept_status(chair.controller.status)
    assert chair.controller.can_save_memory
    assert chair.writes == []


@pytest.mark.parametrize("reset", ["stale_gap", "reconnect"])
def test_snapshot_stability_restarts_after_observation_gap(chair, reset):
    chair.time = 2
    chair.controller.accept_status(chair.controller.status)
    assert chair.controller.can_save_memory
    if reset == "stale_gap":
        chair.time = 6
    else:
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
    chair.controller.accept_status(status_with())
    assert not chair.controller.can_save_memory
    chair.time += 2
    chair.controller.accept_status(chair.controller.status)
    assert chair.controller.can_save_memory


@pytest.mark.parametrize(
    "changes",
    [{"18": 1}, {"18": 2}, {"positions": (0, 0, 41, 0)}, {"20": 4}, {"6": 1}, {"19": 0}],
)
def test_snapshot_rejects_active_or_unmapped_state_without_writes(chair, changes):
    chair.controller.accept_status(status_with(**changes))
    chair.time = 2
    chair.controller.accept_status(chair.controller.status)
    with pytest.raises(ControlError):
        chair.controller.snapshot_memory()
    assert not chair.controller.can_save_memory
    assert chair.writes == []


async def test_snapshot_does_not_cancel_active_movement(chair):
    task = asyncio.create_task(chair.controller.adjust_lumbar(True))
    await asyncio.sleep(0)
    before = list(chair.writes)
    with pytest.raises(ControlError, match="stop"):
        chair.controller.snapshot_memory()
    assert not task.done()
    assert chair.writes == before
    await task


async def test_save_after_native_preset_requires_new_stationarity_but_sends_no_stop(chair):
    chair.time = 2
    chair.controller.accept_status(chair.controller.status)
    assert chair.controller.can_save_memory
    await chair.controller.select_preset("zgr")
    assert chair.controller._needs_stop
    assert not chair.controller.can_save_memory
    before = list(chair.writes)
    chair.time = 3
    chair.controller.accept_status(chair.controller.status)
    chair.time = 4
    chair.controller.accept_status(chair.controller.status)
    assert not chair.controller.can_save_memory
    chair.time = 5
    chair.controller.accept_status(chair.controller.status)
    assert chair.controller.snapshot_memory()
    assert chair.controller._needs_stop
    assert chair.writes == before


async def test_recall_restores_profile_once_using_live_unknown_context(chair):
    saved = profile(heat=False, massage=5, **{"20": 1, "21": 0, "22": 3})
    chair.controller.accept_status(status_with(**{"11": 3, "12": 4, "23": 5, "24": 6}))
    await chair.controller.recall_memory(2, saved)
    commands = [packet for packet in chair.writes if is_memory_write(packet)]
    assert len(commands) == 1
    packet = commands[0]
    assert packet[10] == 0x12
    assert tuple(packet[13:17]) == saved.positions
    assert packet[17:23] == bytes((5, 0, 2, 1, 0, 3))
    assert packet[11:13] == b"\x03\x04"
    assert packet[23:25] == b"\x05\x06"
    assert saved.matches_settings(chair.controller.status)
    assert chair.controller.memory_requests[-1]["outcome"] == "target_reached"
    assert not chair.controller._needs_stop


@pytest.mark.parametrize("slot", [0, 4, True, 1.0, "1"])
async def test_invalid_recall_slot_has_no_side_effect(chair, slot):
    with pytest.raises(ControlError, match="Memory 1"):
        await chair.controller.recall_memory(slot, profile())
    assert chair.writes == []


@pytest.mark.parametrize("problem", ["stale", "bad_positions", "missing_profile"])
async def test_invalid_recall_inputs_do_not_activate(chair, problem):
    saved = profile()
    if problem == "stale":
        chair.time = 4
    elif problem == "bad_positions":
        chair.controller.accept_status(status_with(positions=(0, 0, 41, 0)))
    else:
        saved = None
    with pytest.raises(ControlError):
        await chair.controller.recall_memory(1, saved)
    assert chair.writes == []


async def test_existing_matching_status_cannot_acknowledge_new_recall():
    chair = TravellingChair(positions=(2, 3, 4, 5))
    saved = profile()
    chair.controller.accept_status(status_with(positions=saved.positions, **{"10": 0x11}))
    chair.acknowledge = False
    with pytest.raises(ControlError, match="did not confirm"):
        await chair.controller.recall_memory(1, saved)
    assert len(chair.memory_writes) == 1
    assert chair.writes[-1][13:17] == b"\x80" * 4


@pytest.mark.parametrize("changes", [{"10": 0x12}, {"19": 2}, {"20": 3}])
async def test_new_mismatched_status_does_not_acknowledge_recall(changes):
    chair = TravellingChair()
    chair.ack_changes = changes
    with pytest.raises(ControlError, match="did not confirm"):
        await chair.controller.recall_memory(1, profile())
    assert len(chair.memory_writes) == 1
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_all_axes_can_progress_at_different_times_without_resending():
    chair = TravellingChair()
    saved = profile((2, 2, 8, 4))

    def progress():
        second = int(chair.time)
        chair.report((min(second, 2), min(max(second - 5, 0), 2), min(second, 8), min(second, 4)))

    chair.on_sleep = progress
    await chair.controller.recall_memory(3, saved)
    assert chair.controller.status.positions == saved.positions
    assert chair.time >= 8
    assert len(chair.memory_writes) == 1
    assert len(chair.writes) == 2  # Activation, absolute target; no pulse stream.


@pytest.mark.parametrize(
    "target,reports",
    [
        ((2, 0, 0, 0), [(1, 0, 0, 0), (3, 0, 0, 0), (2, 0, 0, 0)]),
        ((2, 2, 0, 0), [(0, 0, 1, 0), (0, 1, 2, 0), (1, 1, 1, 0), (2, 2, 0, 0)]),
    ],
)
async def test_temporary_overshoot_and_coupling_require_exact_final_target(target, reports):
    chair = TravellingChair()
    pending = list(reports)
    chair.on_sleep = lambda: chair.report(pending.pop(0))
    await chair.controller.recall_memory(1, profile(target))
    assert pending == []
    assert chair.controller.status.positions == target
    assert len(chair.memory_writes) == 1


@pytest.mark.parametrize("motion", ["stationary", "jitter", "away"])
async def test_fresh_nonprogressing_feedback_stops_within_three_seconds(motion):
    chair = TravellingChair((5, 0, 0, 0))

    def report():
        step = int(chair.time * 10)
        position = {"stationary": 5, "jitter": 5 + step % 2, "away": min(5 + step, 17)}[motion]
        chair.report((position, 0, 0, 0))

    chair.on_sleep = report
    with pytest.raises(ControlError, match="stopped changing"):
        await chair.controller.recall_memory(1, profile((2, 0, 0, 0)))
    assert 3 <= chair.time < 3.2
    assert len(chair.memory_writes) == 1
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_stale_feedback_stops_recall_without_replaying_target():
    chair = TravellingChair()
    chair.controller._stale_after = 0.2
    with pytest.raises(ControlError, match="Fresh chair status"):
        await chair.controller.recall_memory(1, profile())
    # No blind stop can be safely derived from stale state, and no target retry.
    assert len(chair.memory_writes) == 1
    assert len(chair.writes) == 2


async def test_progress_cannot_extend_overall_recall_deadline(monkeypatch):
    monkeypatch.setattr(controller_module, "MEMORY_MAX_SECONDS", 0.5)
    chair = TravellingChair()
    chair.on_sleep = lambda: chair.report((int(round(chair.time * 10)), 0, 0, 0))
    with pytest.raises(ControlError, match="time limit"):
        await chair.controller.recall_memory(1, profile((17, 0, 0, 0)))
    assert chair.time < 0.7
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_out_of_range_feedback_stops_recall():
    chair = TravellingChair()
    chair.on_sleep = lambda: chair.report((0, 0, 41, 0))
    with pytest.raises(ControlError, match="Unexpected memory position"):
        await chair.controller.recall_memory(1, profile())
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_external_heat_change_stops_motion_and_release_preserves_new_heat():
    chair = TravellingChair()
    chair.on_sleep = lambda: chair.report((0, 0, 0, 0), **{"19": 2})
    with pytest.raises(ControlError, match="settings changed"):
        await chair.controller.recall_memory(1, profile(heat=True))
    assert len(chair.memory_writes) == 1
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert chair.writes[-1][19] == 2


@pytest.mark.parametrize("action", ["stop", "heat", "replacement"])
async def test_new_request_releases_memory_before_taking_over(action):
    chair = TravellingChair()
    task = asyncio.create_task(chair.controller.recall_memory(1, profile()))
    await asyncio.sleep(0)
    assert chair.controller.busy
    if action == "stop":
        await chair.controller.stop_movement()
    elif action == "heat":
        await chair.controller.set_heat(False)
    else:
        # A second profile already at the current position completes immediately.
        await chair.controller.recall_memory(2, profile((0, 0, 0, 0)))
    with pytest.raises(CommandSuperseded):
        await task
    first = next(index for index, packet in enumerate(chair.writes) if is_memory_write(packet))
    assert chair.writes[first + 1][13:17] == b"\x80" * 4
    assert len([packet for packet in chair.memory_writes if packet[10] == 0x11]) == 1
    assert chair.controller.memory_requests[0]["outcome"] == "replaced_by_new_request"
    assert not chair.controller.busy


async def test_task_cancellation_releases_memory_once_without_resending():
    chair = TravellingChair()
    task = asyncio.create_task(chair.controller.recall_memory(1, profile()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(chair.memory_writes) == 1
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert chair.controller.memory_requests[-1]["outcome"] == "cancelled"


async def test_reconnect_prevents_old_release_and_target_replay():
    chair = TravellingChair()

    def reconnect():
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with(heat=False))

    chair.on_sleep = reconnect
    with pytest.raises(ControlError, match="Fresh chair status"):
        await chair.controller.recall_memory(1, profile())
    assert len(chair.memory_writes) == 1
    assert len(chair.writes) == 2
    assert not chair.controller.can_save_memory


@pytest.mark.parametrize("newer", ["stop", "heat", "reconnect", "recall"])
async def test_reserved_recall_cannot_run_after_later_request(chair, newer):
    request = chair.controller.reserve_memory_recall()
    assert chair.writes == []
    if newer == "stop":
        await chair.controller.stop_movement()
    elif newer == "heat":
        await chair.controller.set_heat(False)
    elif newer == "reconnect":
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with())
    else:
        latest = chair.controller.reserve_memory_recall()
        await chair.controller.recall_memory(2, profile(), request=latest)
    before = list(chair.writes)
    with pytest.raises(CommandSuperseded):
        await chair.controller.recall_memory(1, profile(), request=request)
    assert chair.writes == before


async def test_reserving_memory_does_not_interrupt_running_motion():
    chair = TravellingChair()
    active = asyncio.create_task(chair.controller.recall_memory(1, profile()))
    await asyncio.sleep(0)
    before = list(chair.writes)
    chair.controller.reserve_memory_recall()
    assert not active.done()
    assert chair.writes == before
    await chair.controller.cancel_active()
    assert active.cancelled()


async def test_reserving_memory_invalidates_older_queued_settings(chair):
    async with chair.controller._settings_lock:
        older = asyncio.create_task(chair.controller.set_timer("heat", 120))
        await asyncio.sleep(0)
        request = chair.controller.reserve_memory_recall()
    with pytest.raises(CommandSuperseded):
        await older
    assert chair.writes == []
    await chair.controller.recall_memory(1, profile(), request=request)
    assert chair.controller.memory_requests[-1]["outcome"] == "target_reached"


async def test_newer_queued_setting_invalidates_reserved_memory_immediately(chair):
    request = chair.controller.reserve_memory_recall()
    async with chair.controller._settings_lock:
        newer = asyncio.create_task(chair.controller.set_timer("heat", 120))
        await asyncio.sleep(0)
        with pytest.raises(CommandSuperseded):
            await chair.controller.recall_memory(1, profile(), request=request)
        assert chair.writes == []
    await newer
    assert chair.controller.status.timer_minutes("heat") == 120


async def test_memory_diagnostics_keep_only_recent_requests(chair):
    for index in range(6):
        await chair.controller.recall_memory(index % 3 + 1, profile((index, 0, 0, 0)))
    assert len(chair.controller.memory_requests) == 4
    assert [trace["target_positions"][0] for trace in chair.controller.memory_requests] == [
        2,
        3,
        4,
        5,
    ]
    assert all(trace["outcome"] == "target_reached" for trace in chair.controller.memory_requests)


async def test_memory_diagnostic_position_history_is_bounded():
    chair = TravellingChair()
    target = (17, 22, 40, 28)
    current = [0, 0, 0, 0]

    def progress():
        for index, value in enumerate(target):
            if current[index] < value:
                current[index] += 1
                break
        chair.report(tuple(current))

    chair.on_sleep = progress
    await chair.controller.recall_memory(1, profile(target))
    reports = chair.controller.memory_requests[-1]["reports"]
    assert len(reports) == 64
    assert reports[-1]["positions"] == list(target)
