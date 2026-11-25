"""Behavioral checks for preservation, cancellation, failure, and reconnection."""

import asyncio

import pytest
from conftest import status_with

from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.protocol import MASSAGE_MODES, OBSERVED_BOUNDS, Status, checksum


@pytest.mark.parametrize("heat", [True, False])
@pytest.mark.parametrize("inflate", [True, False])
async def test_lumbar_preserves_heat_and_massage_for_whole_burst(chair, heat, inflate):
    chair.controller.accept_status(status_with(heat=heat, massage=4))
    await chair.controller.adjust_lumbar(inflate)
    assert len(chair.writes) == 32
    assert chair.writes[0][5] == 0x11
    assert [p[18] for p in chair.writes] == [0] + [2 if inflate else 1] * 30 + [0]
    assert chair.time == pytest.approx(3)
    assert all(p[19] == (1 if heat else 2) and p[17] == 4 for p in chair.writes)
    assert all(p[13:17] == b"\x8f" * 4 and p[25] == checksum(p) for p in chair.writes[1:])


async def test_heat_and_massage_changes_during_burst_including_release(chair):
    def new_state():
        n = len(chair.writes)
        if n == 5:
            chair.controller.accept_status(status_with(heat=False, massage=3))
        if n == 30:
            chair.controller.accept_status(status_with(heat=True, massage=2, **{"10": 0x13}))

    chair.on_sleep = new_state
    await chair.controller.adjust_lumbar(True)
    assert all(p[19] == 1 for p in chair.writes[:5])
    assert all(p[19] == 2 and p[17] == 3 for p in chair.writes[5:30])
    assert chair.writes[-1][19] == 1
    assert chair.writes[-1][17] == 2 and chair.writes[-1][10] == 0x13


async def test_lumbar_release_deadline_counts_transport_latency(chair):
    starts = []

    def delay(packet):
        if packet[18] == 2:
            starts.append(chair.time)
            chair.time += 0.1

    chair.on_write = delay
    await chair.controller.adjust_lumbar(True)
    assert starts and max(starts) < 3
    assert chair.time - starts[0] == pytest.approx(3)
    assert chair.writes[-1][18] == 0


async def test_lumbar_deadline_interrupts_blocked_write_and_attempts_release(chair, monkeypatch):
    from custom_components.svago import controller

    monkeypatch.setattr(controller, "LUMBAR_BURST_SECONDS", 0.02)
    attempted = []

    async def blocked(packet):
        attempted.append(packet[18])
        if packet[18] == 1:
            await asyncio.Event().wait()
        await chair.write(packet)

    chair.controller._write = blocked
    with pytest.raises(ControlError, match="Lumbar write timed out"):
        await chair.controller.adjust_lumbar(False)
    assert attempted == [0, 1, 0]  # activation, one attempted deflate, release


async def test_lumbar_deadline_during_sleep_releases_once_and_completes(chair, monkeypatch):
    from custom_components.svago import controller

    monkeypatch.setattr(controller, "LUMBAR_BURST_SECONDS", 0.02)

    async def blocked_sleep(_seconds):
        await asyncio.Event().wait()

    chair.controller._sleep = blocked_sleep
    await asyncio.wait_for(chair.controller.adjust_lumbar(True), 1)

    assert [packet[18] for packet in chair.writes] == [0, 2, 0]
    assert chair.writes[0][5] == 0x11
    assert chair.writes[-1][13:17] == b"\x8f" * 4
    assert not chair.controller.busy


@pytest.mark.parametrize("mode", MASSAGE_MODES)
async def test_massage_selection_preserves_heat(chair, mode):
    chair.controller.accept_status(status_with(heat=False))
    await chair.controller.set_massage(mode)
    assert len(chair.writes) == (1 if mode == "Off" else 2)
    assert chair.writes[0][19] == 2
    assert chair.controller.status.massage == mode


