"""Native-preset packets and observed ranges grounded in the labeled captures."""

import pytest

from custom_components.svago.protocol import (
    OBSERVED_BOUNDS,
    PRESET_COMMANDS,
    checksum,
    preset_command,
)


@pytest.mark.parametrize(
    "name,selector,captured_hex",
    [
        (
            "zgr",
            0x01,
            "7E020100100003842A300100008F8F8F8F0200010100010000367E",
        ),
        (
            "lay_flat",
            0x02,
            "7E020100100003842A300200008F8F8F8F0200010100010000377E",
        ),
    ],
)
def test_exact_native_invocations_include_captured_options(name, selector, captured_hex):
    packet = preset_command(name)
    assert packet == bytes.fromhex(captured_hex)
    assert packet == PRESET_COMMANDS[name]
    assert len(packet) == 27 and packet[-1] == 0x7E
    assert packet[25] == checksum(packet)
    assert packet[10] == selector
    assert packet[13:17] == b"\x8f" * 4  # No replacement numeric end positions.
    assert int.from_bytes(packet[6:8], "big") == 15 * 60
    assert int.from_bytes(packet[8:10], "big") == 180 * 60
    assert packet[17:23] == bytes((2, 0, 1, 1, 0, 1))


def test_native_presets_differ_only_in_selector_and_checksum():
    zgr = preset_command("zgr")
    lay_flat = preset_command("lay_flat")
    assert [index for index in range(27) if zgr[index] != lay_flat[index]] == [10, 25]


@pytest.mark.parametrize("name", ["", "Memory 1", "ZGR", "flat", None, 1, []])
def test_unknown_native_preset_is_not_guessed(name):
    with pytest.raises(ValueError, match="Unknown chair preset"):
        preset_command(name)


def test_observed_bounds_include_native_preset_position_feedback():
    assert OBSERVED_BOUNDS == ((0, 17), (0, 22), (0, 40), (0, 28))
    for positions in ((0, 11, 40, 22), (0, 22, 26, 28)):
        assert all(
            lower <= value <= upper
            for value, (lower, upper) in zip(positions, OBSERVED_BOUNDS, strict=True)
        )
