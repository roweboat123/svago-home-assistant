"""Exercise the real integration connection code with a simulated BLE endpoint."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import TV_STATUS, status_with

from custom_components.svago import connection as module
from custom_components.svago.controller import ControlError
from custom_components.svago.protocol import NOTIFY_UUID, WRITE_UUID


class Endpoint:
    def __init__(self):
        self.is_connected = True
        self.notify = SimpleNamespace(properties=["notify"])
        self.write = SimpleNamespace(
            properties=["write-without-response"], max_write_without_response_size=256
        )
        self.services = Mock()
        service = self.services.get_service.return_value
        service.get_characteristic.side_effect = lambda uuid: {
            NOTIFY_UUID: self.notify,
            WRITE_UUID: self.write,
        }[uuid]
        self.write_gatt_char = AsyncMock()
        self.clear_cache = AsyncMock(return_value=True)
        self.disconnect = AsyncMock(side_effect=self._disconnect)
        self.callback = None

    def _disconnect(self):
        self.is_connected = False

    async def start_notify(self, characteristic, callback):
        assert characteristic is self.notify
        self.callback = callback
        callback(characteristic, bytearray(TV_STATUS[:20]))
        callback(characteristic, bytearray(TV_STATUS[20:]))


@pytest.fixture
def transport(monkeypatch):
    endpoint = Endpoint()
    connect = AsyncMock(return_value=endpoint)
    monkeypatch.setattr(module, "establish_connection", connect)
    monkeypatch.setattr(
        module.bluetooth, "async_ble_device_from_address", Mock(return_value=object())
    )
    hass = SimpleNamespace(
        async_create_background_task=lambda task, name: asyncio.create_task(task)
    )
    connection = module.Connection(hass, "02:00:00:00:00:AB")
    return connection, endpoint, connect


async def test_setup_subscribes_without_any_control_writes_and_unloads(transport):
    connection, endpoint, _ = transport
    await connection.start()
    assert connection.controller.available
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()
    endpoint.disconnect.assert_awaited_once()
    assert not connection.controller.available
    assert connection._monitor is None


async def test_write_uses_fff2_without_response(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    await connection.controller.stop_movement()
    args = endpoint.write_gatt_char.await_args
    assert args.args[0] is endpoint.write
    assert len(args.args[1]) == 27
    assert args.kwargs == {"response": False}
    await connection.close()


async def test_missing_service_disconnects_cleanly_without_writes(transport):
    connection, endpoint, connect = transport
    retry = Endpoint()
    endpoint.services.get_service.return_value = None
    retry.services.get_service.return_value = None
    connect.side_effect = [endpoint, retry]
    with pytest.raises(ControlError, match="FFF0") as error:
        await connection.start(monitor=False)
    assert str(error.value).startswith("Check that the Motion Mastery Bluetooth remote is off. ")
    assert connection.diagnostics()["last_setup_error"] == (
        "Failed while checking the chair's Bluetooth services: "
        "The expected Svago service FFF0 is absent after fresh service discovery"
    )
    assert not connection.controller.available
    endpoint.disconnect.assert_awaited_once()
    retry.disconnect.assert_awaited_once()
    assert connect.await_count == 2
    endpoint.clear_cache.assert_awaited_once()
    retry.clear_cache.assert_not_awaited()
    endpoint.write_gatt_char.assert_not_awaited()
    retry.write_gatt_char.assert_not_awaited()


async def test_reconnect_gets_fresh_client_and_ignores_old_notifications(transport):
    connection, old, connect = transport
    await connection.start(monitor=False)
    await connection._drop()
    newer = Endpoint()
    connect.return_value = newer
    await connection._connect()
    current_status = connection.controller.status
    old.is_connected = True  # Simulate a late old callback, even if backend reports connected.
    old.callback(old.notify, bytearray(TV_STATUS))
    connection._disconnected(old)
    assert connection.controller.available
    assert connection.controller.status is current_status
    old.write_gatt_char.assert_not_awaited()
    newer.write_gatt_char.assert_not_awaited()
    assert connect.await_count == 2
    await connection.close()


async def test_size_guard_never_splits_a_command(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    endpoint.write.max_write_without_response_size = 20
    with pytest.raises(ControlError, match="27-byte"):
        await connection.controller.stop_movement()
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_transport_failure_is_not_replayed(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    endpoint.write_gatt_char.side_effect = RuntimeError("link lost")
    with pytest.raises(ControlError, match="not retried"):
        await connection.controller.stop_movement()
    assert endpoint.write_gatt_char.await_count == 1
    await connection.close()


async def test_diagnostics_retains_exact_frames_after_disconnect_without_writing(transport):
    from custom_components.svago.diagnostics import async_get_config_entry_diagnostics

    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    await connection.controller.stop_movement()
    sent = endpoint.write_gatt_char.await_args.args[1].hex().upper()
    await connection.close()
    before = endpoint.write_gatt_char.await_count
    result = await async_get_config_entry_diagnostics(
        None, SimpleNamespace(runtime_data=connection)
    )
    assert endpoint.write_gatt_char.await_count == before
    assert result["version"] == "1.1.0rc2"
    assert result["last_status_hex"] == TV_STATUS.hex().upper()
    assert result["connected"] is False
    assert any(
        e.get("hex") == sent and e["event"] == "FFF2_write_attempt" for e in result["history"]
    )
    assert "02:00:00:00:00:AB" not in str(result)


async def test_diagnostic_history_has_a_fixed_memory_bound(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    for _ in range(200):
        connection._record("test_event")
    assert len(connection.diagnostics()["history"]) == 64
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_home_diagnostics_are_read_only_and_detached(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    connection.controller.last_home_request = {
        "outcome": "reported_neutral",
        "reports": [{"positions": [0, 0, 0, 0]}],
    }
    result = connection.diagnostics()["last_home_request"]
    result["reports"][0]["positions"][0] = 10
    assert connection.controller.last_home_request["reports"][0]["positions"] == [0, 0, 0, 0]
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_heat_trace_survives_history_rollover_without_writes(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)

    # Heat is already on, but each write still needs its own fresh report.
    async def report_after_write(characteristic, payload, **kwargs):
        endpoint.callback(endpoint.notify, status_with(heat=True, **{"5": payload[5]}).raw)

    endpoint.write_gatt_char.side_effect = report_after_write
    await connection.controller.set_heat(True)
    for _ in range(100):
        connection._record("later_event")
    count = endpoint.write_gatt_char.await_count
    trace = connection.diagnostics()["last_heat_request"]
    assert trace["outcome"] == "confirmed" and trace["requested"] is True
    assert trace["command_hex"] == endpoint.write_gatt_char.await_args.args[1].hex().upper()
    trace["outcome"] = "changed snapshot"
    assert connection.diagnostics()["last_heat_request"]["outcome"] == "confirmed"
    assert endpoint.write_gatt_char.await_count == count
    activation = connection.diagnostics()["last_activation"]
    assert activation["outcome"] == "acknowledged"
    activation["outcome"] = "changed snapshot"
    assert connection.diagnostics()["last_activation"]["outcome"] == "acknowledged"
    await connection.close()


async def test_diagnostics_before_setup_has_no_connection_side_effects():
    from custom_components.svago.diagnostics import async_get_config_entry_diagnostics

    assert await async_get_config_entry_diagnostics(None, SimpleNamespace()) == {
        "version": "1.1.0rc2",
        "connection_initialized": False,
    }


async def test_position_trace_survives_rollover_and_export_is_independent(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    await connection.controller.set_position(0, 5)  # Already at target, no write.
    for _ in range(100):
        connection._record("later_event")
    trace = connection.diagnostics()["position_requests"][-1]
    assert trace["outcome"] == "target_reached"
    trace["reports"][0]["positions"][0] = 99
    assert connection.diagnostics()["position_requests"][-1]["reports"][0]["positions"][0] == 5
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_tap_trace_survives_rollover_and_export_is_independent(transport):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)

    async def acknowledge_activation(_characteristic, payload, **_kwargs):
        if payload[5] == 0x11:
            endpoint.callback(endpoint.notify, status_with(**{"5": 0x11}).raw)

    endpoint.write_gatt_char.side_effect = acknowledge_activation
    await connection.controller.step_position(0, 1)
    for _ in range(100):
        connection._record("later_event")
    count = endpoint.write_gatt_char.await_count
    trace = connection.diagnostics()["tap_requests"][-1]
    assert trace["outcome"] == "pulse_completed"
    assert trace["feedback_changed"] is False
    assert trace["direction_writes"] == count - 2  # Activation and final release.
    assert trace["stop"]["command_hex"] == endpoint.write_gatt_char.await_args.args[1].hex().upper()
    trace["direction_write_log"][0]["outcome"] = "changed snapshot"
    trace["positions_before"][0] = 99
    trace["stop"]["outcome"] = "changed snapshot"
    exported = connection.diagnostics()["tap_requests"][-1]
    assert exported["direction_write_log"][0]["outcome"] == "write_returned"
    assert exported["positions_before"][0] == 5
    assert exported["stop"]["outcome"] == "write_returned"
    assert endpoint.write_gatt_char.await_count == count
    await connection.close()


async def test_setup_error_identifies_missing_advertisement(transport, monkeypatch):
    connection, endpoint, connect = transport
    monkeypatch.setattr(module.bluetooth, "async_ble_device_from_address", Mock(return_value=None))
    with pytest.raises(ControlError, match="finding the chair: Home Assistant has not detected"):
        await connection.start(monitor=False)
    connect.assert_not_awaited()
    endpoint.write_gatt_char.assert_not_awaited()


async def test_setup_error_keeps_underlying_connection_failure(transport):
    connection, endpoint, connect = transport
    connect.side_effect = RuntimeError("No available connection slot")
    with pytest.raises(
        ControlError, match="opening the Bluetooth connection: No available connection slot"
    ):
        await connection.start(monitor=False)
    endpoint.write_gatt_char.assert_not_awaited()


async def test_setup_error_identifies_subscription_failure(transport):
    connection, endpoint, _ = transport
    endpoint.start_notify = AsyncMock(
        side_effect=PermissionError("Notification subscription rejected")
    )
    with pytest.raises(
        ControlError,
        match="enabling chair status notifications: Notification subscription rejected",
    ):
        await connection.start(monitor=False)
    endpoint.disconnect.assert_awaited_once()
    endpoint.write_gatt_char.assert_not_awaited()


@pytest.mark.parametrize("case", ["silent", "invalid", "unknown_state"])
async def test_setup_distinguishes_status_failure_reasons(transport, monkeypatch, case):
    from conftest import status_with

    connection, endpoint, _ = transport
    monkeypatch.setattr(module, "STATUS_TIMEOUT", 0.01)

    async def subscribe(characteristic, callback):
        if case == "invalid":
            callback(characteristic, b"not a chair status")
        elif case == "unknown_state":
            callback(characteristic, status_with(**{"19": 0}).raw)

    endpoint.start_notify = subscribe
    expected = {
        "silent": "no chair status notifications",
        "invalid": "Received 1 notifications",
        "unknown_state": "unrecognized heat or massage values .*heat=0x00",
    }
    with pytest.raises(ControlError, match=expected[case]):
        await connection.start(monitor=False)
    result = connection.diagnostics()
    assert result["setup_stage"] == "waiting for chair status"
    assert result["last_setup_error"]
    endpoint.write_gatt_char.assert_not_awaited()


async def test_setup_reports_actual_write_capacity_without_sending(transport, monkeypatch):
    connection, endpoint, _ = transport
    monkeypatch.setattr(module, "WRITE_SIZE_TIMEOUT", 0.01)
    endpoint.write.max_write_without_response_size = 20
    with pytest.raises(ControlError, match="checking Bluetooth command capacity:.*20 bytes"):
        await connection.start(monitor=False)
    endpoint.write_gatt_char.assert_not_awaited()
    assert connection.diagnostics()["last_status_hex"] == TV_STATUS.hex().upper()


async def test_disconnection_during_setup_is_not_masked_as_status_timeout(transport):
    connection, endpoint, _ = transport

    async def disconnect_during_subscribe(characteristic, callback):
        endpoint.is_connected = False
        connection._disconnected(endpoint)

    endpoint.start_notify = disconnect_during_subscribe
    with pytest.raises(ControlError, match="Bluetooth connection closed"):
        await asyncio.wait_for(connection.start(monitor=False), timeout=0.5)
    endpoint.write_gatt_char.assert_not_awaited()


@pytest.mark.parametrize("kind", ["massage", "heat"])
async def test_continuous_selection_diagnostics_and_unload_are_read_only(transport, kind):
    connection, endpoint, _ = transport
    await connection.start(monitor=False)
    endpoint.callback(endpoint.notify, status_with(massage=0, heat=False).raw)
    await getattr(connection.controller, f"set_{kind}_continuous")()
    before = endpoint.write_gatt_char.await_count
    trace = connection.diagnostics()[f"{kind}_renewal"]
    assert trace["selected"] is True
    assert trace["active"] is False
    assert trace["outcome"] == "armed"
    trace["outcome"] = "edited export"
    assert connection.diagnostics()[f"{kind}_renewal"]["outcome"] == "armed"
    assert endpoint.write_gatt_char.await_count == before == 0
    await connection.close()
    assert connection.diagnostics()[f"{kind}_renewal"]["selected"] is True
    assert connection.diagnostics()[f"{kind}_renewal"]["active"] is False
    assert endpoint.write_gatt_char.await_count == 0
