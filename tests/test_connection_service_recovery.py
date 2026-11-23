"""Recover incomplete cached services without sending or replaying chair commands."""

import asyncio
from unittest.mock import Mock

import pytest
from conftest import TV_STATUS
from test_connection import Endpoint
from test_connection import transport as connection_transport

from custom_components.svago import connection as module
from custom_components.svago.controller import ControlError
from custom_components.svago.protocol import NOTIFY_UUID, WRITE_UUID

transport = connection_transport


def incomplete_services(endpoint, missing="service"):
    """Model a connected backend whose service database is incomplete."""
    if missing == "service":
        endpoint.services.get_service.return_value = None
    elif missing in ("notify", "write"):
        endpoint.services.get_service.return_value.get_characteristic.side_effect = lambda uuid: {
            NOTIFY_UUID: None if missing == "notify" else endpoint.notify,
            WRITE_UUID: None if missing == "write" else endpoint.write,
        }[uuid]
    elif missing == "notify_property":
        endpoint.notify.properties = []
    else:
        endpoint.write.properties = ["write"]


@pytest.mark.parametrize(
    "missing", ["service", "notify", "write", "notify_property", "write_property"]
)
async def test_incomplete_services_get_one_fresh_discovery(transport, monkeypatch, missing):
    connection, stale, connect = transport
    incomplete_services(stale, missing)
    fresh = Endpoint()
    connect.side_effect = [stale, fresh]
    first_device, latest_device = object(), object()
    lookup = Mock(side_effect=[first_device, latest_device])
    monkeypatch.setattr(module.bluetooth, "async_ble_device_from_address", lookup)

    await connection.start(monitor=False)

    assert connection.controls_ready
    assert connect.await_count == 2
    assert connect.await_args_list[0].args[1] is first_device
    assert connect.await_args_list[0].kwargs["use_services_cache"] is True
    assert connect.await_args_list[1].args[1] is latest_device
    assert connect.await_args_list[1].kwargs["use_services_cache"] is False
    stale.clear_cache.assert_awaited_once()
    stale.disconnect.assert_awaited_once()
    fresh.clear_cache.assert_not_awaited()
    fresh.disconnect.assert_not_awaited()
    snapshot = connection.diagnostics()
    assert snapshot["service_discovery"]["cache_recovery_attempts"] == 1
    assert snapshot["service_discovery"]["last_cache_clear"] == "cleared"
    assert snapshot["service_discovery"]["last_missing_service_error"]
    assert snapshot["connection_health"]["connect_attempts"] == 1
    assert snapshot["connection_health"]["successful_connections"] == 1
    assert snapshot["last_setup_error"] is None
    current_status = connection.controller.status
    stale.is_connected = True  # A delayed old backend callback must not drop the new client.
    connection._disconnected(stale)
    assert connection.controls_ready
    assert connection.controller.status is current_status
    stale.write_gatt_char.assert_not_awaited()
    fresh.write_gatt_char.assert_not_awaited()
    await connection.close()


