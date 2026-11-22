"""Run the public startup example through Home Assistant's real script engine."""

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from homeassistant.components.script.config import SCRIPT_ENTITY_SCHEMA
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.script import Script, async_validate_actions_config

EXAMPLE = Path(__file__).parents[1] / "examples" / "start_when_ready.yaml"
READY = "sensor.example_connection_status"
BUTTON = "button.example_turn_on"
INPUTS = {"ready_sensor": READY, "turn_on_button": BUTTON}


@pytest.fixture
async def example_hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    calls = []

    async def press(call):
        calls.append(call.data)

    hass.services.async_register("button", "press", press)
    yield hass, calls
    await hass.async_block_till_done()
    await hass.async_stop(force=True)


async def make_script(hass, *, short_timeout=False):
    raw = yaml.safe_load(EXAMPLE.read_text())
    if short_timeout:
        raw["sequence"][1]["timeout"] = 0.01
    config = SCRIPT_ENTITY_SCHEMA(deepcopy(raw))
    sequence = await async_validate_actions_config(hass, config["sequence"])
    return Script(hass, sequence, "Svago startup example", "script", script_mode=config["mode"])


@pytest.mark.parametrize("button_state", ["unknown", "2026-01-01T00:00:00+00:00"])
async def test_ready_chair_is_pressed_once_without_waiting_for_a_state_change(
    example_hass, button_state
):
    hass, calls = example_hass
    hass.states.async_set(READY, "Ready")
    hass.states.async_set(BUTTON, button_state)
    script = await make_script(hass)
    await asyncio.wait_for(script.async_run(INPUTS, Context()), timeout=1)
    assert calls == [{"entity_id": [BUTTON]}]


async def test_waiting_chair_proceeds_when_readiness_arrives(example_hass):
    hass, calls = example_hass
    hass.states.async_set(READY, "Connecting")
    hass.states.async_set(BUTTON, "unavailable")
    script = await make_script(hass)
    running = asyncio.create_task(script.async_run(INPUTS, Context()))
    await hass.async_block_till_done()
    assert script.is_running and calls == []
    hass.states.async_set(READY, "Ready")
    hass.states.async_set(BUTTON, "unknown")
    await asyncio.wait_for(running, timeout=1)
    assert calls == [{"entity_id": [BUTTON]}]


async def test_new_invocation_replaces_the_old_wait_instead_of_pressing_twice(example_hass):
    hass, calls = example_hass
    hass.states.async_set(READY, "Connecting")
    hass.states.async_set(BUTTON, "unavailable")
    script = await make_script(hass)
    first = asyncio.create_task(script.async_run(INPUTS, Context()))
    await hass.async_block_till_done()
    second = asyncio.create_task(script.async_run(INPUTS, Context()))
    await hass.async_block_till_done()
    await asyncio.wait_for(first, timeout=1)
    assert not second.done() and calls == []
    hass.states.async_set(READY, "Ready")
    hass.states.async_set(BUTTON, "unknown")
    await asyncio.wait_for(second, timeout=1)
    assert calls == [{"entity_id": [BUTTON]}]


@pytest.mark.parametrize(
    "case", ["missing_button", "unavailable_button", "not_ready", "missing_sensor"]
)
async def test_timeout_never_presses_missing_or_unavailable_controls(example_hass, case):
    hass, calls = example_hass
    if case != "missing_sensor":
        hass.states.async_set(READY, "Connecting" if case == "not_ready" else "Ready")
    if case != "missing_button":
        hass.states.async_set(BUTTON, "unavailable" if case == "unavailable_button" else "unknown")
    script = await make_script(hass, short_timeout=True)
    await asyncio.wait_for(script.async_run(INPUTS, Context()), timeout=1)
    assert calls == []


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"ready_sensor": READY},
        {"turn_on_button": BUTTON},
        {**INPUTS, "ready_sensor": ""},
        {**INPUTS, "turn_on_button": "switch.power"},
        {**INPUTS, "turn_on_button": None},
    ],
)
async def test_script_fields_are_required_at_runtime(example_hass, inputs):
    hass, calls = example_hass
    hass.states.async_set(READY, "Ready")
    hass.states.async_set(BUTTON, "unknown")
    script = await make_script(hass)
    await asyncio.wait_for(script.async_run(inputs, Context()), timeout=1)
    assert calls == []
