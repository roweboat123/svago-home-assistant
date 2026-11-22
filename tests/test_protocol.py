"""Compare construction and reassembly against observed chair bytes."""

import pytest
from conftest import TV_STATUS, status_with

from custom_components.svago.protocol import FrameAssembler, Status, checksum


@pytest.mark.parametrize("split", range(1, 27))
def test_every_notification_split(split):
    parser = FrameAssembler()
    assert parser.feed(TV_STATUS[:split]) == []
    assert parser.feed(TV_STATUS[split:]) == [Status(TV_STATUS)]


def test_stream_recovers_from_garbage_missing_bytes_and_bad_checksum():
    parser = FrameAssembler()
    corrupt = bytearray(TV_STATUS)
    corrupt[25] ^= 1
    stream = b"garbage" + bytes(corrupt) + TV_STATUS[:20] + TV_STATUS + TV_STATUS
    assert parser.feed(stream) == [Status(TV_STATUS), Status(TV_STATUS)]
    parser.reset()
    assert parser.feed(TV_STATUS[20:]) == []
    assert parser.feed(TV_STATUS) == [Status(TV_STATUS)]


def test_payload_delimiter_is_not_a_frame_boundary():
    status = status_with(**{"6": 0x7E})
    assert FrameAssembler().feed(status.raw) == [status]


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (2, "7E02010010000A8C0E100300008F8F8F8F00020102020200000F7E"),
        (1, "7E02010010000A8C0E100300008F8F8F8F00010102020200000E7E"),
        (0, "7E02010010000A8C0E100300008F8F8F8F00000102020200000D7E"),
    ],
)
def test_exact_captured_lumbar_commands(action, expected):
    assert Status(TV_STATUS).command(lumbar=action) == bytes.fromhex(expected)


@pytest.mark.parametrize(
    ("positions", "expected"),
    [
        ((6, 6, 16, 28), "7E02010010000A8C0E100300000606101C0000010202020000097E"),
        ((5, 7, 16, 28), "7E02010010000A8C0E100300000507101C0000010202020000097E"),
        ((5, 6, 17, 28), "7E02010010000A8C0E100300000506111C0000010202020000097E"),
        ((5, 6, 16, 27), "7E02010010000A8C0E100300000506101B0000010202020000077E"),
    ],
)
def test_physically_tested_position_packets(positions, expected):
    assert Status(TV_STATUS).command(motors=positions) == bytes.fromhex(expected)


def test_unknown_current_heat_is_never_replaced_with_a_default():
    status = status_with(**{"19": 0})
    assert not status.usable
    with pytest.raises(ValueError):
        status.command(lumbar=2)


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("direction,checksum_byte", [(0x81, 0x0F), (0x82, 0x10)])
def test_manual_motor_commands_match_memory_three_captures(index, direction, checksum_byte):
    # Captured remote manual packets have identical fields except motor slot.
    status = status_with(**{"10": 0x13})
    motors = [0x8F] * 4
    motors[index] = direction
    packet = status.command(motors=tuple(motors))
    assert packet[:13] == bytes.fromhex("7E02010010000A8C0E10130000")
    assert packet[13:17] == bytes(motors)
    assert packet[17:25] == bytes.fromhex("0000010202020000")
    assert packet[25:] == bytes((checksum_byte, 0x7E))


@pytest.mark.parametrize("heat", [True, False])
def test_explicit_heat_preserves_live_massage_and_unrelated_context(heat):
    before = status_with(
        massage=4,
        **{"8": 0x2A, "9": 0x30, "10": 0x13, "20": 1, "21": 0, "22": 3, "23": 0x12, "24": 0x34},
    )
    packet = before.command(motors=(0xFF,) * 4, heat=heat)
    assert packet[8:10] == b"\x2a\x30"
    assert packet[19] == (1 if heat else 2)
    assert packet[20:23] == b"\x01\x00\x03"
    for index in (3, 4, 6, 7, 10, 11, 12, 17, 23, 24):
        assert packet[index] == before.raw[index]


@pytest.mark.parametrize("heat_fields", [(0, 0, 2), (14, 16, 1), (14, 16, 2)])
@pytest.mark.parametrize("options", [(0, 0, 0), (2, 2, 2), (1, 2, 3)])
def test_other_controls_preserve_all_received_heat_fields(heat_fields, options):
    values = dict(zip(("8", "9", "19"), heat_fields, strict=True))
    values.update(zip(("20", "21", "22"), options, strict=True))
    status = status_with(**values)
    for changes in (
        {"lumbar": 2},
        {"lumbar": 1},
        {"lumbar": 0},
        {"massage": "Gentle", "motors": (255,) * 4},
        {"motors": (130, 143, 143, 143)},
        {"motors": (128,) * 4},
    ):
        packet = status.command(**changes)
        assert (packet[8], packet[9], packet[19]) == heat_fields
        assert packet[20:23] == bytes(options)


def test_unknown_options_survive_and_checksum_wraps():
    status = status_with(**{"6": 0xFD, "23": 0xFC})
    command = status.command(lumbar=2)
    for index in (*range(3, 13), 17, *range(19, 25)):
        assert command[index] == status.raw[index]
    assert command[25] == checksum(command)


@pytest.mark.parametrize("status_field", [0x01, 0x11])
@pytest.mark.parametrize("heat", [True, False])
def test_status_field_five_is_not_echoed_as_a_control_field(status_field, heat):
    status = status_with(heat=heat, **{"5": status_field})
    command = status.command(lumbar=2)
    assert command == status_with(heat=heat).command(lumbar=2)
    assert command[5] == 0
    assert command[19] == (1 if heat else 2)
    assert status.raw[5] == status_field  # Preserve the received evidence unchanged.
