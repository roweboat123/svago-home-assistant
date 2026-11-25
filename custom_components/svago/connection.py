"""Persistent notification connection using Home Assistant's Bluetooth adapters."""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime

from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .activity import ActionActivity
from .const import VERSION
from .controller import CommandSuperseded, ControlError, Controller
from .memory import MemoryStore
from .memory_protocol import validate_slot
from .preferences import TimerPreferences
from .protocol import FRAME_SIZE, NOTIFY_UUID, SERVICE_UUID, WRITE_UUID, FrameAssembler

_LOGGER = logging.getLogger(__name__)
STATUS_TIMEOUT = 15
WRITE_SIZE_TIMEOUT = 10
MTU_TIMEOUT = 10
SERVICE_CACHE_TIMEOUT = 10


class Connection:
    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self.hass = hass
        self.address = address
        self.listeners: set[Callable[[], None]] = set()
        self.controller = Controller(
            self._write,
            self.notify_listeners,
            preferences_changed=self._timer_preferences_changed,
            preferences_flush=self._flush_timer_preferences,
        )
        self.memories: MemoryStore | None = None
        self.preferences: TimerPreferences | None = None
        self._memory_save_armed = False
        self._saving_memory_slot: int | None = None
        self._memory_arm_epoch = 0
        self._client = None
        self._write_char = None
        self._assembler = FrameAssembler()
        self._ready = asyncio.Event()
        self._closed = False
        self._monitor: asyncio.Task | None = None
        self._started_at = time.monotonic()
        self._history = deque(maxlen=64)
        self._last_status: str | None = None
        self._last_status_at: float | None = None
        self._setup_stage = "not started"
        self._last_setup_error: str | None = None
        self._notification_count = 0
        self._valid_status_count = 0
        self._bluetooth_backend: str | None = None
        self._mtu_result = "not checked"
        self._bluez_mtu: int | None = None
        self._mtu_client = None
        self._mtu_backend = None
        self._connecting = False
        self._setup_complete = False
        self._notifications_subscribed = False
        self._connect_attempts = 0
        self._successful_connections = 0
        self._disconnect_count = 0
        self._session_had_link = False
        self._last_attempt_at: datetime | None = None
        self._last_connected_at: datetime | None = None
        self._last_disconnected_at: datetime | None = None
        self._last_disconnect_reason: str | None = None
        self._last_reconnect_reason: str | None = None
        self._last_status_received: datetime | None = None
        self._write_count = 0
        self._last_write: dict | None = None
        self._last_write_failure: dict | None = None
        self._service_cache_recoveries = 0
        self._last_cache_clear: str | None = None
        self._last_missing_service_error: str | None = None
        self.activity = ActionActivity(self.notify_listeners)

    async def load_preferences(self, entry_id: str) -> None:
        self.preferences = TimerPreferences(self.hass, entry_id, self.notify_listeners)
        await self.preferences.async_load()
        self.controller.restore_timer_preferences(self.preferences.choices)

    def _timer_preferences_changed(self, choices: dict[str, bool]) -> None:
        if self.preferences is not None:
            self.preferences.update(choices)

    async def _flush_timer_preferences(self) -> None:
        if self.preferences is not None:
            try:
                await self.preferences.async_flush()
            except HomeAssistantError as exc:
                raise ControlError(str(exc)) from exc

    @property
    def bluetooth_connected(self) -> bool:
        """Transport state, independent of parsed status or controller intent."""
        return bool(self._client is not None and self._client.is_connected)

    @property
    def connection_status(self) -> str:
        """Describe the live path to the chair without sending a probe."""
        if self._closed:
            return "Stopped"
        if not self.bluetooth_connected:
            return "Connecting" if self._connecting else "Disconnected"
        if not self.controller.connected:
            return "Waiting for status" if self._connecting else "Disconnected"
        if not self._notifications_subscribed or self.controller.status is None:
            return "Waiting for status"
        if not self.controller.status.usable:
            return "Invalid status"
        if not self.controller.available:
            return "Status stale"
        if self._command_capacity() < FRAME_SIZE:
            return "Command unavailable"
        return "Ready" if self._setup_complete else "Connecting"

    @property
    def controls_ready(self) -> bool:
        return self.connection_status == "Ready"

    @property
    def last_status_received(self) -> datetime | None:
        """Last usable chair reply, retained so a disconnected chair has a last-seen time."""
        return self._last_status_received

    @property
    def wired_controller_power_estimate(self) -> dict:
        """Expose command-derived power consistently in the UI and diagnostic export."""
        snapshot = dict(self.controller.wired_controller_power_estimate)
        if not self.controls_ready:
            snapshot["state"] = None
            snapshot["basis"] = "Connection is not ready"
        return snapshot

    def health_snapshot(self) -> dict:
        """Current health and durable transport history; no Bluetooth commands."""
        return {
            "status": self.connection_status,
            "controls_ready": self.controls_ready,
            "bluetooth_connected": self.bluetooth_connected,
            "controller_connected": self.controller.connected,
            "notifications_subscribed": self._notifications_subscribed,
            "setup_complete": self._setup_complete,
            "controls_locked": self.controls_locked,
            "memory_save_armed": self._memory_save_armed,
            "saving_memory_slot": self._saving_memory_slot,
            "status_usable": bool(self.controller.status and self.controller.status.usable),
            "current_status_age_seconds": self.controller.status_age_seconds,
            "status_freshness_limit_seconds": self.controller.status_freshness_limit,
            "last_status_received": (
                self._last_status_received.isoformat() if self._last_status_received else None
            ),
            "effective_command_capacity": self._command_capacity(),
            "connect_attempts": self._connect_attempts,
            "successful_connections": self._successful_connections,
            "disconnect_count": self._disconnect_count,
            "last_connect_attempt": self._last_attempt_at.isoformat()
            if self._last_attempt_at
            else None,
            "last_connected": self._last_connected_at.isoformat()
            if self._last_connected_at
            else None,
            "last_disconnected": self._last_disconnected_at.isoformat()
            if self._last_disconnected_at
            else None,
            "last_disconnect_reason": self._last_disconnect_reason,
            "last_reconnect_reason": self._last_reconnect_reason,
            "last_setup_error": self._last_setup_error,
            "last_write": deepcopy(self._last_write),
            "last_write_failure": deepcopy(self._last_write_failure),
            "write_count": self._write_count,
        }

    def _note_disconnect(self, reason: str) -> None:
        if self._session_had_link:
            self._session_had_link = False
            self._disconnect_count += 1
            self._last_disconnected_at = datetime.now(UTC)
            self._last_disconnect_reason = reason

    def _record(self, event: str, **details) -> None:
        self._history.append(
            {"seconds": round(time.monotonic() - self._started_at, 3), "event": event, **details}
        )
        _LOGGER.debug("Svago %s: %s", event, details)

    def diagnostics(self) -> dict:
        """Read-only snapshot; no device or host identifiers are included."""
        status = self.controller.status
        return {
            "version": VERSION,
            "connected": self.bluetooth_connected,
            "available": self.controls_ready,
            "connection_health": self.health_snapshot(),
            "wired_controller_power_estimate": self.wired_controller_power_estimate,
            "service_discovery": {
                "cache_recovery_attempts": self._service_cache_recoveries,
                "last_cache_clear": self._last_cache_clear,
                "last_missing_service_error": self._last_missing_service_error,
            },
            "activity": self.activity.snapshot(),
            "timer_preferences": self.preferences.diagnostics if self.preferences else None,
            "busy": self.controller.busy,
            "setup_stage": self._setup_stage,
            "last_setup_error": self._last_setup_error,
            "notification_count": self._notification_count,
            "valid_status_count": self._valid_status_count,
            "bluetooth_backend": self._bluetooth_backend,
            "mtu_check": self._mtu_result,
            "verified_bluez_mtu": self._bluez_mtu,
            "effective_command_capacity": self._command_capacity(),
            "last_status_hex": self._last_status,
            "last_status_age_seconds": (
                round(time.monotonic() - self._last_status_at, 3)
                if self._last_status_at is not None
                else None
            ),
            "heat": status.heat if status else None,
            "last_activation": deepcopy(self.controller.last_activation),
            "last_home_request": deepcopy(self.controller.last_home_request),
            "last_heat_request": (
                dict(self.controller.last_heat_request)
                if self.controller.last_heat_request
                else None
            ),
            "position_requests": deepcopy(list(self.controller.position_requests)),
            "tap_requests": deepcopy(list(self.controller.tap_requests)),
            "setting_requests": deepcopy(list(self.controller.setting_requests)),
            "preset_requests": deepcopy(list(self.controller.preset_requests)),
            "memory_requests": deepcopy(list(self.controller.memory_requests)),
            "saved_memory_slots": (
                [slot for slot in range(1, 4) if self.memories.get(slot) is not None]
                if self.memories is not None
                else []
            ),
            "massage": status.massage if status else None,
            "massage_renewal": self.controller.massage_renewal_diagnostics,
            "heat_renewal": self.controller.heat_renewal_diagnostics,
            "raw_positions": status.positions if status else None,
            "max_write_without_response_size": (
                self._write_char.max_write_without_response_size if self._write_char else None
            ),
            "history": [dict(record) for record in self._history],
        }

    def notify_listeners(self) -> None:
        if not self.controls_ready:
            self._reset_memory_arm()
        for listener in tuple(self.listeners):
            listener()

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    def _reset_memory_arm(self) -> None:
        """Arming belongs only to the current live connection, never a restart."""
        self._memory_save_armed = False
        self._memory_arm_epoch += 1

    def memory_save_armed(self) -> bool:
        return self._memory_save_armed

    def memory_save_in_progress(self) -> bool:
        return self._saving_memory_slot is not None

    @property
    def controls_locked(self) -> bool:
        """Only the memory destination and cancel controls are usable while saving."""
        return self._memory_save_armed or self._saving_memory_slot is not None

    async def set_memory_save_armed(self, armed: bool) -> None:
        if armed:
            if self._closed or self.memories is None or not self.controls_ready:
                raise ControlError("Connect to the chair before turning on Set memory.")
            if self._saving_memory_slot is not None:
                raise ControlError(f"Memory {self._saving_memory_slot} is still being saved.")
            self.controller.arm_memory_save()
        self._memory_save_armed = armed
        self.notify_listeners()

    async def press_memory(self, slot: int, *, save_intent: bool | None = None) -> None:
        """Save to the selected slot when armed; otherwise recall its profile."""
        validate_slot(slot)
        if self._saving_memory_slot is not None:
            # Any further memory press must not become a recall or another save
            # while the first destination is still being committed.
            return
        if save_intent is True and not self._memory_save_armed:
            raise ControlError("Set memory was cancelled. Turn it on again before saving.")
        if save_intent is False and self._memory_save_armed:
            raise CommandSuperseded
        if not self._memory_save_armed:
            await self.recall_memory(slot)
            return
        self._memory_save_armed = False
        self._saving_memory_slot = slot
        self.notify_listeners()
        try:
            await self.save_memory(slot)
        finally:
            # Failed/cancelled attempts must also require deliberate re-arming.
            self._saving_memory_slot = None
            self.notify_listeners()

    async def _loaded_memories(self) -> MemoryStore:
        if self._closed:
            raise ControlError("The integration is unloading; memory controls are unavailable.")
        if self.memories is None:
            raise ControlError("Memory storage is not ready. Wait for the integration to load.")
        try:
            # Usually a no-op; reread after an uncertain disk commit rather than
            # overwriting or recalling from a cache that no longer matches disk.
            await self.memories.async_load()
        except HomeAssistantError as exc:
            raise ControlError(str(exc)) from exc
        except Exception as exc:
            raise ControlError("Saved memories could not be read. Try again.") from exc
        if self._closed:
            raise ControlError("The integration is unloading; memory controls are unavailable.")
        return self.memories

    async def save_memory(self, slot: int) -> None:
        """Persist a fresh stationary snapshot without sending a chair command."""
        arm_epoch = self._memory_arm_epoch
        save_intent = self.controller.intent_sequence
        memories = await self._loaded_memories()
        if arm_epoch != self._memory_arm_epoch:
            raise ControlError("The chair connection changed. Turn on Set memory again to save.")
        if save_intent != self.controller.intent_sequence:
            raise ControlError(
                "A newer chair adjustment interrupted this save. Turn on Set memory again to save."
            )
        profile = self.controller.snapshot_memory()
        try:
            await memories.async_save(slot, profile)
        except HomeAssistantError as exc:
            raise ControlError(str(exc)) from exc
        except Exception as exc:
            raise ControlError(f"Could not save Memory {slot}. Try again.") from exc

    async def recall_memory(self, slot: int) -> None:
        if self.controls_locked:
            raise ControlError("Finish or cancel Set memory before recalling a preset.")
        # Reserve the user's movement intent before any disk wait. A later Stop,
        # save arm, control or reconnect prevents this older request starting afterward.
        request = self.controller.reserve_memory_recall()
        memories = await self._loaded_memories()
        try:
            profile = memories.get(slot)
        except ValueError as exc:
            raise ControlError(str(exc)) from exc
        if profile is None:
            raise ControlError(
                f"Memory {slot} is empty. Turn on Set memory, then press Memory {slot} to save it."
            )
        await self.controller.recall_memory(slot, profile, request=request)

    async def start(self, *, monitor: bool = True) -> None:
        await self._connect()
        if monitor:
            self._monitor = self.hass.async_create_background_task(
                self._maintain(), f"Svago connection {self.address}"
            )

    async def _connect(self) -> None:
        self._reset_memory_arm()
        self._connecting = True
        self._setup_complete = False
        self._notifications_subscribed = False
        self._connect_attempts += 1
        self._last_attempt_at = datetime.now(UTC)
        self._record("connecting")
        self._assembler.reset()
        self._ready.clear()
        self._notification_count = 0
        self._valid_status_count = 0
        self._reset_mtu()
        self._bluetooth_backend = None
        self._mtu_result = "not checked"
        self._setup_stage = "finding the chair"
        self.notify_listeners()
        try:
            client, notify_char = await self._connect_services()
            self.controller.set_connected(True)

            def received(_characteristic, data):
                # Ignore late notifications from a previous connection.
                if client is not self._client or not client.is_connected:
                    return
                self._notification_count += 1
                for status in self._assembler.feed(data):
                    self._valid_status_count += 1
                    hex_value = status.raw.hex().upper()
                    if hex_value != self._last_status:
                        self._record("FFF1_status", hex=hex_value)
                    self._last_status = hex_value
                    self._last_status_at = time.monotonic()
                    if status.usable:
                        self._last_status_received = datetime.now(UTC)
                    self.controller.accept_status(status)
                    if status.usable:
                        self._ready.set()

            self._setup_stage = "enabling chair status notifications"
            await client.start_notify(notify_char, received)
            self._notifications_subscribed = bool(client.is_connected)
            self._setup_stage = "waiting for chair status"
            self.notify_listeners()
            try:
                async with asyncio.timeout(STATUS_TIMEOUT):
                    await self._ready.wait()
            except TimeoutError as exc:
                if not self._notification_count:
                    reason = (
                        "Connected, but no chair status notifications arrived within 15 seconds."
                    )
                elif not self._valid_status_count:
                    reason = (
                        f"Received {self._notification_count} notifications, but none formed "
                        "a complete Svago status message with a valid checksum."
                    )
                else:
                    status = self.controller.status
                    reason = "The chair reported unrecognized heat or massage values"
                    if status is not None:
                        reason += f" (heat=0x{status.raw[19]:02X}, massage=0x{status.raw[17]:02X})"
                    reason += "."
                raise ControlError(reason) from exc
            if not client.is_connected or not self.controller.connected:
                raise ControlError("The Bluetooth connection closed before setup completed.")
            # Do not truncate/split the 27-byte application command.
            self._setup_stage = "checking Bluetooth command capacity"
            await self._check_bluez_mtu(client)
            try:
                async with asyncio.timeout(WRITE_SIZE_TIMEOUT):
                    while self._command_capacity() < FRAME_SIZE:  # noqa: ASYNC110
                        if not client.is_connected or not self.controller.connected:
                            raise ControlError(
                                "The Bluetooth connection closed while checking command capacity."
                            )
                        # Bleak exposes this as a property, without an update event.
                        await asyncio.sleep(0.2)
            except TimeoutError as exc:
                raise ControlError(
                    "Bluetooth reports a maximum command size of "
                    f"{self._write_char.max_write_without_response_size} bytes; "
                    "the chair's commands require 27 bytes. "
                    f"Backend: {self._bluetooth_backend}. MTU check: {self._mtu_result}."
                ) from exc
            if not self.controller.available:
                raise ControlError("The chair stopped sending fresh status during setup")
            self._record("ready")
            self._setup_stage = "ready"
            self._last_setup_error = None
            self._setup_complete = True
            self._successful_connections += 1
            self._last_connected_at = datetime.now(UTC)
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            self._last_setup_error = f"Failed while {self._setup_stage}: {detail}"
            self._record("setup_failed", stage=self._setup_stage, error_type=type(exc).__name__)
            await self._drop(reason="Setup failed")
            raise ControlError(
                f"Check that the Motion Mastery Bluetooth remote is off. {self._last_setup_error}"
            ) from exc
        except asyncio.CancelledError:
            await self._drop(reason="Connection attempt cancelled")
            raise
        finally:
            self._connecting = False
            self.notify_listeners()

    async def _connect_services(self):
        """Refresh missing services once, without sending a chair command."""
        for discovery_attempt in range(2):
            if self._closed:
                raise asyncio.CancelledError
            # Cache clearing can remove the old backend device path. Resolve it
            # again instead of reusing the device from the failed connection.
            self._setup_stage = "finding the chair"
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if device is None:
                raise ControlError(
                    "Home Assistant has not detected this chair through a Bluetooth adapter "
                    "or proxy that can connect to it."
                )
            self._setup_stage = "opening the Bluetooth connection"
            self.notify_listeners()
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                "Svago Motion Mastery",
                disconnected_callback=self._disconnected,
                ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                    self.hass, self.address, connectable=True
                ),
                use_services_cache=discovery_attempt == 0,
                max_attempts=2,
                timeout=20,
            )
            self._client = client
            self._session_had_link = bool(client.is_connected)
            backend = getattr(client, "_backend", None)
            self._bluetooth_backend = type(backend if backend is not None else client).__name__
            if self._closed:
                raise asyncio.CancelledError
            if not client.is_connected:
                raise ControlError("The chair disconnected during setup")
            self._setup_stage = "checking the chair's Bluetooth services"
            self.notify_listeners()
            service = client.services.get_service(SERVICE_UUID)
            if service is None:
                missing = "The expected Svago service FFF0 is absent"
            else:
                notify_char = service.get_characteristic(NOTIFY_UUID)
                write_char = service.get_characteristic(WRITE_UUID)
                if (
                    notify_char is not None
                    and write_char is not None
                    and "notify" in notify_char.properties
                    and "write-without-response" in write_char.properties
                ):
                    self._write_char = write_char
                    return client, notify_char
                missing = "The expected FFF1/FFF2 characteristics are unavailable"
            self._last_missing_service_error = missing
            self._record("required_services_missing", fresh_discovery=bool(discovery_attempt))
            if discovery_attempt:
                raise ControlError(f"{missing} after fresh service discovery")

            self._service_cache_recoveries += 1
            self._setup_stage = "refreshing the chair's Bluetooth services"
            self.notify_listeners()
            self._record("service_cache_refresh_started")
            try:
                async with asyncio.timeout(SERVICE_CACHE_TIMEOUT):
                    cleared = await client.clear_cache()
                self._last_cache_clear = "cleared" if cleared else "not supported"
            except asyncio.CancelledError:
                self._last_cache_clear = "cancelled"
                raise
            except Exception as exc:
                # An unsupported or failed cache clear must not prevent the
                # one uncached attempt. Preserve the outcome for diagnostics.
                self._last_cache_clear = f"failed ({type(exc).__name__})"
            self._record("service_cache_refresh_result", outcome=self._last_cache_clear)
            await self._drop(reason="Refreshing Bluetooth services")
            # Clearing a cache may fire the old client's disconnect callback.
            # Its wake-up must not satisfy the replacement's status wait.
            self._ready.clear()

    def _reset_mtu(self) -> None:
        """A negotiated size is valid only for the connection that reported it."""
        self._bluez_mtu = None
        self._mtu_client = None
        self._mtu_backend = None

    def _command_capacity(self) -> int:
        client, characteristic = self._client, self._write_char
        if client is None or characteristic is None or not client.is_connected:
            return 0
        reported = characteristic.max_write_without_response_size
        if (
            reported == 20
            and self._bluez_mtu is not None
            and self._mtu_client is client
            and self._mtu_backend is getattr(client, "_backend", None)
        ):
            # BlueZ's characteristic MTU property can be absent/stale (default 20).
            # Only a successful AcquireWrite/MTU result on this same local BlueZ
            # connection may replace that default. Never trust a generic client
            # mtu_size or apply this override to proxies/other Bluetooth backends.
            return self._bluez_mtu - 3
        return reported

    async def _check_bluez_mtu(self, client) -> None:
        """Resolve BlueZ's default size using Bleak's documented MTU workaround."""
        if self._write_char.max_write_without_response_size != 20:
            self._mtu_result = "not needed"
            return
        backend = getattr(client, "_backend", None)
        if not isinstance(backend, BleakClientBlueZDBus):
            self._mtu_result = "not a local BlueZ connection; using reported capacity"
            return
        self._record("bluez_mtu_requested")
        try:
            # Bleak 3.0.2 _acquire_mtu uses AcquireWrite when a write-without-
            # response characteristic exists, then closes the returned descriptor.
            # It sends no characteristic payload. Keep this private-API use gated
            # to the known BlueZ backend; see Bleak's examples/mtu_size.py.
            async with asyncio.timeout(MTU_TIMEOUT):
                await backend._acquire_mtu()
            if (
                client is not self._client
                or not client.is_connected
                or getattr(client, "_backend", None) is not backend
            ):
                raise ControlError("The Bluetooth connection changed during the MTU check")
            mtu = backend.mtu_size
            if type(mtu) is not int or not 23 <= mtu <= 517:
                raise ControlError(f"BlueZ returned an invalid MTU: {mtu!r}")
            self._bluez_mtu = mtu
            self._mtu_client = client
            self._mtu_backend = backend
            self._mtu_result = f"negotiated {mtu} bytes"
            self._record("bluez_mtu_acquired", mtu=mtu, capacity=mtu - 3)
        except Exception as exc:
            self._reset_mtu()
            reason = str(exc) or type(exc).__name__
            self._mtu_result = f"failed ({reason})"
            self._record("bluez_mtu_failed", error_type=type(exc).__name__, reason=reason)
            # A property update may still arrive; the existing bounded wait below
            # decides whether setup can continue. No command is sent as a probe.

    def _disconnected(self, client) -> None:
        if client is self._client:
            self._note_disconnect("Bluetooth link disconnected")
            self._setup_complete = False
            self._notifications_subscribed = False
            if not self._connecting:
                self._setup_stage = "disconnected"
            self._reset_mtu()
            self._record("disconnected")
            self.controller.set_connected(False)
            self._assembler.reset()
            self._ready.set()  # Wake setup immediately instead of hiding a disconnect as a timeout.

    async def _write(self, payload: bytes) -> None:
        client = self._client
        characteristic = self._write_char
        self._write_count += 1
        started = time.monotonic()
        trace = {
            "number": self._write_count,
            "outcome": "pending",
            "attempted_at": datetime.now(UTC).isoformat(),
            "command_hex": payload.hex().upper(),
        }
        self._last_write = trace
        sent = False
        try:
            if client is None or not client.is_connected or characteristic is None:
                raise ControlError("The Bluetooth connection is unavailable")
            if self._command_capacity() < len(payload):
                raise ControlError("This Bluetooth connection cannot carry the 27-byte command")
            self._record("FFF2_write_attempt", hex=payload.hex().upper())
            sent = True
            await client.write_gatt_char(characteristic, payload, response=False)
            # A transport return is not proof of the requested physical effect.
            self._record("FFF2_write_returned")
            trace["outcome"] = "returned"
        except asyncio.CancelledError:
            trace["outcome"] = "cancelled"
            raise
        except Exception as exc:
            self._record("FFF2_write_failed", error_type=type(exc).__name__)
            trace["outcome"] = "failed" if sent else "blocked"
            trace["error_type"] = type(exc).__name__
            # Backend messages may contain host/adapter identifiers. Keep the
            # useful exception class and a safe cause rather than exporting them.
            trace["error"] = "The chair write failed; it was not retried" if sent else str(exc)
            self._last_write_failure = trace
            if not sent:
                raise
            raise ControlError("The chair write failed; it was not retried") from exc
        finally:
            trace["completed_at"] = datetime.now(UTC).isoformat()
            trace["duration_seconds"] = round(time.monotonic() - started, 3)

    async def _drop(self, *, reason: str = "Connection reset") -> None:
        self._note_disconnect(reason)
        self._setup_complete = False
        self._notifications_subscribed = False
        if self._last_setup_error is None:
            self._setup_stage = "stopped" if self._closed else "disconnected"
        self._record("connection_dropped")
        client, self._client = self._client, None
        self._reset_mtu()
        self._write_char = None
        self.controller.set_connected(False)
        self._assembler.reset()
        if client is not None:
            try:
                async with asyncio.timeout(5):
                    await client.disconnect()
            except Exception:
                _LOGGER.debug("Svago disconnect cleanup failed", exc_info=True)

    async def _maintain(self) -> None:
        while not self._closed:
            await asyncio.sleep(1)
            # Health remains visible even with identical notifications or a
            # missing disconnect callback. This is a local snapshot, not a probe.
            self.notify_listeners()
            if self.controls_ready:
                continue
            self._last_reconnect_reason = self.connection_status
            # End the current operation before replacing its transport. Never replay it.
            await self.controller.cancel_active()
            await self._drop(reason=self._last_reconnect_reason)
            await asyncio.sleep(5)
            if self._closed:
                return
            try:
                await self._connect()
            except Exception as exc:
                _LOGGER.debug("Svago reconnect pending: %s", exc)
            else:
                _LOGGER.info("Svago status connection restored; no action was replayed")

    async def close(self) -> None:
        self._closed = True
        self._reset_memory_arm()
        if self._monitor is not None:
            self._monitor.cancel()
            try:
                await self._monitor
            except asyncio.CancelledError:
                pass
            self._monitor = None
        # On a normal unload, cancel while the old transport can still release.
        try:
            await self.controller.cancel_active()
        finally:
            try:
                await self._drop(reason="Integration stopped")
            finally:
                # A late disk commit must not race a reload or entry removal.
                # Stop/drop the chair first so disk latency cannot delay release.
                try:
                    if self.memories is not None:
                        await self.memories.async_close()
                finally:
                    if self.preferences is not None:
                        await self.preferences.async_close()
