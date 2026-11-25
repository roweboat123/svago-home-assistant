"""Captured native Home travel, stable observations, and interruption handling."""

import asyncio

import pytest
from conftest import FakeChair, status_with

from custom_components.svago import controller as controller_module
from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.protocol import HOME_COMMAND, Status

CAPTURED_HOME = bytes.fromhex("7E0201001001000100000000000000000001000201000100001A7E")
NEUTRAL = (0, 0, 0, 0)
CAPTURED_HOME_REPORTS = (
    (0.000, "7E010200100100010000000000000004060000020100010000237E"),
    (0.442, "7E010200100100010000000000000005060000020100010000247E"),
    (1.132, "7E010200100100010000000000000004060000020100010000237E"),
    (2.033, "7E010200100100010000000000000003060000020100010000227E"),
    (2.331, "7E010200100100010000000000000003070000020100010000237E"),
    (2.844, "7E010200100100010000000000000003060000020100010000227E"),
    (2.933, "7E010200100100010000000000000002060000020100010000217E"),
    (3.534, "7E010200100100010000000000000002050000020100010000207E"),
    (3.940, "7E0102001001000100000000000000010500000201000100001F7E"),
    (4.134, "7E0102001001000100000000000000010400000201000100001E7E"),
    (4.539, "7E0102001001000100000000000000000400000201000100001D7E"),
    (4.732, "7E0102001001000100000000000000000300000201000100001C7E"),
    (5.331, "7E0102001001000100000000000000000200000201000100001B7E"),
    (6.039, "7E0102001001000100000000000000000100000201000100001A7E"),
    (6.745, "7E010200100100010000000000000000000000020100010000197E"),
)


class HomeChair(FakeChair):
    """A native Home write does not fabricate its eventual position feedback."""

    def __init__(self, positions=(0, 0, 4, 6)):
        super().__init__()
        self.controller.accept_status(status_with(positions=positions, heat=True, massage=1))

    async def write(self, payload):
        if payload != CAPTURED_HOME:
            await super().write(payload)
            return
        self.writes.append(payload)
        if self.fail_at == len(self.writes):
            raise OSError("simulated transport failure")
        if self.on_write:
            self.on_write(payload)

    def report(self, positions=NEUTRAL, *, heat=False, massage=0, **changes):
        self.controller.accept_status(
            status_with(positions=positions, heat=heat, massage=massage, **changes)
        )


@pytest.fixture
async def home_chair():
    chair = HomeChair()
    yield chair
    await chair.controller.cancel_active()


def assert_home_once(chair):
    assert chair.writes.count(CAPTURED_HOME) == 1


async def test_home_sends_exact_capture_once_and_observes_staged_return(home_chair):
    chair = home_chair
    chair.controller.accept_status(Status(bytes.fromhex(CAPTURED_HOME_REPORTS[0][1])))
    before = chair.controller.status.raw.hex().upper()

    def report_progress():
        # Repeat actual phone-captured reports between their observed transitions.
        # Both seat and footrest briefly move away from zero during native travel.
        frame = next(
            frame for second, frame in reversed(CAPTURED_HOME_REPORTS) if second <= chair.time
        )
        chair.controller.accept_status(Status(bytes.fromhex(frame)))

    chair.on_sleep = report_progress
    await chair.controller.return_home()

    assert HOME_COMMAND == CAPTURED_HOME
    assert chair.writes == [CAPTURED_HOME]
    assert 8.745 <= chair.time < 9
    assert chair.controller.last_activation is None
    assert not chair.controller.busy
    trace = chair.controller.last_home_request
    assert trace["outcome"] == "reported_neutral"
    assert trace["command_hex"] == CAPTURED_HOME.hex().upper()
    assert trace["status_before_hex"] == before
    assert trace["status_after_hex"] == chair.controller.status.raw.hex().upper()
    assert trace["positions_before"] == [0, 0, 4, 6]
    assert trace["positions_after"] == [0, 0, 0, 0]
    assert trace["duration_seconds"] >= 8.745
    assert trace["neutral_stable_seconds"] >= 2
    assert trace["status_reports"] >= 2
    assert 1 <= len(trace["reports"]) <= 64
    await chair.controller.turn_on()
    assert len(chair.writes) == 2  # A completed Home leaves no old stop to replay.
    assert chair.writes[-1][5] == 0x11


async def test_already_neutral_needs_new_reports_spanning_two_seconds(home_chair):
    chair = home_chair
    chair.report()
    chair.on_write = lambda packet: chair.report() if packet == CAPTURED_HOME else None
    chair.on_sleep = chair.report

    await chair.controller.return_home()

    assert chair.writes == [CAPTURED_HOME]
    assert 2 <= chair.time < 2.2
    assert chair.controller.last_home_request["status_reports"] >= 2