@pytest.mark.parametrize("outcome", ["unsupported", "exception", "timeout"])
async def test_cache_clear_failure_still_allows_uncached_discovery(transport, monkeypatch, outcome):
    connection, stale, connect = transport
    incomplete_services(stale)
    fresh = Endpoint()
    connect.side_effect = [stale, fresh]
    if outcome == "unsupported":
        stale.clear_cache.return_value = False
        expected = "not supported"
    elif outcome == "exception":
        stale.clear_cache.side_effect = OSError("adapter details must not enter diagnostics")
        expected = "failed (OSError)"
    else:

        async def never_finishes():
            await asyncio.Event().wait()

        stale.clear_cache.side_effect = never_finishes
        monkeypatch.setattr(module, "SERVICE_CACHE_TIMEOUT", 0.01)
        expected = "failed (TimeoutError)"

    await connection.start(monitor=False)

    assert connection.controls_ready
    assert connect.await_count == 2
    assert connect.await_args.kwargs["use_services_cache"] is False
    assert connection.diagnostics()["service_discovery"]["last_cache_clear"] == expected
    assert "adapter details" not in str(connection.diagnostics())
    stale.write_gatt_char.assert_not_awaited()
    fresh.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_cancel_during_cache_clear_disconnects_without_retry(transport):
    connection, stale, connect = transport
    incomplete_services(stale)
    clearing = asyncio.Event()

    async def clear_cache():
        clearing.set()
        await asyncio.Event().wait()

    stale.clear_cache.side_effect = clear_cache
    task = asyncio.create_task(connection.start(monitor=False))
    await asyncio.wait_for(clearing.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not connection.controls_ready
    connect.assert_awaited_once()
    stale.disconnect.assert_awaited_once()
    assert connection.diagnostics()["service_discovery"]["last_cache_clear"] == "cancelled"
    stale.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_close_during_cache_clear_does_not_open_another_connection(transport):
    connection, stale, connect = transport
    incomplete_services(stale)
    clearing, finish_clear = asyncio.Event(), asyncio.Event()

    async def clear_cache():
        clearing.set()
        await finish_clear.wait()
        return True

    stale.clear_cache.side_effect = clear_cache
    task = asyncio.create_task(connection.start(monitor=False))
    await asyncio.wait_for(clearing.wait(), 1)
    await connection.close()
    finish_clear.set()
    with pytest.raises((ControlError, asyncio.CancelledError)):
        await task

    assert connection.connection_status == "Stopped"
    connect.assert_awaited_once()
    stale.disconnect.assert_awaited_once()
    stale.write_gatt_char.assert_not_awaited()


async def test_repair_requires_a_new_advertised_device(transport, monkeypatch):
    connection, stale, connect = transport
    incomplete_services(stale)
    monkeypatch.setattr(
        module.bluetooth, "async_ble_device_from_address", Mock(side_effect=[object(), None])
    )

    with pytest.raises(ControlError, match="Home Assistant has not detected"):
        await connection.start(monitor=False)

    connect.assert_awaited_once()
    stale.clear_cache.assert_awaited_once()
    stale.disconnect.assert_awaited_once()
    stale.write_gatt_char.assert_not_awaited()
    assert not connection.controls_ready
    await connection.close()


async def test_disconnect_during_cache_clear_does_not_skip_new_status_wait(transport):
    connection, stale, connect = transport
    incomplete_services(stale)
    fresh = Endpoint()
    connect.side_effect = [stale, fresh]
    subscribed = asyncio.Event()

    async def clear_cache():
        stale.is_connected = False
        connection._disconnected(stale)
        return True

    async def subscribe(characteristic, callback):
        fresh.callback = callback
        subscribed.set()

    stale.clear_cache.side_effect = clear_cache
    fresh.start_notify = subscribe
    task = asyncio.create_task(connection.start(monitor=False))
    try:
        await asyncio.wait_for(subscribed.wait(), 1)
        assert not task.done()
        assert not connection.controls_ready
        fresh.callback(fresh.notify, bytearray(TV_STATUS))
        await asyncio.wait_for(task, 1)
        assert connection.controls_ready
        stale.write_gatt_char.assert_not_awaited()
        fresh.write_gatt_char.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await connection.close()


async def test_monitor_recovers_after_power_loss_and_incomplete_services(transport, monkeypatch):
    connection, old, connect = transport
    ticks, advance = asyncio.Queue(), asyncio.Queue()

    async def controlled_sleep(seconds):
        await ticks.put(seconds)
        await advance.get()

    monkeypatch.setattr(module.asyncio, "sleep", controlled_sleep)
    await connection.start()
    assert await asyncio.wait_for(ticks.get(), 1) == 1
    old.write_gatt_char.side_effect = OSError("chair powered off")
    with pytest.raises(ControlError, match="not retried"):
        await connection.controller.stop_movement()
    old.is_connected = False  # Also cover a backend that misses its disconnect callback.
    stale, fresh = Endpoint(), Endpoint()
    incomplete_services(stale)
    connect.side_effect = [RuntimeError("chair is still off"), stale, fresh]

    await advance.put(None)
    assert await asyncio.wait_for(ticks.get(), 1) == 5
    assert not connection.controller.connected
    await advance.put(None)  # First reconnect fails while the chair is off.
    assert await asyncio.wait_for(ticks.get(), 1) == 1
    assert not connection.controls_ready
    await advance.put(None)
    assert await asyncio.wait_for(ticks.get(), 1) == 5
    await advance.put(None)  # Chair returns with stale cached services, then fresh services.
    assert await asyncio.wait_for(ticks.get(), 1) == 1

    assert connection.controls_ready
    assert connection.health_snapshot()["connect_attempts"] == 3
    assert connection.health_snapshot()["successful_connections"] == 2
    assert connect.await_count == 4
    assert connect.await_args.kwargs["use_services_cache"] is False
    old.write_gatt_char.assert_awaited_once()
    stale.write_gatt_char.assert_not_awaited()
    fresh.write_gatt_char.assert_not_awaited()
    assert connection._monitor is not None and not connection._monitor.done()
    await connection.close()
