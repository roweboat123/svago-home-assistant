"""Use Bleak's real BlueZ MTU method with a simulated D-Bus response."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
from dbus_fast.constants import MessageType
from test_connection import Endpoint
from test_connection import transport as transport_fixture

from custom_components.svago import connection as module
from custom_components.svago.controller import ControlError

transport = transport_fixture


@pytest.fixture
def bluez(transport):
    connection, endpoint, connect = transport
    endpoint.write.max_write_without_response_size = 20
    endpoint.write.obj = ("/org/bluez/hci0/dev_test/service0018/char001e", {})
    backend = BleakClientBlueZDBus(
        "02:00:00:00:00:AB", disconnected_callback=None, timeout=20, bluez={}
    )
    backend.services = SimpleNamespace(characteristics={31: endpoint.write})
    endpoint._backend = backend
    issued_fds = []

    async def acquire(message):
        assert message.member == "AcquireWrite"
        assert message.path == endpoint.write.obj[0]
        assert message.body == [{}]
        descriptor = os.open(os.devnull, os.O_RDONLY)
        issued_fds.append(descriptor)
        return SimpleNamespace(
            message_type=MessageType.METHOD_RETURN,
            unix_fds=[descriptor],
            body=[0, 259],
        )

    backend._bus = SimpleNamespace(call=AsyncMock(side_effect=acquire))
    yield connection, endpoint, connect, backend, issued_fds
    for descriptor in issued_fds:
        try:
            os.close(descriptor)
        except OSError:
            pass


async def test_real_bluez_mtu_acquisition_allows_one_complete_command(bluez):
    connection, endpoint, _, backend, descriptors = bluez
    await connection.start(monitor=False)
    assert connection.controller.available
    assert endpoint.write.max_write_without_response_size == 20
    assert connection.diagnostics()["effective_command_capacity"] == 256
    assert connection.diagnostics()["verified_bluez_mtu"] == 259
    backend._bus.call.assert_awaited_once()
    endpoint.write_gatt_char.assert_not_awaited()
    with pytest.raises(OSError):
        os.fstat(descriptors[0])  # Bleak releases the AcquireWrite lock immediately.
    await connection.controller.stop_movement()
    assert endpoint.write_gatt_char.await_count == 1
    assert len(endpoint.write_gatt_char.await_args.args[1]) == 27
    assert endpoint.write_gatt_char.await_args.kwargs == {"response": False}
    await connection.close()


@pytest.mark.parametrize("mtu,works", [(23, False), (29, False), (30, True)])
async def test_negotiated_mtu_must_fit_the_complete_frame(bluez, monkeypatch, mtu, works):
    connection, endpoint, _, backend, _ = bluez
    monkeypatch.setattr(module, "WRITE_SIZE_TIMEOUT", 0.01)
    acquire = backend._bus.call.side_effect

    async def smaller(message):
        reply = await acquire(message)
        reply.body[1] = mtu
        return reply

    backend._bus.call.side_effect = smaller
    if works:
        await connection.start(monitor=False)
        assert connection._command_capacity() == 27
        await connection.close()
    else:
        with pytest.raises(ControlError, match=f"MTU check: negotiated {mtu} bytes"):
            await connection.start(monitor=False)
        assert not connection.controller.available
        endpoint.disconnect.assert_awaited_once()
    endpoint.write_gatt_char.assert_not_awaited()


async def test_non_bluez_backend_does_not_get_a_size_override(transport, monkeypatch):
    connection, endpoint, _ = transport
    monkeypatch.setattr(module, "WRITE_SIZE_TIMEOUT", 0.01)
    endpoint.write.max_write_without_response_size = 20
    endpoint.mtu_size = 259
    endpoint._backend = SimpleNamespace(mtu_size=259, _acquire_mtu=AsyncMock())
    with pytest.raises(ControlError, match="not a local BlueZ connection"):
        await connection.start(monitor=False)
    endpoint._backend._acquire_mtu.assert_not_awaited()
    endpoint.write_gatt_char.assert_not_awaited()


async def test_adequate_reported_capacity_does_not_acquire_mtu(bluez):
    connection, endpoint, _, backend, _ = bluez
    endpoint.write.max_write_without_response_size = 27
    await connection.start(monitor=False)
    backend._bus.call.assert_not_awaited()
    assert connection._command_capacity() == 27
    await connection.close()


@pytest.mark.parametrize("failure", ["rejected", "timeout", "invalid_mtu"])
async def test_failed_acquisition_does_not_authorize_writes(bluez, monkeypatch, failure):
    connection, endpoint, _, backend, _ = bluez
    monkeypatch.setattr(module, "WRITE_SIZE_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "MTU_TIMEOUT", 0.01)
    backend._mtu_size = 259  # A cached value cannot rescue a failed acquisition.

    async def fail():
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "invalid_mtu":
            backend._mtu_size = 999
            return
        raise RuntimeError("AcquireWrite rejected")

    monkeypatch.setattr(backend, "_acquire_mtu", fail)
    with pytest.raises(ControlError, match="MTU check: failed"):
        await connection.start(monitor=False)
    assert connection._command_capacity() == 0
    endpoint.write_gatt_char.assert_not_awaited()


async def test_property_update_can_succeed_after_acquisition_rejection(bluez, monkeypatch):
    connection, endpoint, _, backend, _ = bluez

    async def rejected():
        endpoint.write.max_write_without_response_size = 256
        raise RuntimeError("AcquireWrite unavailable")

    monkeypatch.setattr(backend, "_acquire_mtu", rejected)
    await connection.start(monitor=False)
    assert connection._command_capacity() == 256
    assert connection.diagnostics()["verified_bluez_mtu"] is None
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_disconnect_discards_acquired_size(bluez):
    connection, endpoint, _, _, _ = bluez
    await connection.start(monitor=False)
    connection._disconnected(endpoint)
    assert connection.diagnostics()["verified_bluez_mtu"] is None
    assert connection._command_capacity() == 20
    with pytest.raises(ControlError):
        await connection.controller.stop_movement()
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_reconnect_does_not_reuse_previous_mtu(bluez, monkeypatch):
    connection, endpoint, connect, _, _ = bluez
    await connection.start(monitor=False)
    await connection._drop()
    new = Endpoint()
    new.write.max_write_without_response_size = 20
    connect.return_value = new
    monkeypatch.setattr(module, "WRITE_SIZE_TIMEOUT", 0.01)
    with pytest.raises(ControlError, match="20 bytes"):
        await connection._connect()
    assert connection.diagnostics()["verified_bluez_mtu"] is None
    endpoint.write_gatt_char.assert_not_awaited()
    new.write_gatt_char.assert_not_awaited()


async def test_backend_replacement_invalidates_acquired_size(bluez):
    connection, endpoint, _, _, _ = bluez
    await connection.start(monitor=False)
    endpoint._backend = SimpleNamespace(mtu_size=259)
    with pytest.raises(ControlError, match="27-byte"):
        await connection.controller.stop_movement()
    endpoint.write_gatt_char.assert_not_awaited()
    await connection.close()


async def test_disconnect_during_acquisition_cannot_pass_setup(bluez, monkeypatch):
    connection, endpoint, _, backend, _ = bluez

    async def disconnect():
        endpoint.is_connected = False
        connection._disconnected(endpoint)
        backend._mtu_size = 259

    monkeypatch.setattr(backend, "_acquire_mtu", disconnect)
    with pytest.raises(ControlError, match="connection closed while checking command capacity"):
        await connection.start(monitor=False)
    assert connection.diagnostics()["verified_bluez_mtu"] is None
    endpoint.write_gatt_char.assert_not_awaited()


async def test_cancellation_during_acquisition_closes_connection(bluez, monkeypatch):
    connection, endpoint, _, backend, _ = bluez
    started = asyncio.Event()

    async def wait_forever():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(backend, "_acquire_mtu", wait_forever)
    task = asyncio.create_task(connection.start(monitor=False))
    await asyncio.wait_for(started.wait(), 0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    endpoint.disconnect.assert_awaited_once()
    endpoint.write_gatt_char.assert_not_awaited()
    assert connection.diagnostics()["verified_bluez_mtu"] is None
