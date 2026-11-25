"""Run the readiness example and a guarded caller through HA's real script engine."""

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from homeassistant.components.script.config import SCRIPT_ENTITY_SCHEMA
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.script import Script, async_validate_actions_config

EXAMPLE = Path(__file__).parents[1] / "examples" / "wait_until_ready.yaml"
READY = "sensor.example_connection_status"
BUTTON = "button.example_action"
INPUTS = {"ready_sensor": READY}


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
    return Script(hass, sequence, "Svago readiness example", "script", script_mode=config["mode"])


async def test_ready_chair_returns_without_waiting_for_a_state_change(example_hass):
    hass, calls = example_hass
    hass.states.async_set(READY, "Ready")
    script = await make_script(hass)
    await asyncio.wait_for(script.async_run(INPUTS, Context()), timeout=1)
    assert not script.is_running and calls == []


async def test_waiting_chair_proceeds_when_readiness_arrives(example_hass):
    hass, calls = example_hass
    hass.states.async_set(READY, "Connecting")
    script = await make_script(hass)
    running = asyncio.create_task(script.async_run(INPUTS, Context()))
    await hass.async_block_till_done()
    assert script.is_running and calls == []
    hass.states.async_set(READY, "Ready")
    await asyncio.wait_for(running, timeout=1)
    assert not script.is_running and calls == []


async def test_new_invocation_replaces_the_old_wait(example_hass):
    hass, calls = example_hass
    hass.states.async_set(READY, "Connecting")
    script = await make_script(hass)
    first = asyncio.create_task(script.async_run(INPUTS, Context()))
    await hass.async_block_till_done()
    second = asyncio.create_task(script.async_run(INPUTS, Context()))
    await hass.async_block_till_done()
    await asyncio.wait_for(first, timeout=1)
    assert not second.done() and calls == []
    hass.states.async_set(READY, "Ready")
    await asyncio.wait_for(second, timeout=1)
    assert not script.is_running and calls == []


@pytest.mark.parametrize("state", [None, "Connecting", "unavailable"])
async def test_timeout_finishes_without_a_chair_action(example_hass, state):
    hass, calls = example_hass
    if state is not None:
        hass.states.async_set(READY, state)
    script = await make_script(hass, short_timeout=True)
    await asyncio.wait_for(script.async_run(INPUTS, Context()), timeout=1)
    assert not script.is_running and calls == []


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"ready_sensor": ""},
        {"ready_sensor": "switch.example"},
        {"ready_sensor": None},
        {"ready_sensor": [READY]},
        {"ready_sensor": "sensor.invalid space"},
    ],
)
async def test_sensor_field_is_validated_at_runtime(example_hass, inputs, caplog):
    caplog.set_level("INFO", logger="homeassistant.helpers.script")
    hass, calls = example_hass
    hass.states.async_set(READY, "Ready")
    script = await make_script(hass)
    await asyncio.wait_for(script.async_run(inputs, Context()), timeout=1)
    assert "Choose the chair's Connection status sensor." in caplog.text
    assert not script.is_running and calls == []


@pytest.mark.parametrize("ready", [False, True])
async def test_calling_script_checks_readiness_before_its_action(example_hass, ready):
    hass, calls = example_hass
    hass.states.async_set(READY, "Ready" if ready else "Connecting")
    child = await make_script(hass, short_timeout=True)

    async def wait_until_ready(call):
        await child.async_run(call.data, call.context)

    hass.services.async_register("script", "svago_wait_until_ready", wait_until_ready)
    actions = await async_validate_actions_config(
        hass,
        [
            {"action": "script.svago_wait_until_ready", "data": INPUTS},
            {"condition": "state", "entity_id": READY, "state": "Ready"},
            {"action": "button.press", "target": {"entity_id": BUTTON}},
        ],
    )
    parent = Script(hass, actions, "Guarded Svago caller", "script")
    await asyncio.wait_for(parent.async_run({}, Context()), timeout=1)
    assert calls == ([{"entity_id": [BUTTON]}] if ready else [])
