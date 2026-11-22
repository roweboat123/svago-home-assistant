"""Durable timer choices never restore or resume an active chair session."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import status_with
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.storage import Store

from custom_components import svago
from custom_components.svago.connection import Connection
from custom_components.svago.controller import HEAT_RENEW_SECONDS, MASSAGE_RENEW_SECONDS
from custom_components.svago.preferences import TimerPreferences
from custom_components.svago.protocol import CONTINUOUS_HEAT_MINUTES, CONTINUOUS_MASSAGE_MINUTES


@pytest.fixture
async def preferences_hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    yield hass
    await hass.async_block_till_done()
    await hass.async_stop(force=True)


async def read_disk(hass, entry="entry_a"):
    return await Store(hass, 1, f"svago.timer_preferences.{entry}").async_load()


@pytest.fixture
async def persisted_chair(chair, preferences_hass):
    store = TimerPreferences(preferences_hass, "entry_a")
    await store.async_load()
    chair.controller._preferences_changed = store.update
    chair.controller._preferences_flush = store.async_flush
    yield chair, store
    await chair.controller.cancel_active()
    await store.async_close()


def test_longest_native_leases_leave_five_minutes_for_renewal():
    assert CONTINUOUS_MASSAGE_MINUTES == 45
    assert MASSAGE_RENEW_SECONDS == 40 * 60
    assert CONTINUOUS_HEAT_MINUTES == 180
    assert HEAT_RENEW_SECONDS == 175 * 60
    assert CONTINUOUS_MASSAGE_MINUTES * 60 - MASSAGE_RENEW_SECONDS == 300
    assert CONTINUOUS_HEAT_MINUTES * 60 - HEAT_RENEW_SECONDS == 300


async def test_new_store_defaults_finite_without_creating_a_file(preferences_hass):
    store = TimerPreferences(preferences_hass, "entry_a")
    await store.async_load()
    assert store.choices == {"heat": False, "massage": False}
    await store.async_close()
    assert await read_disk(preferences_hass) is None


async def test_roundtrip_isolated_per_entry_and_remove(preferences_hass):
    a = TimerPreferences(preferences_hass, "entry_a")
    b = TimerPreferences(preferences_hass, "entry_b")
    await a.async_load()
    await b.async_load()
    a.update({"heat": True, "massage": False})
    b.update({"heat": False, "massage": True})
    await asyncio.gather(a.async_close(), b.async_close())
    reloaded = TimerPreferences(preferences_hass, "entry_a")
    await reloaded.async_load()
    assert reloaded.choices == {"heat": True, "massage": False}
    assert (await read_disk(preferences_hass, "entry_b"))["continuous"] == b.choices
    snapshot = reloaded.choices
    snapshot["heat"] = False
    assert reloaded.choices["heat"] is True
    await reloaded.async_remove()
    reloaded.update({"heat": True, "massage": True})
    await reloaded.async_flush()
    assert await read_disk(preferences_hass) is None
    assert await read_disk(preferences_hass, "entry_b") is not None


@pytest.mark.parametrize(
    "bad", [[], {}, {"continuous": {}}, {"continuous": {"heat": 1, "massage": True}}]
)
async def test_invalid_data_is_not_replaced(preferences_hass, bad):
    await Store(preferences_hass, 1, "svago.timer_preferences.entry_a").async_save(bad)
    store = TimerPreferences(preferences_hass, "entry_a")
    with pytest.raises(HomeAssistantError, match="invalid"):
        await store.async_load()
    store.update({"heat": True, "massage": True})
    await store.async_close()
    assert await read_disk(preferences_hass) == bad


async def test_corrupt_file_stays_protected_after_ha_quarantines_it(preferences_hass):
    path = Path(Store(preferences_hass, 1, "svago.timer_preferences.entry_a").path)
    await preferences_hass.async_add_executor_job(path.parent.mkdir, 0o755, True, True)
    await preferences_hass.async_add_executor_job(path.write_text, "{invalid JSON")
    for _ in range(2):
        store = TimerPreferences(preferences_hass, "entry_a")
        with pytest.raises(HomeAssistantError, match="could not be read"):
            await store.async_load()
        store.update({"heat": True, "massage": True})
        await store.async_close()
        assert await read_disk(preferences_hass) is None


async def test_failed_load_prevents_connection_and_preserves_storage(monkeypatch):
    connection = SimpleNamespace(
        load_preferences=AsyncMock(side_effect=HomeAssistantError("Preferences unreadable")),
        start=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(svago, "Connection", Mock(return_value=connection))
    memories = Mock()
    monkeypatch.setattr(svago, "MemoryStore", memories)
    entry = SimpleNamespace(entry_id="entry_a", data={"address": "02:00:00:00:00:AB"})
    with pytest.raises(ConfigEntryNotReady, match="Preferences unreadable"):
        await svago.async_setup_entry(Mock(), entry)
    connection.start.assert_not_awaited()
    connection.close.assert_awaited_once()
    memories.assert_not_called()


async def test_latest_choice_wins_during_an_in_flight_commit(preferences_hass):
    store = TimerPreferences(preferences_hass, "entry_a")
    await store.async_load()
    entered, release = asyncio.Event(), asyncio.Event()
    persist = store._persist
    commits = []

    async def slow_first(choices):
        commits.append(dict(choices))
        if len(commits) == 1:
            entered.set()
            await release.wait()
        await persist(choices)

    store._persist = slow_first
    store.update({"heat": True, "massage": False})
    await entered.wait()
    store.update({"heat": False, "massage": True})
    store.update({"heat": True, "massage": True})
    release.set()
    await asyncio.gather(store.async_flush(), store.async_flush())
    assert commits == [{"heat": True, "massage": False}, {"heat": True, "massage": True}]
    assert (await read_disk(preferences_hass))["continuous"] == store.choices
    assert not store.diagnostics["pending"]


@pytest.mark.parametrize("remove", [False, True])
async def test_cancelled_close_or_remove_drains_write_and_rejects_late_updates(
    preferences_hass, remove
):
    store = TimerPreferences(preferences_hass, "entry_a")
    await store.async_load()
    entered, release = asyncio.Event(), asyncio.Event()
    persist = store._persist

    async def delayed(choices):
        entered.set()
        await release.wait()
        await persist(choices)

    store._persist = delayed
    store.update({"heat": True, "massage": False})
    await entered.wait()
    closing = asyncio.create_task(store.async_remove() if remove else store.async_close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    store.update({"heat": False, "massage": True})
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await asyncio.sleep(0)
    assert store._writer is None
    assert await read_disk(preferences_hass) == (
        None if remove else {"continuous": {"heat": True, "massage": False}}
    )


async def test_suppressed_store_failure_is_detected_and_retry_saves_choice(preferences_hass):
    store = TimerPreferences(preferences_hass, "entry_a")
    await store.async_load()
    save = store._store.async_save
    store._store.async_save = AsyncMock()  # HA can log an error without raising.
    store.update({"heat": True, "massage": True})
    with pytest.raises(HomeAssistantError, match="could not be saved"):
        await store.async_flush()
    assert store.diagnostics["pending"]
    assert await read_disk(preferences_hass) is None
    store._store.async_save = save
    store.update(store.choices)
    await store.async_flush()
    assert not store.diagnostics["pending"]
    assert store.diagnostics["last_error"] is None


async def test_connection_reload_restores_choices_without_ble_or_workers(preferences_hass):
    store = TimerPreferences(preferences_hass, "entry_a")
    await store.async_load()
    store.update({"heat": True, "massage": True})
    await store.async_close()
    connection = Connection(preferences_hass, "02:00:00:00:00:AB")
    connection.controller._write = AsyncMock()
    await connection.load_preferences("entry_a")
    controller = connection.controller
    assert controller.timer_preferences == {"heat": True, "massage": True}
    controller.set_connected(True)
    controller.accept_status(status_with(heat=True, massage=2))
    await asyncio.sleep(0)
    assert not controller.heat_renewal_diagnostics["active"]
    assert not controller.massage_renewal_diagnostics["active"]
    controller._write.assert_not_awaited()
    assert connection.diagnostics()["timer_preferences"]["choices"] == controller.timer_preferences
    await connection.close()
    assert (await read_disk(preferences_hass))["continuous"] == controller.timer_preferences


@pytest.mark.parametrize("failure", [OSError("memory close failed"), asyncio.CancelledError()])
async def test_connection_close_drains_preferences_when_memory_cleanup_fails(
    preferences_hass, failure
):
    connection = Connection(preferences_hass, "02:00:00:00:00:AB")
    await connection.load_preferences("entry_a")
    store = connection.preferences
    entered, release = asyncio.Event(), asyncio.Event()
    persist = store._persist

    async def delayed(choices):
        entered.set()
        await release.wait()
        await persist(choices)

    store._persist = delayed
    store.update({"heat": True, "massage": False})
    await entered.wait()
    connection.memories = SimpleNamespace(async_close=AsyncMock(side_effect=failure))
    closing = asyncio.create_task(connection.close())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(type(failure)):
        await closing
    assert store._writer is None
    assert store._closing
    assert (await read_disk(preferences_hass))["continuous"] == {"heat": True, "massage": False}


async def test_entry_removal_deletes_preferences_after_failed_save(preferences_hass):
    store = TimerPreferences(preferences_hass, "entry_a")
    await store.async_load()
    store.update({"heat": True, "massage": False})
    await store.async_flush()
    store._persist = AsyncMock(side_effect=OSError("disk failed"))
    store.update({"heat": True, "massage": True})
    with pytest.raises(HomeAssistantError):
        await store.async_flush()
    memories = SimpleNamespace(async_close=AsyncMock(), async_remove=AsyncMock())
    entry = SimpleNamespace(
        entry_id="entry_a", runtime_data=SimpleNamespace(memories=memories, preferences=store)
    )
    await svago.async_remove_entry(preferences_hass, entry)
    assert await read_disk(preferences_hass) is None
    assert store._writer is None and store._removed


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_selection_while_off_persists_without_activating(
    persisted_chair, preferences_hass, kind
):
    chair, store = persisted_chair
    chair.controller.accept_status(status_with(heat=False, massage=0))
    await getattr(chair.controller, f"set_{kind}_continuous")()
    assert chair.writes == []
    assert (await read_disk(preferences_hass))["continuous"][kind] is True
    assert not getattr(chair.controller, f"{kind}_renewal_diagnostics")["active"]


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_disconnect_keeps_choice_but_requires_explicit_new_start(persisted_chair, kind):
    chair, store = persisted_chair
    controller = chair.controller
    controller.restore_timer_preferences({"heat": True, "massage": True})
    store.update(controller.timer_preferences)
    await controller.set_heat(True) if kind == "heat" else await controller.set_massage("Gentle")
    tasks = controller._heat_tasks | controller._massage_tasks
    assert getattr(controller, f"{kind}_renewal_diagnostics")["active"]
    count = len(chair.writes)
    controller.set_connected(False)
    controller.set_connected(True)
    controller.accept_status(status_with(heat=True, massage=0 if kind == "heat" else 1))
    await asyncio.sleep(0)
    assert all(task.done() for task in tasks)
    assert controller.timer_preferences == {"heat": True, "massage": True}
    assert not getattr(controller, f"{kind}_renewal_diagnostics")["active"]
    assert len(chair.writes) == count
    await controller.set_heat(True) if kind == "heat" else await controller.set_massage("Gentle")
    assert getattr(controller, f"{kind}_renewal_diagnostics")["active"]


@pytest.mark.parametrize("kind", ["heat", "massage"])
async def test_off_retains_preference_on_disk_and_does_not_passively_resume(
    persisted_chair, preferences_hass, kind
):
    chair, store = persisted_chair
    controller = chair.controller
    await getattr(controller, f"set_{kind}_continuous")()
    if kind == "massage":
        await controller.set_massage("Gentle")
    await controller.set_heat(False) if kind == "heat" else await controller.set_massage("Off")
    assert not getattr(controller, f"{kind}_renewal_diagnostics")["active"]
    assert (await read_disk(preferences_hass))["continuous"][kind] is True
    count = len(chair.writes)
    controller.accept_status(status_with(heat=True, massage=2))
    await asyncio.sleep(0)
    assert len(chair.writes) == count
    assert not getattr(controller, f"{kind}_renewal_diagnostics")["active"]


@pytest.mark.parametrize("kind,minutes", [("heat", 120), ("massage", 30)])
async def test_finite_selection_persists_only_its_choice_off(
    persisted_chair, preferences_hass, kind, minutes
):
    chair, store = persisted_chair
    chair.controller.restore_timer_preferences({"heat": True, "massage": True})
    store.update(chair.controller.timer_preferences)
    await chair.controller.set_timer(kind, minutes)
    expected = {"heat": True, "massage": True, kind: False}
    assert (await read_disk(preferences_hass))["continuous"] == expected
    assert chair.controller.timer_preferences == expected


@pytest.mark.parametrize("action", ["memory", "preset"])
async def test_memory_or_native_preset_keeps_its_finite_timers_authoritative(
    persisted_chair, preferences_hass, action
):
    chair, store = persisted_chair
    chair.controller.restore_timer_preferences({"heat": True, "massage": True})
    store.update(chair.controller.timer_preferences)
    if action == "memory":
        chair.controller.reserve_memory_recall()
    else:
        await chair.controller.select_preset("zgr")
        assert chair.controller.status.timer_minutes("massage") == 15
        assert chair.controller.status.timer_minutes("heat") == 180
    await store.async_flush()
    assert (await read_disk(preferences_hass))["continuous"] == {"heat": False, "massage": False}


@pytest.mark.parametrize(
    "kind,changes", [("heat", {"8": 0x1C, "9": 0x20}), ("massage", {"6": 7, "7": 8})]
)
async def test_external_finite_timer_clears_and_persists_only_active_choice(
    persisted_chair, preferences_hass, kind, changes
):
    chair, store = persisted_chair
    await getattr(chair.controller, f"set_{kind}_continuous")()
    if kind == "massage":
        await chair.controller.set_massage("Gentle")
    raw = {str(offset): value for offset, value in enumerate(chair.controller.status.raw)}
    chair.controller.accept_status(status_with(**(raw | changes)))
    await store.async_flush()
    assert (await read_disk(preferences_hass))["continuous"][kind] is False
    assert not getattr(chair.controller, f"{kind}_renewal_diagnostics")["active"]