async def test_heat_preserves_massage_and_options(chair):
    chair.controller.accept_status(status_with(massage=5, **{"10": 0x13}))
    await chair.controller.set_heat(False)
    assert chair.writes[0][17] == 5
    assert chair.writes[0][10] == 0x13
    assert chair.controller.status.heat is False


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("direction", [-1, 1])
async def test_directional_step_leaves_other_motors_untouched_then_releases(
    chair, index, direction
):
    chair.controller.accept_status(status_with(positions=(5, 6, 16, 27), heat=False))
    before = chair.controller.status
    await chair.controller.step_position(index, direction)
    expected = [0x8F] * 4
    expected[index] = 0x82 if direction > 0 else 0x81
    assert chair.writes[0][5] == 0x11
    assert list(chair.writes[1][13:17]) == expected
    assert chair.writes[1][19] == 2
    boundary = OBSERVED_BOUNDS[index][1 if direction > 0 else 0]
    increments = min(5 if index in (0, 3) else 3, abs(boundary - before.positions[index]))
    assert len(chair.writes) == increments + 2
    assert chair.writes[-1][13:17] == b"\x80" * 4
    positions = list(before.positions)
    positions[index] += direction * increments
    assert chair.controller.status.positions == tuple(positions)
    trace = chair.controller.tap_requests[-1]
    assert trace["positions_before"] == list(before.positions)
    assert trace["positions_after"] == positions
    assert trace["feedback_changed"] is True
    assert trace["outcome"] == "pulse_completed"


async def test_press_at_observed_limit_is_a_noop_without_writing(chair):
    assert await chair.controller.step_position(3, 1) == "at_observed_limit"
    assert chair.writes == []


@pytest.mark.parametrize("state", ["stale", "disconnected", "unknown"])
async def test_no_writes_without_fresh_known_state(chair, state):
    if state == "stale":
        chair.time = 4
    elif state == "disconnected":
        chair.controller.set_connected(False)
    else:
        chair.controller.accept_status(status_with(**{"19": 0}))
    with pytest.raises(ControlError):
        await chair.controller.adjust_lumbar(True)
    assert chair.writes == []


async def test_new_heat_request_replaces_lumbar_after_release(chair):
    task = asyncio.create_task(chair.controller.adjust_lumbar(True))
    await asyncio.sleep(0)
    await chair.controller.set_heat(False)
    with pytest.raises(CommandSuperseded):
        await task
    assert [p[18] for p in chair.writes] == [0, 2, 0, 0, 0]
    assert chair.writes[-2][13:17] == b"\x80" * 4
    assert chair.writes[-1][19] == 2


async def test_cancellation_releases_with_latest_heat(chair):
    task = asyncio.create_task(chair.controller.adjust_lumbar(True))
    await asyncio.sleep(0)
    chair.controller.accept_status(status_with(heat=False))
    await chair.controller.cancel_active()
    assert task.cancelled()
    assert len(chair.writes) == 3
    assert chair.writes[-1][18:20] == b"\x00\x02"
    assert not chair.controller.busy


async def test_stop_interrupts_burst_then_sends_motor_stop(chair):
    task = asyncio.create_task(chair.controller.adjust_lumbar(True))
    await asyncio.sleep(0)
    await chair.controller.stop_movement()
    with pytest.raises(CommandSuperseded):
        await task
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert chair.writes[-1][18] == 0
    assert not chair.controller.busy


async def test_write_failure_attempts_release_but_does_not_retry_action(chair):
    chair.fail_at = 4  # Activation, then failure on the third lumbar write.
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.adjust_lumbar(True)
    assert [p[18] for p in chair.writes] == [0, 2, 2, 2, 0]
    assert not chair.controller.busy


@pytest.mark.parametrize("fail_direction", [False, True])
async def test_failed_lumbar_release_gates_next_movement(chair, fail_direction):
    def fail(packet):
        if packet[5] == 0 and (packet[18] == 0 or fail_direction):
            raise OSError("simulated failure")

    chair.on_write = fail
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.adjust_lumbar(True)
    assert chair.writes[-1][13:17] == b"\x8f" * 4 and chair.writes[-1][18] == 0

    # If even the handoff fails, no activation or new movement may slip through.
    count = len(chair.writes)
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.step_position(0, 1)
    assert len(chair.writes) == count + 1
    assert chair.writes[count][13:17] == b"\x80" * 4 and chair.writes[count][18] == 0

    chair.on_write = None
    await chair.controller.step_position(0, 1)
    assert chair.writes[count + 1] == chair.writes[count]
    assert chair.writes[count + 2][5] == 0x11
    assert chair.writes[count + 3][13] == 0x82


