"""Labeled capture fixtures and preservation rules for mapped chair settings."""

import pytest
from conftest import status_with

from custom_components.svago.protocol import HEAT_LEVELS, HEAT_ZONES, Status, checksum


@pytest.mark.parametrize("zone,index", tuple(zip(HEAT_ZONES, (20, 21, 22), strict=True)))
@pytest.mark.parametrize("value,level", tuple(enumerate(HEAT_LEVELS)))
def test_heat_level_readback_and_only_target_zone_changes(zone, index, value, level):
    status = status_with(**{str(index): value, "18": 2})
    assert status.heat_level(zone) == level
    before = status_with(heat=False, massage=4, **{"20": 1, "21": 0, "22": 3, "18": 2})
    command = before.setting_command(zone=zone, level=level)
    assert command[index] == value
    assert command[13:17] == before.raw[13:17]
    assert command[17] == 4 and command[18] == 0 and command[19] == 0x8F
    for field in set(range(27)) - {1, 2, 18, 19, index, 25}:
        assert command[field] == before.raw[field]
    assert command[25] == checksum(command)


@pytest.mark.parametrize("zone,index", tuple(zip(HEAT_ZONES, (20, 21, 22), strict=True)))
def test_unknown_heat_levels_are_not_displayed_as_off(zone, index):
    assert status_with(**{str(index): 0x8F}).heat_level(zone) is None


@pytest.mark.parametrize(
    "kind,minutes,seconds_hex",
    [
        ("massage", 15, "0384"),
        ("massage", 30, "0708"),
        ("massage", 45, "0A8C"),
        ("heat", 60, "0E10"),
        ("heat", 120, "1C20"),
        ("heat", 180, "2A30"),
    ],
)
def test_labeled_timer_encodings_and_unrelated_field_preservation(kind, minutes, seconds_hex):
    offset = 8 if kind == "heat" else 6
    encoded = bytes.fromhex(seconds_hex)
    status = status_with(**{str(offset): encoded[0], str(offset + 1): encoded[1]})
    assert status.timer_minutes(kind) == minutes
    before = status_with(massage=3, **{"20": 3, "21": 0, "22": 1, "18": 2})
    command = before.setting_command(timer=kind, minutes=minutes)
    assert command[offset : offset + 2] == encoded
    for field in set(range(27)) - {1, 2, 18, 19, offset, offset + 1, 25}:
        assert command[field] == before.raw[field]
    assert command[18:20] == b"\x00\x8f"
    assert command[25] == checksum(command)


@pytest.mark.parametrize("kind", ["heat", "massage"])
@pytest.mark.parametrize("raw", [b"\x00\x00", b"\x00\x01", b"\xff\xff", b"\x00\x3c"])
def test_unconfirmed_timer_values_are_unknown(kind, raw):
    offset = 8 if kind == "heat" else 6
    status = status_with(**{str(offset): raw[0], str(offset + 1): raw[1]})
    assert status.timer_minutes(kind) is None


@pytest.mark.parametrize(
    "zone,status_hex,expected",
    [
        (
            "seat",
            "7E0102001011000000008F0000000000000000020100010000B77E",
            (
                "7E0201001011000000008F00000000000000008F0100010000447E",
                "7E0201001011000000008F00000000000000008F0101010000457E",
                "7E0201001011000000008F00000000000000008F0102010000467E",
                "7E0201001011000000008F00000000000000008F0103010000477E",
            ),
        ),
        (
            "footrest",
            "7E010200100100012A308F0000000000000000020100010000027E",
            (
                "7E020100100100012A308F00000000000000008F01000000008E7E",
                "7E020100100100012A308F00000000000000008F01000100008F7E",
                "7E020100100100012A308F00000000000000008F0100020000907E",
                "7E020100100100012A308F00000000000000008F0100030000917E",
            ),
        ),
    ],
)
def test_exact_seat_and_footrest_capture_commands(zone, status_hex, expected):
    status = Status(bytes.fromhex(status_hex))
    for level, captured in zip(HEAT_LEVELS, expected, strict=True):
        assert status.setting_command(zone=zone, level=level) == bytes.fromhex(captured)


