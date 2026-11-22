"""Regression sequences from chair testing, with labeled simulated endings."""

import pytest
from conftest import status_with

from custom_components.svago.controller import ControlError
from custom_components.svago.protocol import Status

BACKREST_SIX = (
    "7E010200100000000E100000000006000000000202020200003F7E",
    "7E010200100000000E10000000000601000000020202020000407E",
    "7E010200100000000E10000000000602000000020202020000417E",
)


def install_linked_sequence(chair, *, complete):
    chair.feedback = False
    chair.activation_feedback = True
    chair.controller.accept_status(Status(bytes.fromhex(BACKREST_SIX[0])))

    def report():
        # Actual 0.1.6 diagnostic: backrest stays 6 while seat reaches 1 and 2.
        seat = 2 if chair.time >= 2.209 else 1 if chair.time >= 1.309 else 0
        state = Status(bytes.fromhex(BACKREST_SIX[seat]))
        if complete and chair.time >= 3:
            # Synthetic continuation: the diagnostic ended before this happened.
            state = status_with(positions=(0, 7, 2, 0), heat=False)
        chair.controller.accept_status(state)

    chair.on_sleep = report


async def test_recorded_linked_seat_progress_allows_backrest_to_continue(chair):
    install_linked_sequence(chair, complete=True)
    await chair.controller.set_position(1, 7)
    assert 3 <= chair.time < 3.2
    assert all(p[13:17] == b"\x8f\x82\x8f\x8f" for p in chair.writes[:-1] if p[5] != 0x11)
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert chair.controller.status.positions == (0, 7, 2, 0)
    trace = chair.controller.position_requests[-1]
    assert trace["outcome"] == "target_reached"
    assert [r["positions"] for r in trace["reports"]] == [
        [0, 6, 0, 0],
        [0, 6, 1, 0],
        [0, 6, 2, 0],
        [0, 7, 2, 0],
    ]


async def test_linked_adjustment_still_stops_if_backrest_never_reaches_target(chair):
    install_linked_sequence(chair, complete=False)
    with pytest.raises(ControlError, match="Position stopped changing"):
        await chair.controller.set_position(1, 7)
    assert 4.2 <= chair.time < 4.5
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert chair.controller.position_requests[-1]["outcome"] == "failed"


async def test_unrelated_headrest_changes_do_not_extend_backrest_stall(chair):
    chair.feedback = False
    chair.activation_feedback = True
    chair.controller.accept_status(status_with(positions=(0, 6, 0, 0)))
    chair.on_sleep = lambda: chair.controller.accept_status(
        status_with(positions=(int(chair.time * 5) % 17, 6, 0, 0))
    )
    with pytest.raises(ControlError, match="Position stopped changing"):
        await chair.controller.set_position(1, 7)
    assert 2 <= chair.time < 2.2


async def test_returning_backrest_does_not_reset_seat(chair):
    chair.controller.accept_status(status_with(positions=(0, 6, 2, 0)))
    await chair.controller.set_position(1, 0)
    assert chair.controller.status.positions == (0, 0, 2, 0)
    assert all(p[13:17] == b"\x8f\x81\x8f\x8f" for p in chair.writes[:-1] if p[5] != 0x11)


@pytest.mark.parametrize(
    "index,start,first_at,first,finish_at,target",
    [(0, 6, 0.531, 7, 0.830, 5), (2, 17, 0.423, 18, 1.518, 16)],
)
async def test_captured_one_unit_startup_excursion(
    chair, index, start, first_at, first, finish_at, target
):
    chair.feedback = False
    chair.activation_feedback = True
    positions = [5, 6, 16, 28]
    positions[index] = start
    chair.controller.accept_status(status_with(positions=positions))

    def report():
        positions[index] = (
            target if chair.time >= finish_at else first if chair.time >= first_at else start
        )
        chair.controller.accept_status(status_with(positions=positions))

    chair.on_sleep = report
    await chair.controller.set_position(index, target)
    assert chair.time >= finish_at
    assert chair.controller.status.positions[index] == target
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_persistent_one_unit_reverse_feedback_is_not_accepted(chair):
    chair.feedback = False
    chair.activation_feedback = True
    chair.on_sleep = lambda: chair.controller.accept_status(status_with(positions=(4, 6, 16, 28)))
    with pytest.raises(ControlError, match="Unexpected"):
        await chair.controller.set_position(0, 8)
    assert 2 <= chair.time < 2.2
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_reverse_after_forward_progress_stops_immediately(chair):
    chair.feedback = False
    chair.activation_feedback = True
    chair.on_sleep = lambda: chair.controller.accept_status(
        status_with(positions=(6 if chair.time < 0.2 else 5, 6, 16, 28))
    )
    with pytest.raises(ControlError, match="Unexpected"):
        await chair.controller.set_position(0, 8)
    assert chair.time == pytest.approx(0.2)
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_linked_progress_cannot_extend_total_time_limit(chair, monkeypatch):
    from custom_components.svago import controller

    monkeypatch.setattr(controller, "SLIDER_MAX_SECONDS", 4)
    chair.feedback = False
    chair.activation_feedback = True
    chair.controller.accept_status(status_with(positions=(0, 6, 0, 0)))
    chair.on_sleep = lambda: chair.controller.accept_status(
        status_with(positions=(0, 6, int(chair.time) % 2, 0))
    )
    with pytest.raises(ControlError, match="time limit"):
        await chair.controller.set_position(1, 7)
    assert 4 <= chair.time < 4.2
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_recent_position_requests_have_bounded_history(chair):
    for target in [4, 5, 6, 7, 8]:
        await chair.controller.set_position(0, target)
    assert [t["target"] for t in chair.controller.position_requests] == [5, 6, 7, 8]