async def test_cancelled_final_lumbar_release_requires_stop_before_next_movement(chair):
    releasing = asyncio.Event()
    attempted = []

    async def block_final_release(packet):
        attempted.append(packet)
        if packet[5] == 0 and packet[18] == 0:
            releasing.set()
            await asyncio.Event().wait()
        await chair.write(packet)

    chair.controller._write = block_final_release
    task = asyncio.create_task(chair.controller.adjust_lumbar(True))
    try:
        await asyncio.wait_for(releasing.wait(), 1)
        assert attempted[-1][13:17] == b"\x8f" * 4
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not chair.controller.busy

        chair.controller._write = chair.write
        count = len(chair.writes)
        await chair.controller.step_position(0, 1)
        following = chair.writes[count:]
        assert following[0][13:17] == b"\x80" * 4
        assert following[0][18] == 0
        assert following[0][5] == 0
        assert following[1][5] == 0x11
        assert following[2][13] == 0x82
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_reconnect_never_resumes_or_releases_on_new_connection(chair):
    def reconnect():
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with(heat=False))

    chair.on_sleep = reconnect
    with pytest.raises(ControlError):
        await chair.controller.adjust_lumbar(True)
    assert len(chair.writes) == 2
    assert not chair.controller.busy
    chair.on_sleep = None
    await chair.controller.turn_on()
    assert [packet[5] for packet in chair.writes[2:]] == [0x11]


async def test_stale_mid_burst_does_not_write_old_heat(chair):
    def stop_notifications():
        chair.time += 4

    chair.on_sleep = stop_notifications
    with pytest.raises(ControlError):
        await chair.controller.adjust_lumbar(True)
    assert len(chair.writes) == 2


@pytest.mark.parametrize(
    "index,name,duration,write_times",
    [
        (0, "headrest", 0.5, [0, 0.1, 0.2, 0.3, 0.4]),
        (1, "backrest", 0.25, [0, 0.1, 0.2]),
        (2, "seat", 0.25, [0, 0.1, 0.2]),
        (3, "footrest", 0.5, [0, 0.1, 0.2, 0.3, 0.4]),
    ],
)
@pytest.mark.parametrize("direction", [-1, 1])
async def test_axis_pulse_with_early_progress_keeps_nominal_duration_and_releases(
    chair, index, name, duration, write_times, direction
):
    chair.feedback = False
    chair.activation_feedback = True
    chair.controller.accept_status(status_with(positions=(5, 6, 16, 10)))
    before = chair.controller.status
    reported = False

    def first_progress():
        nonlocal reported
        if not reported:
            positions = list(before.positions)
            positions[index] += direction
            chair.controller.accept_status(status_with(positions=positions))
            reported = True

    chair.on_sleep = first_progress
    await chair.controller.step_position(index, direction)
    assert len(chair.writes) == len(write_times) + 2
    assert chair.time == pytest.approx(duration)
    motors = [0x8F] * 4
    motors[index] = 0x82 if direction == 1 else 0x81
    assert all(p[13:17] == bytes(motors) for p in chair.writes[1:-1])
    assert chair.writes[-1][13:17] == b"\x80" * 4
    expected_positions = list(before.positions)
    expected_positions[index] += direction
    assert chair.controller.status.positions == tuple(expected_positions)
    assert chair.controller.status.heat == before.heat
    assert not chair.controller.busy
    trace = chair.controller.tap_requests[-1]
    assert trace["control"] == name and trace["direction"] == direction
    assert trace["outcome"] == "pulse_completed"
    assert trace["feedback_changed"] is True
    assert trace["status_reports"] == 2  # Activation and one useful position report.
    assert trace["startup_extended"] is False
    assert trace["direction_writes"] == len(write_times)
    assert trace["duration_seconds"] == duration
    writes = trace["direction_write_log"]
    assert [record["seconds"] for record in writes] == write_times
    assert all(record["outcome"] == "write_returned" for record in writes)
    assert [record["command_hex"] for record in writes] == [
        payload.hex().upper() for payload in chair.writes[1:-1]
    ]
    assert trace["stop"]["outcome"] == "write_returned"
    assert trace["stop"]["seconds"] == duration
    assert trace["stop"]["command_hex"] == chair.writes[-1].hex().upper()