@pytest.mark.parametrize(
    "minutes,expected",
    [
        (120, "7E020100100100011C208F00000000000000008F0100010000717E"),
        (180, "7E020100100100012A308F00000000000000008F01000100008F7E"),
    ],
)
def test_exact_heat_timer_capture_commands(minutes, expected):
    status = Status(bytes.fromhex("7E010200100100010E108F0000000000000000020100010000C67E"))
    assert status.setting_command(timer="heat", minutes=minutes) == bytes.fromhex(expected)


def test_massage_timer_adaptation_keeps_numeric_motor_positions():
    status = Status(bytes.fromhex("7E010200100003840E108F000006021E0A00000101000100007A7E"))
    captured = bytes.fromhex("7E020100100007080E108F000006FFFFFF00008F0100010000637E")
    command = status.setting_command(timer="massage", minutes=30)
    # The remote used FF for the last three motors. Holding the reported numeric
    # positions is an adaptation, not an exact replay verified on the real chair.
    assert command[13:17] == bytes((6, 2, 30, 10))
    assert [i for i in range(27) if command[i] != captured[i]] == [14, 15, 16, 25]


def test_backrest_uses_captured_level_mapping_with_heat_preservation_adaptation():
    status = Status(bytes.fromhex("7E0102001011000000000000000000000000000202020200002C7E"))
    captured = bytes.fromhex("7E0201001011000000000000000000000000000203020200002D7E")
    command = status.setting_command(zone="backrest", level="High")
    # This earlier capture copied explicit heat-Off=02; the newer seat/footrest
    # captures establish 8F as preserve. Applying that to backrest is adaptation.
    assert [i for i in range(27) if command[i] != captured[i]] == [19, 25]


@pytest.mark.parametrize(
    "setting",
    [
        {},
        {"zone": "seat"},
        {"level": "High"},
        {"zone": "lumbar", "level": "High"},
        {"zone": "seat", "level": "Very high"},
        {"timer": "heat"},
        {"minutes": 60},
        {"timer": "unknown", "minutes": 60},
        {"timer": "heat", "minutes": 240},
        {"timer": "heat", "minutes": 0},
        {"timer": "heat", "minutes": 60.0},
        {"timer": "massage", "minutes": True},
        {"zone": "seat", "level": "High", "timer": "heat", "minutes": 60},
    ],
)
def test_invalid_or_ambiguous_settings_rejected(setting):
    with pytest.raises(ValueError):
        status_with().setting_command(**setting)


@pytest.mark.parametrize("changes", [{"19": 0}, {"17": 0x8F}, {"13": 0x82}])
def test_setting_requires_usable_status_and_numeric_positions(changes):
    with pytest.raises(ValueError):
        status_with(**changes).setting_command(zone="seat", level="High")


@pytest.mark.parametrize("heat", [True, False])
def test_master_heat_preserves_explicit_all_zones_off(heat):
    status = status_with(**{"20": 0, "21": 0, "22": 0})
    assert status.command(heat=heat)[20:23] == b"\x00\x00\x00"


@pytest.mark.parametrize("heat", [True, False])
def test_zero_timer_default_only_on_explicit_heat_on(heat):
    status = status_with(**{"8": 0, "9": 0, "20": 1, "21": 0, "22": 3})
    command = status.command(heat=heat)
    assert command[8:10] == (b"\x0e\x10" if heat else b"\x00\x00")
    assert command[20:23] == b"\x01\x00\x03"


@pytest.mark.parametrize(
    "heat,expected",
    [
        (True, "7E020100100000000E10000000FFFFFFFF0000010202020000347E"),
        (False, "7E020100100000000E10000000FFFFFFFF0000020202020000357E"),
    ],
)
def test_physically_verified_heat_messages_from_matching_live_settings(heat, expected):
    status = status_with(
        heat=False,
        massage=0,
        positions=(0, 0, 0, 0),
        **{"6": 0, "7": 0, "10": 0},
    )
    assert status.command(motors=(0xFF,) * 4, heat=heat) == bytes.fromhex(expected)