@pytest.mark.parametrize("reports", ["none", "one", "two_too_close"])
async def test_cached_or_insufficient_neutral_feedback_cannot_complete(home_chair, reports):
    chair = home_chair
    chair.report()

    def reply(packet):
        if packet != CAPTURED_HOME or reports == "none":
            return
        chair.report()
        if reports == "two_too_close":
            chair.report()

    chair.on_write = reply
    with pytest.raises(ControlError):
        await chair.controller.return_home()

    assert chair.writes == [CAPTURED_HOME]  # Stale feedback cannot build a stop packet.
    assert chair.controller.last_home_request["outcome"] == "failed"
    assert chair.time >= 3


async def test_neutral_reports_while_home_write_is_pending_cannot_confirm_completion(home_chair):
    chair = home_chair

    async def delayed_home(packet):
        await chair.write(packet)
        if packet == CAPTURED_HOME:
            for second in (0.0, 1.0, 2.1):
                chair.time = second
                chair.report()
                await asyncio.sleep(0)

    chair.controller._write = delayed_home
    with pytest.raises(ControlError):
        await chair.controller.return_home()

    assert chair.writes == [CAPTURED_HOME]
    assert chair.time >= 5.1
    assert chair.controller.last_home_request["outcome"] == "failed"


@pytest.mark.parametrize("wrong", ["heat_on", "massage_on"])
async def test_zero_positions_need_heat_and_massage_off_for_full_stable_window(home_chair, wrong):
    chair = home_chair

    def report():
        chair.report(
            heat=wrong == "heat_on" and chair.time < 3,
            massage=1 if wrong == "massage_on" and chair.time < 3 else 0,
        )

    chair.on_write = lambda packet: report() if packet == CAPTURED_HOME else None
    chair.on_sleep = report
    await chair.controller.return_home()

    assert chair.writes == [CAPTURED_HOME]
    assert 5 <= chair.time < 5.3
    assert chair.controller.status.heat is False
    assert chair.controller.status.massage == "Off"


@pytest.mark.parametrize("transient", ["positions", "heat"])
async def test_transient_nonneutral_report_between_polls_restarts_stability(home_chair, transient):
    chair = home_chair
    transient_sent = False
    chair.on_write = lambda packet: chair.report() if packet == CAPTURED_HOME else None

    def report():
        nonlocal transient_sent
        if chair.time >= 1 and not transient_sent:
            transient_sent = True
            if transient == "positions":
                chair.report((0, 0, 1, 0))
            else:
                chair.report(heat=True)
                chair.report()
                chair.report(heat=True)
        chair.report()

    chair.on_sleep = report
    await chair.controller.return_home()

    assert transient_sent
    assert 3 <= chair.time < 3.4
    assert chair.writes == [CAPTURED_HOME]


@pytest.mark.parametrize("problem", ["stale", "disconnected", "missing", "unknown", "bounds"])
async def test_unusable_initial_status_prevents_home_dispatch(home_chair, problem):
    chair = home_chair
    if problem == "stale":
        chair.time = 4
    elif problem == "disconnected":
        chair.controller.set_connected(False)
    elif problem == "missing":
        chair.controller.status = None
    elif problem == "unknown":
        chair.report(**{"19": 0})
    else:
        chair.report((0, 0, 41, 0))

    with pytest.raises(ControlError):
        await chair.controller.return_home()

    assert chair.writes == []


@pytest.mark.parametrize("problem", ["missing", "unknown", "bounds", "transient_bounds"])
async def test_bad_feedback_does_not_confirm_home(home_chair, problem):
    chair = home_chair

    def report(packet):
        if packet != CAPTURED_HOME:
            return
        if problem == "missing":
            chair.controller.status = None
        elif problem == "unknown":
            chair.report(**{"19": 0})
        else:
            chair.report((0, 0, 41, 0))
            if problem == "transient_bounds":
                chair.report()

    chair.on_write = report
    with pytest.raises(ControlError):
        await chair.controller.return_home()

    assert_home_once(chair)
    assert chair.controller.last_home_request["outcome"] == "failed"
    if problem in ("missing", "unknown"):
        assert chair.writes == [CAPTURED_HOME]
    else:
        assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_fresh_nonzero_feedback_runs_to_total_deadline_without_stall_guard(home_chair):
    chair = home_chair
    chair.on_sleep = lambda: chair.report((0, 0, 1 + int(chair.time * 10) % 2, 0))

    with pytest.raises(ControlError):
        await chair.controller.return_home()

    assert 60 <= chair.time < 60.2
    assert len(chair.writes) == 2
    assert_home_once(chair)
    assert chair.writes[-1][13:17] == b"\x80" * 4
    trace = chair.controller.last_home_request
    assert trace["outcome"] == "failed"
    assert trace["duration_seconds"] >= 60
    assert trace["status_reports"] >= 500
    assert len(trace["reports"]) <= 64


async def test_wall_deadline_interrupts_blocked_transport_and_attempts_stop(
    home_chair, monkeypatch
):
    chair = home_chair
    monkeypatch.setattr(controller_module, "HOME_MAX_SECONDS", 0.02)
    attempted = []

    async def blocked(packet):
        attempted.append(packet)
        if packet == CAPTURED_HOME:
            await asyncio.Event().wait()
        await chair.write(packet)

    chair.controller._write = blocked
    with pytest.raises(ControlError):
        await asyncio.wait_for(chair.controller.return_home(), 1)

    assert attempted[0] == CAPTURED_HOME
    assert attempted.count(CAPTURED_HOME) == 1
    assert attempted[-1][13:17] == b"\x80" * 4
    assert chair.controller.last_home_request["outcome"] == "failed"


