"""Exercise recall-only targeting and arming races with a simulated chair and RAM store."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import probatio
import pytest
from conftest import FakeChair, status_with
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotSupported
from homeassistant.helpers import service
from homeassistant.helpers.entity_platform import DATA_DOMAIN_PLATFORM_ENTITIES

from custom_components import svago
from custom_components.svago.button import HomeButton, MemoryButton
from custom_components.svago.connection import Connection
from custom_components.svago.const import DOMAIN
from custom_components.svago.memory_protocol import MemoryProfile


def test_ha_loads_recall_target_for_action_editor():
    # HA can register the action but drop its entire target picker if the
    # description uses a feature that the selector cannot resolve.
    integration = SimpleNamespace(domain=DOMAIN, file_path=Path(svago.__file__).parent)
    description = service._load_services_file(integration)
    assert description["recall_memory"]["target"]["entity"] == [
        {"domain": ["button"], "integration": DOMAIN}
    ]


@pytest.fixture
async def configured(tmp_path, chair):
    hass = HomeAssistant(str(tmp_path))
    connection = Connection(hass, "02:00:00:00:00:03")
    connection.controller = chair.controller
    connection._client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
    connection._write_char = SimpleNamespace(max_write_without_response_size=256)
    connection._notifications_subscribed = connection._setup_complete = True
    chair.time += 2.1
    chair.controller.accept_status(chair.controller.status)
    profiles = {n: MemoryProfile.from_status(status_with(positions=(n, 2, 3, 4))) for n in (1, 3)}
    connection.memories = SimpleNamespace(
        async_load=AsyncMock(), get=profiles.get, async_save=AsyncMock(), async_close=AsyncMock()
    )
    entities = {}
    for suffix, entity in (
        ("my_first_slot", MemoryButton(connection, 1)),
        ("my_other_slot", MemoryButton(connection, 3)),
        ("my_home", HomeButton(connection)),
    ):
        entity.entity_id = f"button.{suffix}"
        entity.hass = hass
        entities[entity.entity_id] = entity
    hass.data[DATA_DOMAIN_PLATFORM_ENTITIES] = {("button", DOMAIN): entities}
    assert await svago.async_setup(hass, {})

    async def recall(data):
        await hass.services.async_call(DOMAIN, "recall_memory", data, blocking=True)

    try:
        yield connection, recall
    finally:
        if not connection._closed:
            await connection.close()
        await hass.async_block_till_done()
        await hass.async_stop(force=True)


async def test_action_uses_selected_renamed_slot_and_never_saves(configured, chair):
    connection, recall = configured
    await recall({"entity_id": "button.my_other_slot"})
    assert [r["slot"] for r in chair.controller.memory_requests] == [3]
    assert chair.controller.status.positions == (3, 2, 3, 4)
    assert connection.activity.last_action["request"] == "Recall"
    connection.memories.async_save.assert_not_awaited()


async def test_arm_between_automation_guard_and_service_cannot_save(configured, chair):
    connection, recall = configured
    assert not connection.memory_save_armed()  # What the caller saw at its guard.
    await connection.set_memory_save_armed(True)
    with pytest.raises(HomeAssistantError, match="Set memory"):
        await recall({"entity_id": "button.my_other_slot"})
    assert connection.memory_save_armed() and not chair.writes
    connection.memories.async_save.assert_not_awaited()
    connection.memories.async_load.assert_not_awaited()


async def test_rearming_during_recall_storage_lookup_neither_saves_nor_moves(configured, chair):
    connection, recall = configured
    entered = asyncio.Event()
    release = asyncio.Event()

    async def load():
        entered.set()
        await release.wait()

    connection.memories.async_load.side_effect = load
    task = asyncio.create_task(recall({"entity_id": "button.my_first_slot"}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await connection.set_memory_save_armed(True)
    finally:
        release.set()
        await task
    assert connection.activity.last_action["outcome"] == "superseded"
    assert connection.memory_save_armed() and not chair.writes
    connection.memories.async_save.assert_not_awaited()


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"entity_id": "all"},
        {"entity_id": "group.chair_memories"},
        {"entity_id": ["button.my_first_slot", "button.my_other_slot"]},
        {"area_id": "theater"},
        {"entity_id": "button.my_first_slot", "device_id": "0123456789abcdef0123456789abcdef"},
    ],
)
async def test_action_rejects_ambiguous_motion_targets(configured, chair, target):
    _, recall = configured
    with pytest.raises(probatio.Invalid):
        await recall(target)
    assert not chair.writes


async def test_action_rejects_other_svago_button_before_any_motion(configured, chair):
    _, recall = configured
    with pytest.raises(ServiceNotSupported):
        await recall({"entity_id": "button.my_home"})
    assert not chair.writes


async def test_targeted_chair_isolated_and_service_survives_other_chair_unload(configured, chair):
    connection, recall = configured
    hass = connection.hass
    other_chair = FakeChair()
    other = Connection(hass, "02:00:00:00:00:04")
    other.controller = other_chair.controller
    other._client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
    other._write_char = SimpleNamespace(max_write_without_response_size=256)
    other._notifications_subscribed = other._setup_complete = True
    profile = MemoryProfile.from_status(status_with(positions=(7, 3, 8, 5)))
    other.memories = SimpleNamespace(
        async_load=AsyncMock(),
        get={3: profile}.get,
        async_save=AsyncMock(),
        async_close=AsyncMock(),
    )
    entity = MemoryButton(other, 3)
    entity.entity_id = "button.second_chair_memory"
    entity.hass = hass
    entities = hass.data[DATA_DOMAIN_PLATFORM_ENTITIES][("button", DOMAIN)]
    entities[entity.entity_id] = entity

    try:
        await recall({"entity_id": "button.my_other_slot"})
        assert chair.controller.status.positions == (3, 2, 3, 4)
        assert [trace["slot"] for trace in chair.controller.memory_requests] == [3]
        assert not other_chair.writes
        other.memories.async_load.assert_not_awaited()

        # Entry unloading closes its transport and removes only its entities.
        await connection.close()
        for entity_id, registered in tuple(entities.items()):
            if registered.connection is connection:
                del entities[entity_id]
        original_writes = list(chair.writes)
        assert hass.services.has_service(DOMAIN, "recall_memory")

        await recall({"entity_id": entity.entity_id})
        assert other_chair.controller.status.positions == (7, 3, 8, 5)
        assert [trace["slot"] for trace in other_chair.controller.memory_requests] == [3]
        assert chair.writes == original_writes
        other.memories.async_save.assert_not_awaited()
        connection.memories.async_save.assert_not_awaited()
    finally:
        await other.close()


@pytest.mark.parametrize("interruption", ["stop", "unload"])
async def test_pending_service_storage_lookup_cannot_move_after_stop_or_unload(
    configured, chair, interruption
):
    connection, recall = configured
    entered, release = asyncio.Event(), asyncio.Event()

    async def load():
        entered.set()
        await release.wait()

    connection.memories.async_load.side_effect = load
    task = asyncio.create_task(recall({"entity_id": "button.my_other_slot"}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not chair.writes
        if interruption == "stop":
            await chair.controller.stop_movement()
            assert len(chair.writes) == 1
            assert chair.writes[0][13:17] == bytes([0x80] * 4)
        else:
            await connection.close()
            assert not chair.writes
        writes_after_interruption = list(chair.writes)
        release.set()
        if interruption == "unload":
            with pytest.raises(HomeAssistantError, match="unloading"):
                await asyncio.wait_for(task, 2)
            assert connection.activity.last_action["outcome"] == "failed"
        else:
            await asyncio.wait_for(task, 2)
            assert connection.activity.last_action["outcome"] == "superseded"
        assert chair.writes == writes_after_interruption
        assert not chair.controller.memory_requests
        connection.memories.async_save.assert_not_awaited()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("unavailable", [False, True], ids=["unknown", "unavailable"])
async def test_unknown_or_unavailable_target_follows_ha_noop_without_motion(
    configured, chair, unavailable, caplog
):
    connection, recall = configured
    target = "button.my_other_slot" if unavailable else "button.does_not_exist"
    if unavailable:
        connection._client.is_connected = False

    # Standard HA entity services report missing/unavailable targets in the log
    # without invoking the entity method or raising for a non-response action.
    assert await recall({"entity_id": target}) is None
    assert target in caplog.text
    assert not chair.writes
    assert connection.activity.last_action is None
    connection.memories.async_load.assert_not_awaited()
    connection.memories.async_save.assert_not_awaited()
