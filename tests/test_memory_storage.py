"""Durability, isolation and lifecycle checks using Home Assistant's real Store."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import status_with
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import storage
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

from custom_components.svago.memory import MemoryStore
from custom_components.svago.memory_protocol import MemoryProfile


@pytest.fixture
async def memory_hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    yield hass
    await hass.async_block_till_done()
    await hass.async_stop(force=True)


def profile(head=5):
    return MemoryProfile.from_status(status_with(positions=(head, 6, 16, 28)))


# Generic legacy values exercise migration retention without bundled chair defaults.
LEGACY_PROFILES = {2: profile(7), 3: profile(9)}
LEGACY_RESTORE_ID = "restore_original_memory_3_0_1_15"


async def read_disk(hass, entry="entry_a"):
    return await Store(hass, 1, f"svago.memories.{entry}").async_load()


async def test_roundtrip_reload_isolation_and_entry_removal(memory_hass):
    a = MemoryStore(memory_hass, "entry_a")
    b = MemoryStore(memory_hass, "entry_b")
    await a.async_load()
    await b.async_load()
    assert a.get(1) is None
    await a.async_save(1, profile(1))
    await a.async_save(3, profile(3))
    await b.async_save(1, profile(2))
    await a.async_close()
    reloaded = MemoryStore(memory_hass, "entry_a")
    await reloaded.async_load()
    assert reloaded.get(1) == profile(1) and reloaded.get(3) == profile(3)
    assert reloaded.get(2) is None and b.get(1) == profile(2)
    await reloaded.async_close()
    await reloaded.async_remove()
    assert await read_disk(memory_hass) is None
    assert (await read_disk(memory_hass, "entry_b"))["profiles"]["1"] == profile(2).to_dict()


async def test_corrupt_slot_rejected_without_losing_other_valid_slots(memory_hass, caplog):
    invalid = profile().to_dict()
    invalid["positions"] = [0x82, 0, 0, 0]
    original = {"profiles": {"1": profile(1).to_dict(), "2": invalid, "4": profile(4).to_dict()}}
    await Store(memory_hass, 1, "svago.memories.entry_a").async_save(original)
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    assert store.get(1) == profile(1) and store.get(2) is None
    assert "Ignoring invalid Svago Memory 2" in caplog.text
    assert "Ignoring invalid Svago memory slot" in caplog.text
    await store.async_import_missing({2: profile(2), 3: profile(3)})
    assert store.get(2) is None and store.get(3) is None
    assert await read_disk(memory_hass) == original
    # An explicit Memory Set is still allowed to recover validated profiles.
    await store.async_save(3, profile(3))
    assert (await read_disk(memory_hass))["profiles"] == {
        "1": profile(1).to_dict(),
        "3": profile(3).to_dict(),
    }


@pytest.mark.parametrize("bad", ([], {"profiles": []}, {"unexpected": {}}))
async def test_malformed_envelope_loads_no_profiles_and_does_not_rewrite(memory_hass, caplog, bad):
    await Store(memory_hass, 1, "svago.memories.entry_a").async_save(bad)
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    assert all(store.get(slot) is None for slot in (1, 2, 3))
    assert "Ignoring invalid saved Svago memory data" in caplog.text
    await store.async_import_missing({2: profile(2), 3: profile(3)})
    assert all(store.get(slot) is None for slot in (1, 2, 3))
    assert await read_disk(memory_hass) == bad


@pytest.mark.parametrize("contents", ("{}", "{invalid JSON"))
async def test_ha_empty_or_quarantined_file_never_becomes_fresh_import(memory_hass, contents):
    path = Path(Store(memory_hass, 1, "svago.memories.entry_a").path)
    await memory_hass.async_add_executor_job(path.parent.mkdir, 0o755, True, True)
    await memory_hass.async_add_executor_job(path.write_text, contents)
    for _ in range(2):
        # A later reload must also recognize HA's .corrupt.* file after the
        # original malformed JSON has been moved out of the way.
        changed = Mock()
        store = MemoryStore(memory_hass, "entry_a", changed)
        await store.async_load()
        changed.reset_mock()
        persist = AsyncMock(wraps=store._persist)
        store._persist = persist
        await store.async_import_missing({2: profile(2), 3: profile(3)})
        assert all(store.get(slot) is None for slot in (1, 2, 3))
        persist.assert_not_awaited()
        changed.assert_not_called()
        await store.async_close()
    # Explicit Memory Set remains a recovery route even after quarantine.
    recovered = MemoryStore(memory_hass, "entry_a")
    await recovered.async_load()
    await recovered.async_save(2, profile(4))
    assert (await read_disk(memory_hass))["profiles"] == {"2": profile(4).to_dict()}


@pytest.mark.parametrize("operation", ("save", "import"))
async def test_swallowed_ha_write_error_does_not_publish_or_allow_stale_overwrite(
    memory_hass, monkeypatch, operation
):
    changed = Mock()
    store = MemoryStore(memory_hass, "entry_a", changed)
    await store.async_load()
    await store.async_save(1, profile(1))
    changed.reset_mock()
    writer = storage.write_utf8_file_atomic
    monkeypatch.setattr(
        storage, "write_utf8_file_atomic", Mock(side_effect=WriteError("disk full"))
    )
    with pytest.raises(HomeAssistantError, match="verified on disk"):
        if operation == "save":
            await store.async_save(1, profile(2))
        else:
            await store.async_import_missing({2: profile(2)})
    assert store.get(1) == profile(1)
    assert store.get(2) is None
    changed.assert_not_called()
    assert (await read_disk(memory_hass))["profiles"]["1"] == profile(1).to_dict()
    with pytest.raises(HomeAssistantError, match="not available"):
        await store.async_save(3, profile(3))
    # Explicit retry recovery reloads durable state before the next save.
    monkeypatch.setattr(storage, "write_utf8_file_atomic", writer)
    await store.async_load()
    await store.async_save(3, profile(3))
    assert set((await read_disk(memory_hass))["profiles"]) == {"1", "3"}


async def test_read_failure_does_not_become_an_empty_writable_store(memory_hass, monkeypatch):
    store = MemoryStore(memory_hass, "entry_a")
    monkeypatch.setattr(store._store, "async_load", AsyncMock(side_effect=OSError("denied")))
    with pytest.raises(OSError):
        await store.async_load()
    with pytest.raises(HomeAssistantError, match="not available"):
        await store.async_save(1, profile())
    assert await read_disk(memory_hass) is None


async def test_save_waits_for_commit_and_concurrent_slots_do_not_overwrite(memory_hass):
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    started, release = asyncio.Event(), asyncio.Event()
    persist = store._persist

    async def delayed(data):
        started.set()
        await release.wait()
        await persist(data)

    store._persist = delayed
    first = asyncio.create_task(store.async_save(1, profile(1)))
    await started.wait()
    second = asyncio.create_task(store.async_save(2, profile(2)))
    await asyncio.sleep(0)
    assert store.get(1) is None and not second.done()
    release.set()
    await asyncio.gather(first, second)
    assert set((await read_disk(memory_hass))["profiles"]) == {"1", "2"}


@pytest.mark.parametrize("operation", ("save", "import"))
async def test_cancelled_commit_finishes_before_later_save(memory_hass, operation):
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    started, release = asyncio.Event(), asyncio.Event()
    persist = store._persist

    async def delayed(data):
        started.set()
        await release.wait()
        await persist(data)

    store._persist = delayed
    first = asyncio.create_task(
        store.async_save(1, profile(1))
        if operation == "save"
        else store.async_import_missing({1: profile(1)})
    )
    await started.wait()
    first.cancel()
    second = asyncio.create_task(store.async_save(2, profile(2)))
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert store.get(1) == profile(1) and store.get(2) == profile(2)
    assert set((await read_disk(memory_hass))["profiles"]) == {"1", "2"}


@pytest.mark.parametrize("after_close", ("reload", "remove"))
async def test_unload_barrier_prevents_late_write_across_store_instances(memory_hass, after_close):
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    started, release = asyncio.Event(), asyncio.Event()
    persist = store._persist

    async def delayed(data):
        started.set()
        await release.wait()
        await persist(data)

    store._persist = delayed
    saving = asyncio.create_task(store.async_save(1, profile(1)))
    await started.wait()
    queued = asyncio.create_task(store.async_save(2, profile(2)))
    closing = asyncio.create_task(store.async_close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await saving
    with pytest.raises(HomeAssistantError, match="not available"):
        await queued
    await closing
    new = MemoryStore(memory_hass, "entry_a")
    if after_close == "reload":
        await new.async_load()
        assert new.get(1) == profile(1) and new.get(2) is None
        await new.async_save(3, profile(3))
        assert set((await read_disk(memory_hass))["profiles"]) == {"1", "3"}
    else:
        await new.async_remove()
        assert await read_disk(memory_hass) is None
    with pytest.raises(HomeAssistantError, match="not available"):
        await store.async_save(1, profile(4))


async def test_remove_prevents_later_file_recreation(memory_hass):
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    await store.async_save(1, profile(1))
    await store.async_remove()
    with pytest.raises(HomeAssistantError):
        await store.async_save(2, profile(2))
    assert store.get(1) is None and await read_disk(memory_hass) is None


async def test_stopping_does_not_queue_a_deferred_save(memory_hass):
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    previous = memory_hass.state
    memory_hass.state = CoreState.stopping
    try:
        with pytest.raises(HomeAssistantError, match="stopping"):
            await store.async_save(1, profile())
    finally:
        memory_hass.state = previous
    assert store.get(1) is None and await read_disk(memory_hass) is None


async def test_import_missing_is_single_durable_commit_and_preserves_existing_slots(memory_hass):
    changed = Mock()
    store = MemoryStore(memory_hass, "entry_a", changed)
    await store.async_load()
    await store.async_save(1, profile(1))
    changed.reset_mock()
    persist = AsyncMock(wraps=store._persist)
    store._persist = persist
    await store.async_import_missing({1: profile(4), 2: profile(2), 3: profile(3)})
    persist.assert_awaited_once()
    changed.assert_called_once_with()
    assert store.get(1) == profile(1)
    assert store.get(2) == profile(2) and store.get(3) == profile(3)
    await store.async_close()
    reloaded = MemoryStore(memory_hass, "entry_a")
    await reloaded.async_load()
    assert [reloaded.get(slot) for slot in (1, 2, 3)] == [profile(1), profile(2), profile(3)]


async def test_import_only_supplied_slots_and_never_replaces_later_memory_set(memory_hass):
    changed = Mock()
    store = MemoryStore(memory_hass, "entry_a", changed)
    await store.async_load()
    imported = {2: profile(2), 3: profile(3)}
    await store.async_import_missing(imported)
    assert store.get(1) is None
    await store.async_save(2, profile(4))  # A later Memory Set wins permanently.
    changed.reset_mock()
    persist = AsyncMock(wraps=store._persist)
    store._persist = persist
    await store.async_import_missing(imported)
    await store.async_import_missing({})
    persist.assert_not_awaited()
    changed.assert_not_called()
    assert store.get(2) == profile(4) and store.get(3) == profile(3)
    await store.async_close()
    reloaded = MemoryStore(memory_hass, "entry_a")
    await reloaded.async_load()
    await reloaded.async_import_missing(imported)
    assert reloaded.get(1) is None and reloaded.get(2) == profile(4)
    assert reloaded.get(3) == profile(3)


@pytest.mark.parametrize(
    "imported",
    (None, [], {2: profile(2), 4: profile(3)}, {2: profile(2), 3: {}}, {True: profile(1)}),
)
async def test_import_validates_whole_request_before_any_write(memory_hass, imported):
    changed = Mock()
    store = MemoryStore(memory_hass, "entry_a", changed)
    await store.async_load()
    changed.reset_mock()
    persist = AsyncMock(wraps=store._persist)
    store._persist = persist
    with pytest.raises(ValueError):
        await store.async_import_missing(imported)
    assert all(store.get(slot) is None for slot in (1, 2, 3))
    persist.assert_not_awaited()
    changed.assert_not_called()
    assert await read_disk(memory_hass) is None


@pytest.mark.parametrize("state", ("unloaded", "closed", "removed"))
async def test_import_requires_loaded_open_store(memory_hass, state):
    store = MemoryStore(memory_hass, "entry_a")
    if state != "unloaded":
        await store.async_load()
        if state == "closed":
            await store.async_close()
        else:
            await store.async_remove()
    with pytest.raises(HomeAssistantError, match="not available"):
        await store.async_import_missing({2: profile(2), 3: profile(3)})
    assert await read_disk(memory_hass) is None


async def test_original_memory_3_restored_once_and_later_custom_save_survives_restart(memory_hass):
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    for slot in (1, 2, 3):
        await store.async_save(slot, profile(slot))
    await store.async_restore_once(LEGACY_RESTORE_ID, {3: LEGACY_PROFILES[3]})
    assert [store.get(slot) for slot in (1, 2, 3)] == [profile(1), profile(2), LEGACY_PROFILES[3]]
    disk = await read_disk(memory_hass)
    assert disk["profiles"]["3"] == LEGACY_PROFILES[3].to_dict()
    assert disk["applied_migrations"] == [LEGACY_RESTORE_ID]
    # Changing a restored profile is permanent, even after setup runs again.
    await store.async_save(3, profile(4))
    await store.async_close()
    reloaded = MemoryStore(memory_hass, "entry_a")
    await reloaded.async_load()
    persist = AsyncMock(wraps=reloaded._persist)
    reloaded._persist = persist
    await reloaded.async_restore_once(LEGACY_RESTORE_ID, {3: LEGACY_PROFILES[3]})
    persist.assert_not_awaited()
    assert [reloaded.get(slot) for slot in (1, 2, 3)] == [profile(1), profile(2), profile(4)]
    assert (await read_disk(memory_hass))["applied_migrations"] == [LEGACY_RESTORE_ID]


async def test_restore_marks_even_identical_profile_without_chair_io(memory_hass):
    changed = Mock()
    store = MemoryStore(memory_hass, "entry_a", changed)
    await store.async_load()
    await store.async_import_missing(LEGACY_PROFILES)
    changed.reset_mock()
    persist = AsyncMock(wraps=store._persist)
    store._persist = persist
    await store.async_restore_once(LEGACY_RESTORE_ID, {3: LEGACY_PROFILES[3]})
    persist.assert_awaited_once()
    changed.assert_called_once_with()
    assert store.get(1) is None
    assert store.get(2) == LEGACY_PROFILES[2]
    assert store.get(3) == LEGACY_PROFILES[3]
    assert (await read_disk(memory_hass))["applied_migrations"] == [LEGACY_RESTORE_ID]


async def test_restore_failure_never_publishes_profile_or_completion_marker(
    memory_hass, monkeypatch
):
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    await store.async_save(3, profile(3))
    writer = storage.write_utf8_file_atomic
    monkeypatch.setattr(
        storage, "write_utf8_file_atomic", Mock(side_effect=WriteError("disk full"))
    )
    with pytest.raises(HomeAssistantError, match="verified on disk"):
        await store.async_restore_once(LEGACY_RESTORE_ID, {3: LEGACY_PROFILES[3]})
    assert store.get(3) == profile(3)
    assert await read_disk(memory_hass) == {"profiles": {"3": profile(3).to_dict()}}
    with pytest.raises(HomeAssistantError, match="not available"):
        await store.async_restore_once(LEGACY_RESTORE_ID, {3: LEGACY_PROFILES[3]})
    monkeypatch.setattr(storage, "write_utf8_file_atomic", writer)
    await store.async_load()
    await store.async_restore_once(LEGACY_RESTORE_ID, {3: LEGACY_PROFILES[3]})
    assert store.get(3) == LEGACY_PROFILES[3]
    assert (await read_disk(memory_hass))["applied_migrations"] == [LEGACY_RESTORE_ID]


async def test_cancelled_restore_completes_atomically_before_new_save(memory_hass):
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    await store.async_save(3, profile(3))
    started, release = asyncio.Event(), asyncio.Event()
    persist = store._persist

    async def delayed(data):
        started.set()
        await release.wait()
        await persist(data)

    store._persist = delayed
    restoring = asyncio.create_task(
        store.async_restore_once(LEGACY_RESTORE_ID, {3: LEGACY_PROFILES[3]})
    )
    await started.wait()
    restoring.cancel()
    saving = asyncio.create_task(store.async_save(3, profile(4)))
    await asyncio.sleep(0)
    assert not restoring.done() and not saving.done()
    assert store.get(3) == profile(3)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await restoring
    await saving
    assert store.get(3) == profile(4)
    assert (await read_disk(memory_hass))["applied_migrations"] == [LEGACY_RESTORE_ID]
    await store.async_restore_once(LEGACY_RESTORE_ID, {3: LEGACY_PROFILES[3]})
    assert store.get(3) == profile(4)


@pytest.mark.parametrize(
    "bad",
    (
        {"profiles": {"3": {}}},
        {"profiles": {"3": profile(3).to_dict()}, "applied_migrations": None},
        {"profiles": {"3": profile(3).to_dict()}, "applied_migrations": [True]},
        {"profiles": {"3": profile(3).to_dict()}, "applied_migrations": [[]]},
        {"profiles": {"3": profile(3).to_dict()}, "applied_migrations": ["one", "one"]},
    ),
)
async def test_restore_never_overwrites_malformed_storage(memory_hass, bad):
    await Store(memory_hass, 1, "svago.memories.entry_a").async_save(bad)
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    persist = AsyncMock(wraps=store._persist)
    store._persist = persist
    await store.async_restore_once(LEGACY_RESTORE_ID, {3: LEGACY_PROFILES[3]})
    persist.assert_not_awaited()
    assert await read_disk(memory_hass) == bad


@pytest.mark.parametrize("contents", ("{}", "{invalid JSON"))
async def test_restore_never_overwrites_empty_or_quarantined_storage(memory_hass, contents):
    path = Path(Store(memory_hass, 1, "svago.memories.entry_a").path)
    await memory_hass.async_add_executor_job(path.parent.mkdir, 0o755, True, True)
    await memory_hass.async_add_executor_job(path.write_text, contents)
    for _ in range(2):
        store = MemoryStore(memory_hass, "entry_a")
        await store.async_load()
        persist = AsyncMock(wraps=store._persist)
        store._persist = persist
        await store.async_restore_once(LEGACY_RESTORE_ID, {3: LEGACY_PROFILES[3]})
        persist.assert_not_awaited()
        assert store.get(3) is None
        await store.async_close()


async def test_legacy_profile_load_defaults_finite_without_rewriting_file(memory_hass):
    legacy = profile(4).to_dict()
    legacy.pop("heat_continuous")
    legacy.pop("massage_continuous")
    original = {"profiles": {"1": legacy}, "applied_migrations": [LEGACY_RESTORE_ID]}
    await Store(memory_hass, 1, "svago.memories.entry_a").async_save(original)
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    assert store.get(1) == profile(4)
    assert not store.get(1).heat_continuous
    assert not store.get(1).massage_continuous
    assert await read_disk(memory_hass) == original
    await store.async_save(2, profile(7))
    assert (await read_disk(memory_hass))["applied_migrations"] == [LEGACY_RESTORE_ID]


async def test_continuous_memory_choices_survive_storage_reload(memory_hass):
    saved = MemoryProfile.from_status(
        status_with(positions=(3, 4, 5, 6), heat=False, massage=0),
        heat_continuous=True,
        massage_continuous=True,
    )
    store = MemoryStore(memory_hass, "entry_a")
    await store.async_load()
    await store.async_save(2, saved)
    await store.async_close()
    restored = MemoryStore(memory_hass, "entry_a")
    await restored.async_load()
    assert restored.get(2) == saved
    assert restored.get(2).heat is False
    assert restored.get(2).massage == "Off"
    assert restored.get(2).heat_continuous is True
    assert restored.get(2).massage_continuous is True