async def test_tap_traces_keep_only_four_requests(chair):
    for direction in (1, -1, 1, -1, 1):
        await chair.controller.step_position(0, direction)
    assert len(chair.controller.tap_requests) == 4
    assert [trace["direction"] for trace in chair.controller.tap_requests] == [-1, 1, -1, 1]
    assert all(len(trace["direction_write_log"]) <= 8 for trace in chair.controller.tap_requests)


async def test_missing_heat_feedback_is_not_reported_as_success(chair):
    chair.feedback = False
    with pytest.raises(ControlError, match="did not confirm"):
        await chair.controller.set_heat(False)
    assert len(chair.writes) == 1


@pytest.mark.parametrize("kind", ["heat", "massage"])
@pytest.mark.parametrize("reply", ["none", "wrong", "matching"])
async def test_off_needs_new_matching_feedback_even_if_already_off(chair, kind, reply):
    chair.controller.accept_status(status_with(heat=False, massage=0))
    chair.feedback = False
    if reply != "none":
        # A notification can arrive before the transport write returns.
        chair.on_write = lambda _: chair.controller.accept_status(
            status_with(heat=reply == "wrong", massage=1 if reply == "wrong" else 0)
        )
    action = (
        chair.controller.set_heat(False) if kind == "heat" else chair.controller.set_massage("Off")
    )
    if reply == "matching":
        await action
    else:
        with pytest.raises(ControlError, match="did not confirm"):
            await action
    assert len(chair.writes) == 1
    assert chair.writes[0][5] == 0  # Off neither activates nor retries.


@pytest.mark.parametrize("kind", ["heat", "continuous_heat", "massage"])
async def test_activation_feedback_cannot_confirm_an_already_on_setting(chair, kind):
    chair.controller.accept_status(status_with(heat=True, massage=1))
    if kind == "continuous_heat":
        # A finite massage defers automatic heat renewal, so explicit Heat On
        # must still confirm its own write before establishing that session.
        chair.controller.restore_timer_preferences({"heat": True, "massage": False})
    chair.feedback = False
    chair.activation_feedback = True

    try:
        with pytest.raises(ControlError, match="did not confirm"):
            if kind == "massage":
                await chair.controller.set_massage("Gentle")
            else:
                await chair.controller.set_heat(True)

        assert chair.controller.last_activation["outcome"] == "acknowledged"
        assert len(chair.writes) == 2
        assert chair.writes[0][5] == 0x11
        assert chair.writes[1][5] == 0
        assert chair.writes[1][17] == 1 and chair.writes[1][19] == 1
        assert not chair.controller.heat_renewal_diagnostics["active"]
        assert not chair.controller.massage_renewal_diagnostics["active"]
    finally:
        await chair.controller.cancel_active()


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [
        (True, "7E020100100000000E10000000FFFFFFFF0000010202020000347E"),
        (False, "7E020100100000000E10000000FFFFFFFF0000020202020000357E"),
    ],
)
async def test_heat_matches_physically_confirmed_commands_from_reported_settings(
    chair, enabled, expected
):
    # Exact phone-tested ON/OFF payloads, confirmed without observed side effects.
    # The confirmed option bytes now have names: 60 minutes and three Medium zones.
    chair.controller.accept_status(
        Status(bytes.fromhex("7E010200100000000E10000000000000000000020202020000397E"))
    )
    await chair.controller.set_heat(enabled)
    assert chair.writes[-1] == bytes.fromhex(expected)
    assert len(chair.writes) == (2 if enabled else 1)
    assert chair.controller.last_heat_request["command_hex"] == expected


