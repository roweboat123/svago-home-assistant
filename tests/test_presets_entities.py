"""Native preset buttons delegate to the serialized controller."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from custom_components.svago.button import BUTTONS, PresetButton, async_setup_entry
from custom_components.svago.controller import CommandSuperseded, ControlError

ADDRESS = "02:00:00:00:00:AB"


def connection_stub():
    controller = SimpleNamespace(available=True, select_preset=AsyncMock())
    return SimpleNamespace(controller=controller, address=ADDRESS)


async def test_setup_retains_existing_button_identities_and_adds_presets_home_and_memories():
    connection = connection_stub()
    add_entities = Mock()
    await async_setup_entry(None, SimpleNamespace(runtime_data=connection), add_entities)
    entities = add_entities.call_args.args[0]
    assert len(entities) == len(BUTTONS) + 6
    assert all(isinstance(entity, ButtonEntity) for entity in entities)
    expected = {f"{ADDRESS}_{key}": name for key, name, _, _ in BUTTONS}
    expected.update({f"{ADDRESS}_preset_zgr": "ZGR", f"{ADDRESS}_preset_lay_flat": "Lay flat"})
    expected[f"{ADDRESS}_return_home"] = "Return to neutral"
    for slot in range(1, 4):
        expected[f"{ADDRESS}_memory_{slot}"] = f"Memory {slot}"
    assert {entity.unique_id: entity.name for entity in entities} == expected
    connection.controller.select_preset.assert_not_called()


@pytest.mark.parametrize("preset,name", [("zgr", "ZGR"), ("lay_flat", "Lay flat")])
async def test_preset_press_sends_the_specific_preset_once(preset, name):
    connection = connection_stub()
    entity = PresetButton(connection, preset, name)
    assert entity.name == name
    assert entity.unique_id == f"{ADDRESS}_preset_{preset}"
    assert entity.icon == "mdi:seat"
    await entity.async_press()
    connection.controller.select_preset.assert_awaited_once_with(preset)


@pytest.mark.parametrize("preset,name", [("zgr", "ZGR"), ("lay_flat", "Lay flat")])
async def test_preset_failure_surfaces_without_silently_retrying(preset, name):
    connection = connection_stub()
    connection.controller.select_preset.side_effect = ControlError("Preset not confirmed")
    with pytest.raises(HomeAssistantError, match="Preset not confirmed"):
        await PresetButton(connection, preset, name).async_press()
    connection.controller.select_preset.assert_awaited_once_with(preset)


async def test_replaced_preset_request_does_not_raise_an_error_toast():
    connection = connection_stub()
    connection.controller.select_preset.side_effect = CommandSuperseded
    await PresetButton(connection, "zgr", "ZGR").async_press()
    connection.controller.select_preset.assert_awaited_once_with("zgr")


def test_preset_availability_tracks_actual_connection_freshness(chair):
    connection = SimpleNamespace(controller=chair.controller, address=ADDRESS)
    entities = [
        PresetButton(connection, "zgr", "ZGR"),
        PresetButton(connection, "lay_flat", "Lay flat"),
    ]
    assert all(entity.available for entity in entities)
    chair.time += 4
    assert all(not entity.available for entity in entities)
    chair.controller.set_connected(False)
    assert all(not entity.available for entity in entities)
