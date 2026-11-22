"""Independent HA memory buttons, persistence delegation and entry lifecycle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import status_with
from homeassistant.components.button import ButtonEntity
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.storage import Store

from custom_components import svago
from custom_components.svago.button import MemoryButton, async_setup_entry
from custom_components.svago.connection import Connection
from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.switch import MemorySetSwitch

ADDRESS = "AA:BB:CC:DD:EE:01"


def ready_connection():
    connection = Connection(SimpleNamespace(), ADDRESS)
    connection._client = SimpleNamespace(is_connected=True)
    connection._write_char = SimpleNamespace(max_write_without_response_size=256)
    connection._notifications_subscribed = True
    connection._setup_complete = True
    connection.controller.set_connected(True)
    connection.controller.accept_status(status_with())
    return connection


def connection_stub():
    connection = ready_connection()
    connection.controller = SimpleNamespace(
        available=True,
        connected=True,
        status=status_with(),
        can_save_memory=True,
        arm_memory_save=Mock(),
    )
    connection.memories = {}
    connection.save_memory = AsyncMock()
    connection.recall_memory = AsyncMock()
    return connection


def memory_button(connection, slot, *, save=False):
    if save:
        connection._memory_save_armed = True
    return MemoryButton(connection, slot)


async def test_setup_adds_three_empty_memories_without_sending_or_saving():
    connection = connection_stub()
    add_entities = Mock()
    await async_setup_entry(None, SimpleNamespace(runtime_data=connection), add_entities)
    entities = add_entities.call_args.args[0]
    memories = [entity for entity in entities if isinstance(entity, MemoryButton)]
    assert len(entities) == 15
    assert len(memories) == 3
    assert all(isinstance(entity, ButtonEntity) for entity in memories)
    for slot in range(1, 4):
        recall = memories[slot - 1]
        assert recall.name == f"Memory {slot}"
        assert recall.unique_id == f"{ADDRESS}_memory_{slot}"
        assert not recall.available
    assert not any(
        entity.unique_id.endswith(("_seat_vertical", "_seat_horizontal")) for entity in entities
    )
    assert connection.memories == {}
    connection.save_memory.assert_not_called()
    connection.recall_memory.assert_not_called()


@pytest.mark.parametrize("slot", [1, 2, 3])
def test_recall_only_available_when_its_own_profile_exists(slot):
    connection = connection_stub()
    recall = MemoryButton(connection, slot)
    connection.memories[slot % 3 + 1] = object()
    assert not recall.available
    connection.memories[slot] = object()
    assert recall.available
    connection.memories[slot] = None
    assert not recall.available


@pytest.mark.parametrize("slot", [1, 2, 3])
@pytest.mark.parametrize("save", [False, True])
async def test_button_delegates_exact_slot_and_uses_stable_identity(slot, save):
    connection = connection_stub()
    connection.memories[slot] = object()
    entity = memory_button(connection, slot, save=save)
    assert entity.available
    assert entity.icon == "mdi:seat"
    await entity.async_press()
    selected = connection.save_memory if save else connection.recall_memory
    other = connection.recall_memory if save else connection.save_memory
    selected.assert_awaited_once_with(slot)
    other.assert_not_called()


def test_set_uses_controller_stationarity_and_all_buttons_require_connection():
    connection = connection_stub()
    connection.memories[1] = object()
    recall = MemoryButton(connection, 1)
    save = MemorySetSwitch(connection)
    assert recall.available and save.available
    connection._memory_save_armed = True
    connection.controller.can_save_memory = False
    assert not recall.available and save.available
    connection.controller.available = False
    assert not recall.available and not save.available
    connection.controller.available = True
    connection.controller.can_save_memory = True
    connection.memories = None
    assert not recall.available and not save.available


@pytest.mark.parametrize("save", [False, True])
async def test_controller_errors_are_user_facing_without_retries(save):
    connection = connection_stub()
    action = connection.save_memory if save else connection.recall_memory
    action.side_effect = ControlError("Wait for the chair to stop moving")
    with pytest.raises(HomeAssistantError, match="stop moving"):
        await memory_button(connection, 2, save=save).async_press()
    action.assert_awaited_once_with(2)


async def test_superseded_recall_does_not_raise_an_error_toast():
    connection = connection_stub()
    connection.recall_memory.side_effect = CommandSuperseded
    await MemoryButton(connection, 1).async_press()
    connection.recall_memory.assert_awaited_once_with(1)


async def test_direct_recall_of_empty_slot_has_useful_error_and_no_controller_action():
    connection = ready_connection()
    connection.controller.set_connected(True)
    connection.controller.accept_status(status_with())
    connection.memories = SimpleNamespace(async_load=AsyncMock(), get=lambda slot: None)
    connection.controller.recall_memory = AsyncMock()
    with pytest.raises(HomeAssistantError, match="Memory 2 is empty. Turn on Set memory"):
        await MemoryButton(connection, 2).async_press()
    connection.controller.recall_memory.assert_not_awaited()


async def test_connection_recall_uses_saved_profile_without_overwriting_it():
    connection = ready_connection()
    profile = object()
    connection.memories = SimpleNamespace(async_load=AsyncMock(), get={3: profile}.get)
    request = object()
    connection.controller.reserve_memory_recall = Mock(return_value=request)
    connection.controller.recall_memory = AsyncMock()
    await connection.recall_memory(3)
    connection.controller.reserve_memory_recall.assert_called_once_with()
    connection.controller.recall_memory.assert_awaited_once_with(3, profile, request=request)
    assert connection.memories.get(3) is profile


async def test_save_waits_for_persistence_and_never_sends_to_chair():
    connection = ready_connection()
    profile = object()
    connection.controller.snapshot_memory = Mock(return_value=profile)
    connection._write = AsyncMock()
    gate = asyncio.Event()
    entered = asyncio.Event()
    saved = {}

    async def persist(slot, snapshot):
        entered.set()
        await gate.wait()
        saved[slot] = snapshot

    connection.memories = SimpleNamespace(
        async_load=AsyncMock(), async_save=AsyncMock(side_effect=persist)
    )
    request = asyncio.create_task(connection.save_memory(1))
    await entered.wait()
    assert not request.done() and saved == {}
    gate.set()
    await request
    assert saved == {1: profile}
    connection.controller.snapshot_memory.assert_called_once_with()
    connection.memories.async_save.assert_awaited_once_with(1, profile)
    connection._write.assert_not_awaited()


async def test_snapshot_rejection_does_not_persist_or_send():
    connection = ready_connection()
    connection.controller.snapshot_memory = Mock(side_effect=ControlError("Chair is moving"))
    connection.memories = SimpleNamespace(async_load=AsyncMock(), async_save=AsyncMock())
    connection._write = AsyncMock()
    with pytest.raises(ControlError, match="moving"):
        await connection.save_memory(1)
    connection.memories.async_save.assert_not_awaited()
    connection._write.assert_not_awaited()


async def test_storage_failure_is_reported_without_chair_writes():
    connection = ready_connection()
    connection.controller.snapshot_memory = Mock(return_value=object())
    connection.memories = SimpleNamespace(
        async_load=AsyncMock(), async_save=AsyncMock(side_effect=OSError("disk full"))
    )
    connection._write = AsyncMock()
    with pytest.raises(HomeAssistantError, match="Could not save Memory 3"):
        await memory_button(connection, 3, save=True).async_press()
    connection.memories.async_save.assert_awaited_once()
    connection._write.assert_not_awaited()


@pytest.mark.parametrize("save", [False, True])
async def test_storage_read_failure_cannot_save_or_recall_cached_data(save):
    connection = ready_connection()
    connection.controller.set_connected(True)
    connection.controller.accept_status(status_with())
    connection.controller.snapshot_memory = Mock()
    connection.controller.recall_memory = AsyncMock()
    connection.memories = SimpleNamespace(
        async_load=AsyncMock(side_effect=OSError("unreadable disk")),
        async_save=AsyncMock(),
        get=Mock(return_value=object()),
    )
    with pytest.raises(HomeAssistantError, match="Saved memories could not be read"):
        await memory_button(connection, 1, save=save).async_press()
    connection.memories.get.assert_not_called()
    connection.memories.async_save.assert_not_called()
    connection.controller.snapshot_memory.assert_not_called()
    connection.controller.recall_memory.assert_not_called()


async def test_save_takes_snapshot_after_storage_recovery_and_preserves_actionable_errors():
    connection = ready_connection()
    events = []
    profile = object()

    async def load():
        events.append("load")

    def snapshot():
        events.append("snapshot")
        return profile

    connection.controller.snapshot_memory = Mock(side_effect=snapshot)
    connection.memories = SimpleNamespace(
        async_load=AsyncMock(side_effect=load),
        async_save=AsyncMock(side_effect=HomeAssistantError("Home Assistant is stopping")),
    )
    with pytest.raises(HomeAssistantError, match="Home Assistant is stopping"):
        await memory_button(connection, 1, save=True).async_press()
    assert events == ["load", "snapshot"]
    connection.memories.async_save.assert_awaited_once_with(1, profile)


@pytest.mark.parametrize("save", [False, True])
async def test_no_memory_operation_before_store_is_ready(save):
    connection = ready_connection()
    connection.controller.set_connected(True)
    connection.controller.accept_status(status_with())
    connection.controller.snapshot_memory = Mock()
    connection.controller.recall_memory = AsyncMock()
    with pytest.raises(HomeAssistantError, match="Memory storage is not ready"):
        await memory_button(connection, 1, save=save).async_press()
    connection.controller.snapshot_memory.assert_not_called()
    connection.controller.recall_memory.assert_not_called()


def entry_stub():
    return SimpleNamespace(
        entry_id="test-svago-entry",
        data={CONF_ADDRESS: ADDRESS},
        async_on_unload=Mock(),
    )


@pytest.mark.parametrize("address", [ADDRESS, ADDRESS.lower(), "AA:BB:CC:DD:EE:02"])
async def test_store_loads_before_connection_and_platforms_and_survives_unload(
    monkeypatch, address
):
    events = []

    async def load():
        events.append("load")

    async def start():
        events.append("start")

    async def forward(entry, platforms):
        assert entry.runtime_data.memories is store
        events.append("platforms")

    store = SimpleNamespace(
        async_load=AsyncMock(side_effect=load),
        async_import_missing=AsyncMock(),
        async_restore_once=AsyncMock(),
        async_remove=AsyncMock(),
    )
    connection = SimpleNamespace(
        start=AsyncMock(side_effect=start),
        close=AsyncMock(),
        notify_listeners=Mock(),
        load_preferences=AsyncMock(),
    )
    factory = Mock(return_value=store)
    monkeypatch.setattr(svago, "async_remove_retired_entities", Mock())
    monkeypatch.setattr(svago, "MemoryStore", factory)
    monkeypatch.setattr(svago, "Connection", Mock(return_value=connection))
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(side_effect=forward),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
        bus=SimpleNamespace(async_listen_once=Mock()),
    )
    entry = entry_stub()
    entry.data[CONF_ADDRESS] = address
    assert await svago.async_setup_entry(hass, entry)
    svago.async_remove_retired_entities.assert_called_once_with(hass, entry)
    assert events == ["load", "start", "platforms"]
    store.async_import_missing.assert_not_called()
    store.async_restore_once.assert_not_called()
    factory.assert_called_once_with(hass, entry.entry_id, connection.notify_listeners)
    assert await svago.async_unload_entry(hass, entry)
    connection.close.assert_awaited_once()
    store.async_remove.assert_not_called()


@pytest.mark.parametrize("address", [ADDRESS, "AA:BB:CC:DD:EE:02"])
@pytest.mark.parametrize("existing", [False, True])
async def test_public_setup_keeps_existing_memories_or_starts_empty_without_defaults(
    tmp_path, monkeypatch, address, existing
):
    """An address-only setup preserves saved slots and never writes memory defaults."""
    from pathlib import Path

    from custom_components.svago.memory_protocol import MemoryProfile

    hass = HomeAssistant(str(tmp_path))
    entry = entry_stub()
    entry.data = {CONF_ADDRESS: address}
    storage = Store(hass, 1, f"svago.memories.{entry.entry_id}")
    path = Path(storage.path)
    # Simulate the six-field profiles used before Continuous was saved per slot.
    legacy_profiles = {
        str(slot): {
            "positions": [slot, 8, 12, 20],
            "massage": "Off",
            "heat": False,
            "heat_levels": ["Low", "Medium", "High"],
            "massage_timer_seconds": 900,
            "heat_timer_seconds": 10800,
        }
        for slot in (1, 2, 3)
    }
    original = {
        "profiles": legacy_profiles,
        "applied_migrations": ["restore_original_memory_3_0_1_15"],
    }
    original_bytes = None
    if existing:
        await storage.async_save(original)
        original_bytes = await hass.async_add_executor_job(path.read_bytes)

    connection = Connection(hass, address)
    connection.start = AsyncMock()
    connection.controller._write = AsyncMock()
    monkeypatch.setattr(svago, "Connection", Mock(return_value=connection))
    monkeypatch.setattr(svago, "async_remove_retired_entities", Mock())
    hass.config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(),
        async_unload_platforms=AsyncMock(return_value=True),
    )
    try:
        assert await svago.async_setup_entry(hass, entry)
        connection.start.assert_awaited_once_with()
        if existing:
            assert [connection.memories.get(slot) for slot in (1, 2, 3)] == [
                MemoryProfile.from_dict(legacy_profiles[str(slot)]) for slot in (1, 2, 3)
            ]
            assert connection.memories._applied_migrations == frozenset(
                original["applied_migrations"]
            )
        else:
            assert all(connection.memories.get(slot) is None for slot in (1, 2, 3))
        connection.controller._write.assert_not_awaited()
        assert await svago.async_unload_entry(hass, entry)
        if existing:
            assert await hass.async_add_executor_job(path.read_bytes) == original_bytes
        else:
            assert not await hass.async_add_executor_job(path.exists)
    finally:
        await connection.close()
        await hass.async_block_till_done()
        await hass.async_stop(force=True)


async def test_storage_load_error_prevents_setup_without_overwriting_or_removing(monkeypatch):
    store = SimpleNamespace(
        async_load=AsyncMock(side_effect=OSError("unreadable storage")), async_remove=AsyncMock()
    )
    connection = SimpleNamespace(
        start=AsyncMock(), close=AsyncMock(), notify_listeners=Mock(), load_preferences=AsyncMock()
    )
    monkeypatch.setattr(svago, "MemoryStore", Mock(return_value=store))
    monkeypatch.setattr(svago, "Connection", Mock(return_value=connection))
    with pytest.raises(ConfigEntryNotReady, match="unreadable storage"):
        await svago.async_setup_entry(Mock(), entry_stub())
    connection.start.assert_not_called()
    connection.close.assert_awaited_once()
    store.async_remove.assert_not_called()


async def test_actual_entry_removal_deletes_profiles_without_a_bluetooth_connection(monkeypatch):
    store = SimpleNamespace(async_close=AsyncMock(), async_remove=AsyncMock())
    factory = Mock(return_value=store)
    connection_factory = Mock()
    monkeypatch.setattr(svago, "MemoryStore", factory)
    preferences = SimpleNamespace(async_close=AsyncMock(), async_remove=AsyncMock())
    monkeypatch.setattr(svago, "TimerPreferences", Mock(return_value=preferences))
    monkeypatch.setattr(svago, "Connection", connection_factory)
    hass = Mock()
    entry = entry_stub()
    await svago.async_remove_entry(hass, entry)
    assert factory.call_args.args[:2] == (hass, entry.entry_id)
    store.async_close.assert_awaited_once()
    store.async_remove.assert_awaited_once()
    preferences.async_close.assert_not_awaited()
    preferences.async_remove.assert_awaited_once()
    connection_factory.assert_not_called()


async def test_removal_reuses_runtime_store_to_share_its_pending_save_barrier(monkeypatch):
    store = SimpleNamespace(async_close=AsyncMock(), async_remove=AsyncMock())
    entry = entry_stub()
    preferences = SimpleNamespace(async_close=AsyncMock(), async_remove=AsyncMock())
    entry.runtime_data = SimpleNamespace(memories=store, preferences=preferences)
    factory = Mock()
    monkeypatch.setattr(svago, "MemoryStore", factory)
    await svago.async_remove_entry(Mock(), entry)
    store.async_close.assert_awaited_once()
    store.async_remove.assert_awaited_once()
    preferences.async_close.assert_not_awaited()
    preferences.async_remove.assert_awaited_once()
    factory.assert_not_called()


async def test_unload_drops_chair_then_drains_save_before_reload_and_removal(tmp_path):
    from conftest import status_with
    from homeassistant.core import HomeAssistant

    from custom_components.svago.memory import MemoryStore
    from custom_components.svago.memory_protocol import MemoryProfile

    hass = HomeAssistant(str(tmp_path))
    entry = entry_stub()
    connection = Connection(hass, ADDRESS)
    entry.runtime_data = connection
    profile = MemoryProfile.from_status(status_with())
    connection.controller.snapshot_memory = Mock(return_value=profile)
    connection.memories = MemoryStore(hass, entry.entry_id, connection.notify_listeners)
    await connection.memories.async_load()
    persist = connection.memories._persist
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_persist(data):
        entered.set()
        await release.wait()
        await persist(data)

    connection.memories._persist = delayed_persist
    pending_save = asyncio.create_task(connection.save_memory(1))
    await entered.wait()
    closing = asyncio.create_task(connection.close())
    await asyncio.sleep(0)
    assert connection._closed and not connection.controller.connected
    assert not closing.done()
    with pytest.raises(ControlError, match="unloading"):
        await connection.save_memory(2)
    release.set()
    await pending_save
    await closing
    reloaded = MemoryStore(hass, entry.entry_id)
    await reloaded.async_load()
    assert reloaded.get(1) == profile
    await reloaded.async_close()
    await svago.async_remove_entry(hass, entry)
    after_removal = MemoryStore(hass, entry.entry_id)
    await after_removal.async_load()
    assert after_removal.get(1) is None


def test_memory_diagnostics_exposes_slots_and_copies_traces_without_identifiers():
    connection = ready_connection()
    connection.memories = {2: object()}
    connection.controller.memory_requests.append({"slot": 2, "outcome": "complete"})
    result = connection.diagnostics()
    assert result["saved_memory_slots"] == [2]
    assert result["memory_requests"] == [{"slot": 2, "outcome": "complete"}]
    result["memory_requests"][0]["outcome"] = "edited copy"
    assert connection.controller.memory_requests[0]["outcome"] == "complete"
    assert ADDRESS not in str(result) and "test-svago-entry" not in str(result)


@pytest.mark.parametrize("newer", ["stop", "heat_off", "setting", "reconnect"])
async def test_delayed_memory_load_cannot_start_after_newer_control(chair, newer):
    from conftest import status_with

    from custom_components.svago.memory_protocol import MemoryProfile

    connection = ready_connection()
    connection.controller = chair.controller
    profile = MemoryProfile.from_status(status_with(positions=(1, 2, 3, 4)))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def load():
        entered.set()
        await release.wait()

    connection.memories = SimpleNamespace(
        async_load=AsyncMock(side_effect=load), get={1: profile}.get
    )
    old_recall = asyncio.create_task(connection.recall_memory(1))
    await entered.wait()
    assert chair.writes == []
    if newer == "stop":
        await chair.controller.stop_movement()
    elif newer == "heat_off":
        await chair.controller.set_heat(False)
    elif newer == "setting":
        await chair.controller.set_timer("heat", 120)
    else:
        chair.controller.set_connected(False)
        chair.controller.set_connected(True)
        chair.controller.accept_status(status_with())
    writes_before_load_finishes = list(chair.writes)
    release.set()
    with pytest.raises(CommandSuperseded):
        await old_recall
    assert chair.writes == writes_before_load_finishes


async def test_latest_of_two_disk_delayed_recalls_wins_even_if_older_load_finishes_first(chair):
    from conftest import status_with

    from custom_components.svago.memory_protocol import MemoryProfile

    connection = ready_connection()
    connection.controller = chair.controller
    profiles = {
        1: MemoryProfile.from_status(status_with(positions=(1, 2, 3, 4))),
        2: MemoryProfile.from_status(status_with(positions=(2, 3, 4, 5))),
    }
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = 0

    async def load():
        nonlocal calls
        index = calls
        calls += 1
        entered[index].set()
        await release[index].wait()

    connection.memories = SimpleNamespace(async_load=AsyncMock(side_effect=load), get=profiles.get)
    first = asyncio.create_task(connection.recall_memory(1))
    await entered[0].wait()
    second = asyncio.create_task(connection.recall_memory(2))
    await entered[1].wait()
    release[0].set()
    with pytest.raises(CommandSuperseded):
        await first
    assert chair.writes == []
    release[1].set()
    await second
    numeric_memory_writes = [
        payload
        for payload in chair.writes
        if payload[5] == 0 and payload[10] in (0x11, 0x12, 0x13) and max(payload[13:17]) < 0x80
    ]
    assert len(numeric_memory_writes) == 1
    assert numeric_memory_writes[0][10] == 0x12
    assert chair.controller.status.positions == profiles[2].positions


async def test_newer_setting_waiting_for_serialization_supersedes_disk_delayed_recall(chair):
    from custom_components.svago.memory_protocol import MemoryProfile

    connection = ready_connection()
    connection.controller = chair.controller
    profile = MemoryProfile.from_status(status_with(positions=(1, 2, 3, 4)))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def load():
        entered.set()
        await release.wait()

    connection.memories = SimpleNamespace(
        async_load=AsyncMock(side_effect=load), get={1: profile}.get
    )
    await chair.controller._settings_lock.acquire()
    try:
        old_recall = asyncio.create_task(connection.recall_memory(1))
        await entered.wait()
        newer_setting = asyncio.create_task(chair.controller.set_timer("heat", 120))
        await asyncio.sleep(0)
        assert not newer_setting.done()
        release.set()
        with pytest.raises(CommandSuperseded):
            await old_recall
        assert chair.writes == []
    finally:
        chair.controller._settings_lock.release()
    await newer_setting
    assert chair.controller.status.timer_minutes("heat") == 120
