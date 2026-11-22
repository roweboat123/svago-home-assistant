"""HA memories retain mapped settings and compose recall from live context."""

from dataclasses import FrozenInstanceError, replace

import pytest
from conftest import status_with

from custom_components.svago.memory_protocol import MemoryProfile
from custom_components.svago.protocol import OBSERVED_BOUNDS, Status, checksum


@pytest.mark.parametrize(
    "slot,captured",
    [
        (1, "7E020100100003842A30110000050000040000010100010000117E"),
        (2, "7E020100100003842A301200000606261100000101000100004C7E"),
        (3, "7E020100100003842A3013000006021E0A00000101000100003A7E"),
    ],
)
def test_recall_format_matches_captured_memory_frames(slot, captured):
    # Captured full-state writes establish the format, not persistent chair slots.
    raw = bytearray.fromhex(captured)
    raw[1:3] = b"\x01\x02"
    current = Status(bytes(raw))
    profile = MemoryProfile.from_status(current)
    assert profile.command(current, slot) == bytes.fromhex(captured)


def test_saved_known_fields_and_live_unknown_fields_are_composed_separately():
    saved = status_with(
        positions=(17, 22, 40, 28),
        massage=5,
        heat=True,
        **{
            "5": 0x11,
            "8": 0x2A,
            "9": 0x30,
            "18": 2,
            "20": 3,
            "21": 0,
            "22": 1,
            "11": 0x40,
            "12": 0x50,
            "23": 0x60,
            "24": 0x70,
        },
    )
    current = status_with(
        positions=(1, 2, 3, 4),
        massage=0,
        heat=False,
        **{"5": 0x01, "10": 0x02, "18": 1, "11": 0x12, "12": 0x23, "23": 0x34, "24": 0x45},
    )
    profile = MemoryProfile.from_status(saved)
    command = profile.command(current, 2)
    assert command[:6] == bytes.fromhex("7E0201001000")
    assert command[6:10] == saved.raw[6:10]
    assert command[10] == 0x12
    assert command[13:17] == saved.raw[13:17]
    assert command[17] == 5 and command[18] == 0 and command[19] == 1
    assert command[20:23] == bytes((3, 0, 1))
    assert all(command[i] == current.raw[i] for i in (11, 12, 23, 24))
    assert command[25] == checksum(command)
    assert set(profile.to_dict()) == {
        "positions",
        "massage",
        "heat",
        "heat_levels",
        "massage_timer_seconds",
        "heat_timer_seconds",
        "massage_continuous",
        "heat_continuous",
    }


def test_profile_is_immutable_and_serialized_data_is_detached():
    profile = MemoryProfile.from_status(status_with())
    data = profile.to_dict()
    restored = MemoryProfile.from_dict(data)
    data["positions"][0] = 0
    data["heat_levels"][0] = "Off"
    assert restored == profile
    with pytest.raises(FrozenInstanceError):
        profile.heat = False
    with pytest.raises(ValueError):
        replace(profile, positions=[0, 0, 0, 0])


def test_setting_match_does_not_claim_motion_finished():
    profile = MemoryProfile.from_status(status_with())
    moving = status_with(positions=(0, 0, 0, 0), **{"5": 0x11, "10": 0x12})
    assert profile.matches_settings(moving)
    for changed in ({"heat": False}, {"massage": 2}, {"8": 0x2A, "9": 0x30}, {"20": 3}):
        assert not profile.matches_settings(status_with(**changed))


@pytest.mark.parametrize("slot", (None, True, 0, 4, -1, 1.0, "1", []))
def test_recall_rejects_invalid_slot(slot):
    profile = MemoryProfile.from_status(status_with())
    with pytest.raises(ValueError):
        profile.command(status_with(), slot)


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("command_value", (0x80, 0x81, 0x82, 0x8F, 0xFF))
def test_snapshot_rejects_motor_control_bytes(index, command_value):
    positions = [0, 0, 0, 0]
    positions[index] = command_value
    with pytest.raises(ValueError):
        MemoryProfile.from_status(status_with(positions=positions))


@pytest.mark.parametrize("index", range(4))
def test_snapshot_rejects_position_above_observed_range(index):
    positions = [0, 0, 0, 0]
    positions[index] = OBSERVED_BOUNDS[index][1] + 1
    with pytest.raises(ValueError):
        MemoryProfile.from_status(status_with(positions=positions))


@pytest.mark.parametrize("fields", ({"19": 0}, {"17": 0x8F}, {"20": 4}, {"21": 0x8F}, {"22": 255}))
def test_snapshot_rejects_unknown_setting_values(fields):
    with pytest.raises(ValueError):
        MemoryProfile.from_status(status_with(**fields))


@pytest.mark.parametrize("massage_seconds", (0, 1))
def test_captured_inactive_timer_values_roundtrip_without_enabling_functions(massage_seconds):
    status = status_with(
        heat=False,
        massage=0,
        **{"6": 0, "7": massage_seconds, "8": 0, "9": 0, "20": 0, "21": 0, "22": 0},
    )
    profile = MemoryProfile.from_status(status)
    restored = MemoryProfile.from_dict(profile.to_dict())
    command = restored.command(status_with(), 1)
    assert command[6:10] == bytes((0, massage_seconds, 0, 0))
    assert command[17] == 0 and command[19:23] == bytes((2, 0, 0, 0))


@pytest.mark.parametrize(
    "changes",
    [
        {"massage": 1, "6": 0, "7": 0},
        {"massage": 1, "6": 0, "7": 1},
        {"heat": True, "8": 0, "9": 0},
        {"heat": False, "8": 0, "9": 1},
        {"heat": False, "8": 0xFF, "9": 0xFF},
        {"massage": 0, "6": 0xFF, "7": 0xFF},
    ],
)
def test_profile_does_not_invent_active_or_unlimited_timer_values(changes):
    with pytest.raises(ValueError):
        MemoryProfile.from_status(status_with(**changes))


@pytest.mark.parametrize(
    "field,value",
    [
        ("positions", [True, 0, 0, 0]),
        ("positions", [1.0, 0, 0, 0]),
        ("positions", [0, 0, 0]),
        ("heat", 1),
        ("heat", "on"),
        ("heat_levels", [1, 2, 3]),
        ("heat_levels", ["Low", "Low"]),
        ("massage_timer_seconds", 2700.0),
        ("heat_timer_seconds", True),
    ],
)
def test_persisted_profile_validates_types_and_shape(field, value):
    data = MemoryProfile.from_status(status_with()).to_dict()
    data[field] = value
    with pytest.raises(ValueError):
        MemoryProfile.from_dict(data)


@pytest.mark.parametrize("data", (None, [], {}, {"raw": "7E"}))
def test_arbitrary_or_raw_packet_storage_is_rejected(data):
    with pytest.raises(ValueError):
        MemoryProfile.from_dict(data)


def test_profile_recall_needs_valid_current_context():
    profile = MemoryProfile.from_status(status_with())
    with pytest.raises(ValueError):
        profile.command(status_with(**{"19": 0}), 1)