async def test_failed_home_transport_is_not_replayed_and_attempts_stop(home_chair):
    chair = home_chair
    chair.fail_at = 1

    with pytest.raises(ControlError, match="not retried"):
        await chair.controller.return_home()

    assert len(chair.writes) == 2
    assert_home_once(chair)
    assert chair.writes[-1][13:17] == b"\x80" * 4
    count = len(chair.writes)
    chair.fail_at = None
    chair.report()
    chair.on_sleep = chair.report
    await chair.controller.turn_on()
    following = chair.writes[count:]
    assert len(following) == 2
    assert following[0][13:17] == b"\x80" * 4
    assert following[1][5] == 0x11
    assert_home_once(chair)


async def test_cancellation_stops_home_with_current_fresh_settings(home_chair):
    chair = home_chair
    waiting = asyncio.Event()

    async def wait(_seconds):
        waiting.set()
        await asyncio.Event().wait()

    chair.controller._sleep = wait
    task = asyncio.create_task(chair.controller.return_home())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        chair.report((0, 0, 4, 5), heat=False, massage=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_home_once(chair)
        assert len(chair.writes) == 2
        assert chair.writes[-1][13:17] == b"\x80" * 4
        assert chair.writes[-1][17] == 3 and chair.writes[-1][19] == 2
        assert chair.controller.last_home_request["outcome"] == "cancelled"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_explicit_stop_interrupts_pending_home(home_chair):
    chair = home_chair
    waiting = asyncio.Event()

    async def wait(_seconds):
        waiting.set()
        await asyncio.Event().wait()

    chair.controller._sleep = wait
    task = asyncio.create_task(chair.controller.return_home())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        chair.controller._sleep = chair.sleep
        chair.on_sleep = lambda: chair.controller.accept_status(chair.controller.status)
        await chair.controller.stop_movement()
        with pytest.raises(CommandSuperseded):
            await task
        assert_home_once(chair)
        following = chair.writes[1:]
        assert following[0][13:17] == b"\x80" * 4
        assert all(packet[13:17] == b"\x80" * 4 for packet in following)
        assert chair.controller.last_home_request["outcome"] == "replaced_by_new_request"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_home_hands_off_existing_lumbar_before_native_dispatch(home_chair):
    chair = home_chair
    old = asyncio.create_task(chair.controller.adjust_lumbar(True))
    try:
        await asyncio.sleep(0)
        chair.on_sleep = lambda: chair.report() if CAPTURED_HOME in chair.writes else None
        await chair.controller.return_home()
        with pytest.raises(CommandSuperseded):
            await old
        home_index = chair.writes.index(CAPTURED_HOME)
        assert chair.writes[home_index - 1][13:17] == b"\x80" * 4
        assert chair.writes[home_index:] == [CAPTURED_HOME]
        assert_home_once(chair)
    finally:
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.parametrize("stage", ["write", "travel"])
async def test_reconnection_does_not_replay_home_or_stop_on_new_link(home_chair, stage):
    chair = home_chair

    def reconnect():
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        chair.report()

    if stage == "write":
        chair.on_write = lambda packet: reconnect() if packet == CAPTURED_HOME else None
    else:
        chair.on_sleep = reconnect
    with pytest.raises(ControlError):
        await chair.controller.return_home()

    assert chair.writes == [CAPTURED_HOME]
    chair.on_write = None
    chair.on_sleep = chair.report
    await chair.controller.turn_on()
    assert len(chair.writes) == 2
    assert chair.writes[-1][5] == 0x11
    assert_home_once(chair)


async def test_home_retires_continuous_workers_at_dispatch_preserving_choices(home_chair):
    chair = home_chair
    await chair.controller.set_massage_continuous()
    await chair.controller.set_heat_continuous()
    assert chair.controller.massage_renewal_diagnostics["active"]
    assert chair.controller.heat_renewal_diagnostics["active"]
    workers = (chair.controller._massage_renewal_task, chair.controller._heat_renewal_task)
    preferences = chair.controller.timer_preferences
    chair.writes.clear()

    def inspect_dispatch(packet):
        if packet != CAPTURED_HOME:
            return
        assert not chair.controller.massage_renewal_diagnostics["active"]
        assert not chair.controller.heat_renewal_diagnostics["active"]
        assert chair.controller.timer_preferences == preferences
        chair.report()

    chair.on_write = inspect_dispatch
    chair.on_sleep = chair.report
    await chair.controller.return_home()
    await asyncio.gather(*workers, return_exceptions=True)

    assert all(worker.done() for worker in workers)
    assert chair.controller.timer_preferences == {"massage": True, "heat": True}
    assert chair.writes == [CAPTURED_HOME]
    assert not chair.controller.massage_renewal_diagnostics["active"]
    assert not chair.controller.heat_renewal_diagnostics["active"]
