"""Captured preset positions remain usable by manual sliders and movement controls."""

from types import SimpleNamespace

import pytest
from conftest import status_with

from custom_components.svago.number import PositionNumber
from custom_components.svago.protocol import POSITION_NAMES, Status

CAPTURED_POSITIONS = {
    "zgr": (
        "7E010200100003842A30010000000B28160200010100010000437E",
        (0, 11, 40, 22),
    ),
    "lay_flat": (
        "7E010200100003842A3002000000161A1C0200010100010000477E",
        (0, 22, 26, 28),
    ),
    "memory_2": (
        "7E010200100003842A301200000606261100000101000100004C7E",
        (6, 6, 38, 17),
    ),
}


def position_number(chair, index):
    return PositionNumber(
        SimpleNamespace(controller=chair.controller, address="test"), index, POSITION_NAMES[index]
    )


@pytest.mark.parametrize("capture", CAPTURED_POSITIONS)
async def test_captured_high_positions_are_visible_in_native_sliders(chair, capture):
    raw, expected = CAPTURED_POSITIONS[capture]
    chair.controller.accept_status(Status(bytes.fromhex(raw)))
    numbers = [position_number(chair, index) for index in range(4)]

    assert tuple(number.native_value for number in numbers) == expected
    assert tuple(number.native_max_value for number in numbers) == (17, 22, 40, 28)
    assert all(number.available for number in numbers)


@pytest.mark.parametrize(
    "capture,index", [("zgr", 1), ("zgr", 2), ("lay_flat", 1), ("memory_2", 2)]
)
async def test_slider_can_decrease_from_captured_high_position(chair, capture, index):
    raw, before = CAPTURED_POSITIONS[capture]
    chair.controller.accept_status(Status(bytes.fromhex(raw)))
    number = position_number(chair, index)
    target = before[index] - 3

    await number.async_set_native_value(target)

    expected = list(before)
    expected[index] = target
    assert chair.controller.status.positions == tuple(expected)
    assert number.native_value == target
    assert all(packet[13 + index] == 0x81 for packet in chair.writes[:-1] if packet[5] != 0x11)
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert chair.controller.position_requests[-1]["outcome"] == "target_reached"


@pytest.mark.parametrize("index,target", [(1, 22), (2, 40)])
async def test_slider_accepts_extended_target_with_live_feedback(chair, index, target):
    before = (0, 7, 31, 22)
    chair.controller.accept_status(status_with(positions=before))
    number = position_number(chair, index)

    await number.async_set_native_value(target)

    expected = list(before)
    expected[index] = target
    assert chair.controller.status.positions == tuple(expected)
    assert number.native_value == target
    assert all(packet[13 + index] == 0x82 for packet in chair.writes[:-1] if packet[5] != 0x11)
    assert chair.writes[-1][13:17] == b"\x80" * 4


@pytest.mark.parametrize("index", [1, 2])
async def test_directional_decrease_works_from_extended_upper_bound(chair, index):
    before = (0, 22, 40, 28)
    chair.controller.accept_status(status_with(positions=before))

    await chair.controller.step_position(index, -1)

    assert chair.controller.status.positions[index] < before[index]
    assert all(packet[13 + index] == 0x81 for packet in chair.writes[:-1] if packet[5] != 0x11)
    assert chair.writes[-1][13:17] == b"\x80" * 4


async def test_linked_seat_progress_above_31_prevents_false_backrest_stall(chair):
    # Synthetic linked movement using the newly observed seat range. The
    # backrest stays stationary for longer than its two-second stall window.
    chair.feedback = False
    chair.activation_feedback = True
    chair.controller.accept_status(status_with(positions=(0, 6, 32, 22)))

    def report():
        seat = 34 if chair.time >= 2 else 33 if chair.time >= 1 else 32
        backrest = 7 if chair.time >= 3 else 6
        chair.controller.accept_status(status_with(positions=(0, backrest, seat, 22)))

    chair.on_sleep = report
    await chair.controller.set_position(1, 7)

    assert 3 <= chair.time < 3.2
    assert chair.controller.status.positions == (0, 7, 34, 22)
    assert all(
        packet[13:17] == b"\x8f\x82\x8f\x8f" for packet in chair.writes[:-1] if packet[5] != 0x11
    )
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert chair.controller.position_requests[-1]["outcome"] == "target_reached"


async def test_full_seat_range_allows_slow_continuous_progress_beyond_30_seconds(chair):
    chair.feedback = False
    chair.activation_feedback = True
    chair.controller.accept_status(status_with(positions=(0, 6, 0, 22)))

    def report():
        # Simulate one reported unit per second, with fresh notifications in
        # between. A valid full-range move exceeds the previous 30-second cap.
        seat = min(40, int(chair.time))
        chair.controller.accept_status(status_with(positions=(0, 6, seat, 22)))

    chair.on_sleep = report
    await chair.controller.set_position(2, 40)

    assert 40 <= chair.time < 40.2
    assert chair.controller.status.positions == (0, 6, 40, 22)
    assert all(
        packet[13:17] == b"\x8f\x8f\x82\x8f" for packet in chair.writes[:-1] if packet[5] != 0x11
    )
    assert chair.writes[-1][13:17] == b"\x80" * 4
    assert chair.controller.position_requests[-1]["outcome"] == "target_reached"


@pytest.mark.parametrize("index,value", [(1, 23), (2, 41)])
async def test_native_slider_still_hides_feedback_beyond_extended_bounds(chair, index, value):
    positions = [0, 22, 40, 28]
    positions[index] = value
    chair.controller.accept_status(status_with(positions=positions))

    assert position_number(chair, index).native_value is None
