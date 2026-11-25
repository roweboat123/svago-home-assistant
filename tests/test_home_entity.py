"""Native Home is a configuration button, independent of saved memory slots."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import status_with
from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.svago.button import HomeButton, async_setup_entry
from custom_components.svago.connection import Connection
from custom_components.svago.memory import MemoryStore

ADDRESS = "AA:BB:CC:DD:EE:01"


@pytest.fixture
async def home_connection(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    connection = Connection(hass, ADDRESS)
    connection._client = SimpleNamespace(is_connected=True)
    connection._write_char = SimpleNamespace(max_write_without_response_size=256)
    connection._notifications_subscribed = True
    connection._setup_complete = True
    connection.controller.set_connected(True)
    connection.controller.accept_status(status_with())
    connection.memories = MemoryStore(hass, "native_home")
    await connection.memories.async_load()
    connection.controller.return_home = AsyncMock()
    try:
        yield connection
    finally:
        await connection.memories.async_close()
        await connection.controller.cancel_active()
        await hass.async_block_till_done()
        await hass.async_stop(force=True)


async def test_home_setup_and_press_do_not_read_or_write_empty_memory_slots(
    home_connection, monkeypatch
):
    connection = home_connection
    store = connection.memories
    get_slot = store.get
    assert all(get_slot(slot) is None for slot in (1, 2, 3))
    for name in ("async_load", "async_save", "async_import_missing", "async_restore_once"):
        monkeypatch.setattr(store, name, AsyncMock(wraps=getattr(store, name)))
    monkeypatch.setattr(store, "get", Mock(wraps=get_slot))
    add_entities = Mock()

    await async_setup_entry(None, SimpleNamespace(runtime_data=connection), add_entities)
    entities = add_entities.call_args.args[0]
    homes = [entity for entity in entities if isinstance(entity, HomeButton)]
    assert len(homes) == 1
    button = homes[0]
    assert isinstance(button, ButtonEntity)
    assert button.name == "Return to neutral"
    assert button.unique_id == f"{ADDRESS}_return_home"
    assert HomeButton(connection).unique_id == button.unique_id
    assert button.entity_category is EntityCategory.CONFIG
    assert button.available

    await button.async_press()

    connection.controller.return_home.assert_awaited_once_with()
    store.get.assert_not_called()
    store.async_load.assert_not_called()
    store.async_save.assert_not_called()
    store.async_import_missing.assert_not_called()
    store.async_restore_once.assert_not_called()
    assert all(get_slot(slot) is None for slot in (1, 2, 3))


@pytest.mark.parametrize("saving", [False, True], ids=["armed", "saving"])
async def test_home_is_blocked_during_memory_save(home_connection, saving):
    connection = home_connection
    connection._memory_save_armed = not saving
    connection._saving_memory_slot = 2 if saving else None
    button = HomeButton(connection)

    assert not button.available
    with pytest.raises(HomeAssistantError, match="Finish or cancel Set memory"):
        await button.async_press()

    connection.controller.return_home.assert_not_awaited()
    assert all(connection.memories.get(slot) is None for slot in (1, 2, 3))


async def test_home_requires_a_ready_chair_connection(home_connection):
    button = HomeButton(home_connection)
    assert button.available
    home_connection._notifications_subscribed = False
    assert not button.available
    home_connection._notifications_subscribed = True
    home_connection.controller.set_connected(False)
    assert not button.available
