"""Retired-control cleanup against Home Assistant's actual entity registry."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.svago.entity_migration import async_remove_retired_entities

ADDRESS = "02:00:00:00:00:AB"


@pytest.fixture
async def registry_hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entries = {}
    hass.config_entries = SimpleNamespace(
        async_get_entry=entries.get, async_wait_initialized=AsyncMock()
    )
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    yield hass, er.async_get(hass), entries
    await hass.async_block_till_done()
    await hass.async_stop(force=True)


def entry(entries, entry_id, address=ADDRESS):
    value = SimpleNamespace(
        entry_id=entry_id,
        data={CONF_ADDRESS: address},
        pref_disable_new_entities=False,
        disabled_by=None,
    )
    entries[entry_id] = value
    return value


async def test_remove_only_retired_controls_for_this_entry_and_address(registry_hass):
    hass, registry, entries = registry_hass
    selected = entry(entries, "selected")
    other = entry(entries, "other")

    def add(key, *, owner=selected, domain="button", platform="svago", address=ADDRESS):
        return registry.async_get_or_create(
            domain, platform, f"{address}_{key}", config_entry=owner
        )

    obsolete = [
        add(key)
        for key in (
            "seat_horizontal",
            "seat_vertical",
            "memory_set_1",
            "memory_set_2",
            "memory_set_3",
        )
    ]
    obsolete.extend(add(f"memory_set_{slot}", domain="switch") for slot in (1, 2, 3))
    # A renamed entity must still be removed by its integration-owned identity.
    renamed = registry.async_update_entity(
        obsolete[0].entity_id, new_entity_id="button.my_old_seat_control"
    )
    obsolete[0] = renamed
    preserved = [
        add("seat_position", domain="number"),
        add("memory_1"),
        add("memory_2"),
        add("memory_3"),
        add("set_memory", domain="switch"),
        add("seat_vertical", domain="switch"),
        add("memory_set_1", domain="switch", platform="another_integration"),
        add("memory_set_2", domain="switch", address="AA:BB:CC:DD:EE:FF"),
        add("heat", domain="switch"),
        add("seat_vertical", platform="another_integration"),
        add("seat_horizontal", address="AA:BB:CC:DD:EE:FF"),
        add("seat_vertical", owner=other, address="11:22:33:44:55:66"),
    ]
    # Transfer one exact matching retired identity to another config entry;
    # cleanup must respect its current owner even when address and key match.
    moved = registry.async_update_entity(obsolete[-1].entity_id, config_entry_id=other.entry_id)
    preserved.append(moved)
    obsolete.pop()

    async_remove_retired_entities(hass, selected)
    assert all(registry.async_get(item.entity_id) is None for item in obsolete)
    assert all(registry.async_get(item.entity_id) == item for item in preserved)
    assert entries == {"selected": selected, "other": other}

    # Reloading and fresh installations with no old controls are harmless.
    async_remove_retired_entities(hass, selected)
    assert all(registry.async_get(item.entity_id) == item for item in preserved)


async def test_fresh_entry_has_nothing_to_remove(registry_hass):
    hass, registry, entries = registry_hass
    selected = entry(entries, "fresh")
    async_remove_retired_entities(hass, selected)
    assert not registry.entities
    assert not registry.deleted_entities
