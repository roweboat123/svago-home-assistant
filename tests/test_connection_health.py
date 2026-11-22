"""Live readiness and durable, read-only connection diagnostics."""

import asyncio
from datetime import UTC
from unittest.mock import Mock

import pytest
from conftest import TV_STATUS, status_with
from test_connection import Endpoint
from test_connection import transport as connection_transport

from custom_components.svago import connection as module
from custom_components.svago.controller import ControlError

transport = connection_transport


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("link_lost", "Disconnected"),
        ("controller_disconnected", "Disconnected"),
        ("not_subscribed", "Waiting for status"),
        ("no_status", "Waiting for status"),
        ("unusable_status", "Invalid status"),
        ("stale_status", "Status stale"),
        ("short_capacity", "Command unavailable"),
        ("setup_pending", "Connecting"),
    ],
)
async def test_readiness_requires_complete_live_command_path(transport, state, expected):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    assert connection.controls_ready and connection.connection_status == "Ready"
    if state == "link_lost":
        endpoint.is_connected = False  # The backend callback can be missed.
        assert connection.controller.available
    elif state == "controller_disconnected":
        connection.controller.set_connected(False)
    elif state == "not_subscribed":
        connection._notifications_subscribed = False
    elif state == "no_status":
        connection.controller.status = None
    elif state == "unusable_status":
        endpoint.callback(endpoint.notify, status_with(**{"19": 0}).raw)
    elif state == "stale_status":
        connection.controller._received_at -= 4
    elif state == "short_capacity":
        endpoint.write.max_write_without_response_size = 20
    else:
        connection._setup_complete = False
    assert not connection.controls_ready
    assert connection.connection_status == expected
    snapshot = connection.diagnostics()
    assert snapshot["available"] is False
    assert snapshot["connection_health"]["status"] == expected
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_fresh_notifications_are_not_ready_until_subscription_finishes(transport):
    connection, endpoint, _ = transport
    subscribed = asyncio.Event()
    finish = asyncio.Event()
    original = endpoint.start_notify

    async def subscribe(characteristic, callback):
        await original(characteristic, callback)
        subscribed.set()
        await finish.wait()

    endpoint.start_notify = subscribe
    task = asyncio.create_task(connection.start(monitor=False))
    await subscribed.wait()
    assert connection.controller.available
    assert not connection.controls_ready
    assert connection.connection_status == "Waiting for status"
    finish.set()
    await task
    assert connection.connection_status == "Ready"
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_identical_valid_replies_update_last_seen_and_survive_disconnect(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    first = connection.last_status_received
    assert first is not None and first.tzinfo is UTC
    previous_count = connection.diagnostics()["valid_status_count"]
    endpoint.callback(endpoint.notify, TV_STATUS)
    assert connection.last_status_received >= first
    assert connection.diagnostics()["valid_status_count"] == previous_count + 1
    last_seen = connection.last_status_received
    await connection._drop(reason="Test disconnect")
    health = connection.health_snapshot()
    assert connection.last_status_received == last_seen
    assert health["current_status_age_seconds"] is None
    assert health["last_status_received"] == last_seen.isoformat()
    assert health["last_disconnect_reason"] == "Test disconnect"
    assert health["disconnect_count"] == 1
    assert connection.connection_status == "Disconnected"
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()
    assert connection.connection_status == "Stopped"


async def test_invalid_reply_does_not_replace_last_usable_response_time(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    last_seen = connection.last_status_received
    endpoint.callback(endpoint.notify, status_with(**{"19": 0}).raw)
    assert connection.last_status_received == last_seen
    assert connection.connection_status == "Invalid status"
    await connection.close()


async def test_callback_and_cleanup_count_one_disconnect_and_keep_setup_failure(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    endpoint.is_connected = False
    connection._disconnected(endpoint)
    assert connection.diagnostics()["setup_stage"] == "disconnected"
    await connection._drop()
    assert connection.health_snapshot()["disconnect_count"] == 1
    assert connection.health_snapshot()["last_disconnect_reason"] == "Bluetooth link disconnected"
    await connection.close()


async def test_reconnect_counters_are_durable_and_current_status_starts_fresh(transport):
    connection, endpoint, connect = transport
    await connection.start(monitor=False)
    await connection._drop()
    next_endpoint = Endpoint()
    connect.return_value = next_endpoint
    await connection._connect()
    health = connection.health_snapshot()
    assert health["connect_attempts"] == 2
    assert health["successful_connections"] == 2
    assert health["disconnect_count"] == 1
    assert health["current_status_age_seconds"] < 1
    assert health["status_freshness_limit_seconds"] == 3
    assert connection.controls_ready
    endpoint.write_gatt_char.assert_not_awaited()
    next_endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_setup_failure_remains_readable_after_close(transport):
    connection, endpoint, _ = transport
    endpoint.services.get_service.return_value = None
    with pytest.raises(ControlError, match="FFF0"):
        await connection.start(monitor=False)
    await connection.close()
    snapshot = connection.diagnostics()
    assert snapshot["connection_health"]["status"] == "Stopped"
    assert snapshot["setup_stage"] == "checking the chair's Bluetooth services"
    assert "FFF0" in snapshot["last_setup_error"]
    assert snapshot["connection_health"]["connect_attempts"] == 1
    assert snapshot["connection_health"]["successful_connections"] == 0
    endpoint.write_gatt_char.assert_not_awaited()


async def test_write_failure_survives_success_and_history_rollover_without_identifiers(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    payload = connection.controller.status.command()
    endpoint.write_gatt_char.side_effect = OSError("adapter host 192.168.1.2 02:00:00:00:00:AB")
    with pytest.raises(ControlError, match="not retried"):
        await connection._write(payload)
    endpoint.write_gatt_char.side_effect = None
    await connection._write(payload)
    for _ in range(100):
        connection._record("later_event")
    health = connection.health_snapshot()
    assert health["last_write"]["outcome"] == "returned"
    failure = health["last_write_failure"]
    assert failure["outcome"] == "failed" and failure["error_type"] == "OSError"
    assert failure["command_hex"] == payload.hex().upper()
    assert failure["duration_seconds"] >= 0 and failure["completed_at"]
    assert "02:00:00:00:00:AB" not in str(health)
    assert "192.168.1.2" not in str(health)
    failure["outcome"] = "edited export"
    assert connection.health_snapshot()["last_write_failure"]["outcome"] == "failed"
    assert endpoint.write_gatt_char.await_count == 2
    await connection.close()


async def test_blocked_write_has_durable_reason_without_transport_send(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    endpoint.write.max_write_without_response_size = 20
    with pytest.raises(ControlError, match="27-byte"):
        await connection._write(connection.controller.status.command())
    trace = connection.health_snapshot()["last_write_failure"]
    assert trace["outcome"] == "blocked" and "27-byte" in trace["error"]
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_monitor_publishes_health_without_new_packets_or_writes(transport, monkeypatch):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    listener = Mock()
    connection.subscribe(listener)
    calls = 0

    async def sleep(_seconds):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await connection._maintain()
    assert listener.call_count == 3
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_monitor_detects_lost_backend_without_callback(transport, monkeypatch):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    endpoint.is_connected = False
    assert connection.controller.available
    dropped = asyncio.Event()
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        if seconds == 5:
            dropped.set()
            await asyncio.Event().wait()
        else:
            await original_sleep(0)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    task = asyncio.create_task(connection._maintain())
    await asyncio.wait_for(dropped.wait(), 1)
    assert not connection.controller.connected
    assert connection.health_snapshot()["last_reconnect_reason"] == "Disconnected"
    endpoint.disconnect.assert_awaited_once()
    endpoint.write_gatt_char.assert_not_awaited()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await connection.close()