@pytest.mark.parametrize("enabled", [True, False])
async def test_confirmed_heat_fields_survive_following_controls(chair, enabled):
    chair.controller.accept_status(
        Status(bytes.fromhex("7E010200100000000E10000000000000000000020202020000397E"))
    )
    await chair.controller.set_heat(enabled)
    await chair.controller.adjust_lumbar(True)
    await chair.controller.set_massage("Wave")
    await chair.controller.step_position(0, 1)
    await chair.controller.stop_movement()
    after_heat = chair.writes[1:] if enabled else chair.writes
    assert all(p[8:10] == b"\x0e\x10" for p in after_heat)
    assert all(p[19] == (1 if enabled else 2) for p in after_heat)
    assert all(p[20:23] == b"\x02\x02\x02" for p in after_heat)


async def test_diagnostic_headrest_four_uses_direction_instead_of_target_five(chair):
    chair.controller.accept_status(
        Status(bytes.fromhex("7E010200100000000000000000040000000000020000000000197E"))
    )
    await chair.controller.step_position(0, 1)
    assert chair.writes[1][13:17] == b"\x82\x8f\x8f\x8f"
    assert chair.writes[1][6:13] == b"\x00" * 7
    assert chair.controller.status.positions == (9, 0, 0, 0)


@pytest.mark.parametrize("index", [0, 3])
async def test_half_second_press_continues_after_one_unit_and_preserves_latest_heat(chair, index):
    chair.feedback = False
    chair.activation_feedback = True
    positions = [5, 6, 16, 10]
    chair.controller.accept_status(status_with(positions=positions))

    def progress():
        if len(chair.writes) == 2:
            positions[index] += 1
            chair.controller.accept_status(
                status_with(positions=positions, heat=False, **{"8": 0, "9": 0})
            )

    chair.on_sleep = progress
    await chair.controller.step_position(index, 1)
    assert len(chair.writes) == 7
    assert chair.time == pytest.approx(0.5)
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert all(packet[19] == 2 for packet in chair.writes[2:])
    assert chair.writes[-1][8:10] == b"\x00\x00" and chair.writes[-1][19] == 2


@pytest.mark.parametrize("index", [0, 3])
@pytest.mark.parametrize("direction", [-1, 1])
async def test_half_second_press_stops_early_at_observed_bound(chair, index, direction):
    chair.feedback = False
    chair.activation_feedback = True
    positions = [5, 6, 16, 10]
    chair.controller.accept_status(status_with(positions=positions))
    positions[index] = OBSERVED_BOUNDS[index][1 if direction == 1 else 0]
    chair.on_sleep = lambda: chair.controller.accept_status(status_with(positions=positions))
    await chair.controller.step_position(index, direction)
    assert len(chair.writes) == 3
    assert chair.time == pytest.approx(0.1)
    assert chair.writes[-1][13:17] == b"\x80" * 4


@pytest.mark.parametrize("index", [0, 3])
async def test_movement_cancellation_attempts_stop(chair, index):
    chair.feedback = False
    chair.activation_feedback = True
    chair.controller.accept_status(status_with(positions=(5, 6, 16, 10)))
    task = asyncio.create_task(chair.controller.step_position(index, 1))
    await asyncio.sleep(0)
    await chair.controller.cancel_active()
    assert task.cancelled()
    assert len(chair.writes) == 3
    assert chair.writes[-1][13:17] == b"\x80" * 4
    trace = chair.controller.tap_requests[-1]
    assert trace["outcome"] == "cancelled"
    assert trace["direction_writes"] == 1
    assert trace["stop"]["outcome"] == "write_returned"


async def test_movement_transport_failure_is_not_retried(chair):
    chair.fail_at = 2  # Activation succeeds before the first direction write fails.
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.step_position(0, 1)
    assert len(chair.writes) == 3
    assert chair.writes[-1][13:17] == b"\x80" * 4
    trace = chair.controller.tap_requests[-1]
    assert trace["outcome"] == "failed"
    assert trace["direction_writes"] == 1
    assert trace["direction_write_log"][0]["outcome"] == "failed"
    assert trace["stop"]["outcome"] == "write_returned"


