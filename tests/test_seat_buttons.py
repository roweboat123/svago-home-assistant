"""Restore historical seat controls through the existing directional/wake path."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.components.button import ButtonEntity

from custom_components.svago.button import async_setup_entry
from custom_components.svago.controller import MOTOR_PULSE_SECONDS

ADDRESS = "02:00:00:00:00:AB"
SEAT_BUTTONS = (
    ("seat_vertical", "Seat tilt up", 1),
    ("seat_horizontal", "Seat tilt down", -1),
)


async def registered_buttons(chair):
    connection = SimpleNamespace(controller=chair.controller, address=ADDRESS)
    add_entities = Mock()
    await async_setup_entry(None, SimpleNamespace(runtime_data=connection), add_entities)
    return {entity.unique_id: entity for entity in add_entities.call_args.args[0]}


async def test_setup_restores_both_historical_seat_identities_without_commands(chair):
    entities = await registered_buttons(chair)
    for key, name, _direction in SEAT_BUTTONS:
        entity = entities[f"{ADDRESS}_{key}"]
        assert isinstance(entity, ButtonEntity)
        assert entity.name == name
        assert entity.entity_registry_enabled_default
        assert entity.entity_category is None
        assert entity.available
    assert chair.writes == []


@pytest.mark.parametrize("key,name,direction", SEAT_BUTTONS)
@pytest.mark.parametrize("position_feedback", [False, True])
async def test_seat_press_uses_original_direction_wakes_and_stops_after_quarter_second(
    chair, key, name, direction, position_feedback
):
    entities = await registered_buttons(chair)
    entity = entities[f"{ADDRESS}_{key}"]
    initial = chair.controller.status.positions
    chair.feedback = position_feedback
    chair.activation_feedback = True
    await entity.async_press()

    assert entity.name == name
    assert chair.writes[0][5] == 0x11
    assert MOTOR_PULSE_SECONDS[2] == 0.25
    assert chair.time == pytest.approx(0.25)
    direction_byte = 0x82 if direction == 1 else 0x81
    movement = chair.writes[1:-1]
    assert len(movement) == 3
    assert all(packet[13:17] == bytes([0x8F, 0x8F, direction_byte, 0x8F]) for packet in movement)
    assert chair.writes[-1][13:17] == bytes([0x80] * 4)
    after = chair.controller.status.positions
    if position_feedback:
        assert (after[2] - initial[2]) * direction > 0
    else:
        assert after == initial
    trace = chair.controller.tap_requests[-1]
    assert trace["startup_allowance_seconds"] == 0
    assert trace["startup_extended"] is False
    assert trace["feedback_changed"] is position_feedback
    assert trace["startup_outcome"] == (
        "progress_observed" if position_feedback else "not_observed_during_timed_pulse"
    )
    assert after[:2] == initial[:2] and after[3] == initial[3]