@pytest.mark.parametrize("index", [0, 3])
async def test_normal_movement_release_failure_is_surfaced(chair, index):
    chair.controller.accept_status(status_with(positions=(5, 6, 16, 10)))
    chair.fail_at = 7  # Activation, five timed direction writes, then the final stop.
    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.step_position(index, 1)
    assert len(chair.writes) == 7
    trace = chair.controller.tap_requests[-1]
    assert trace["outcome"] == "failed"
    assert trace["stop"]["outcome"] == "failed"
    assert trace["stop"]["write_attempted"] is True
    assert "not retried" in trace["stop"]["error"]


async def test_movement_reconnect_never_releases_on_new_connection(chair):
    chair.feedback = False
    chair.activation_feedback = True

    def reconnect():
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with())

    chair.on_sleep = reconnect
    with pytest.raises(ControlError, match="Fresh"):
        await chair.controller.step_position(0, 1)
    assert len(chair.writes) == 2
    trace = chair.controller.tap_requests[-1]
    assert trace["outcome"] == "failed"
    assert trace["positions_after"] is None
    assert trace["feedback_changed"] is None
    assert trace["status_reports"] is None
    assert trace["stop"]["outcome"] == "failed"
    assert trace["stop"]["write_attempted"] is False


async def test_reverse_movement_feedback_causes_stop_and_error(chair):
    chair.feedback = False
    chair.activation_feedback = True
    chair.on_sleep = lambda: chair.controller.accept_status(status_with(positions=(3, 6, 16, 28)))
    with pytest.raises(ControlError, match="Unexpected"):
        await chair.controller.step_position(0, 1)
    assert len(chair.writes) == 3
    assert chair.writes[-1][13:17] == b"\x80" * 4


@pytest.mark.parametrize("index", [0, 3])
async def test_wall_clock_pulse_limit_stops_a_blocked_directional_write(chair, monkeypatch, index):
    from custom_components.svago import controller

    monkeypatch.setattr(controller, "MOTOR_PULSE_SECONDS", (0.01, 0.25, 0.25, 0.01))
    chair.controller.accept_status(status_with(positions=(5, 6, 16, 10)))
    writes = []
    cancelled = asyncio.Event()

    async def slow_write(packet):
        writes.append(packet)
        if packet[5] == 0x11:
            await chair.write(packet)
        if packet[13 + index] == 0x82:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    chair.controller._write = slow_write
    with pytest.raises(ControlError, match="write timed out"):
        await chair.controller.step_position(index, 1)
    assert cancelled.is_set()
    assert len(writes) == 3
    assert writes[-1][13:17] == b"\x80" * 4
    trace = chair.controller.tap_requests[-1]
    assert trace["outcome"] == "failed"
    assert trace["direction_write_log"][0]["outcome"] == "timed_out"
    assert trace["stop"]["outcome"] == "write_returned"


@pytest.mark.parametrize("operation", ["heat", "massage", "position", "inflate", "deflate", "stop"])
async def test_every_control_uses_normal_command_field_from_status_11(chair, operation):
    chair.controller.accept_status(status_with(**{"5": 0x11}))

    def repeated_status_11():
        current = chair.controller.status
        chair.controller.accept_status(
            status_with(
                positions=current.positions,
                heat=current.heat,
                massage=current.raw[17],
                **{"5": 0x11},
            )
        )

    chair.on_sleep = repeated_status_11
    actions = {
        "heat": lambda: chair.controller.set_heat(False),
        "massage": lambda: chair.controller.set_massage("Gentle"),
        "position": lambda: chair.controller.step_position(0, 1),
        "inflate": lambda: chair.controller.adjust_lumbar(True),
        "deflate": lambda: chair.controller.adjust_lumbar(False),
        "stop": chair.controller.stop_movement,
    }
    await actions[operation]()
    assert chair.writes
    normal = (
        chair.writes[1:]
        if operation in ("massage", "inflate", "deflate", "position")
        else chair.writes
    )
    assert all(payload[5] == 0 for payload in normal)
    assert all(payload[25] == checksum(payload) for payload in chair.writes)
