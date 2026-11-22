"""Persist timer choices independently from running chair sessions."""

import asyncio
import re
from collections.abc import Callable

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .memory import _has_existing_storage

_KEYS = {"heat", "massage"}


class TimerPreferences:
    """Coalesce current choices while serializing and verifying disk writes."""

    def __init__(self, hass: HomeAssistant, entry_id: str, changed: Callable = lambda: None):
        if not isinstance(entry_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", entry_id):
            raise ValueError("A valid configuration entry ID is required")
        self._hass = hass
        self._key = f"svago.timer_preferences.{entry_id}"
        self._store = Store(hass, 1, self._key, atomic_writes=True)
        self._changed = changed
        self._choices = {"heat": False, "massage": False}
        self._loaded = False
        self._closing = False
        self._removed = False
        self._revision = 0
        self._saved_revision = 0
        self._writer: asyncio.Task | None = None
        self._last_error: str | None = None

    @property
    def choices(self) -> dict[str, bool]:
        return dict(self._choices)

    @property
    def diagnostics(self) -> dict:
        return {
            "loaded": self._loaded,
            "choices": self.choices,
            "pending": self._saved_revision != self._revision,
            "last_error": self._last_error,
        }

    async def async_load(self) -> None:
        if self._loaded or self._closing or self._removed:
            return
        existed = await self._hass.async_add_executor_job(_has_existing_storage, self._store.path)
        data = await self._store.async_load()
        if data is None and existed:
            raise HomeAssistantError(
                "Saved timer preferences could not be read; restore their backup."
            )
        if data is not None:
            if (
                type(data) is not dict
                or set(data) != {"continuous"}
                or type(data["continuous"]) is not dict
                or set(data["continuous"]) != _KEYS
                or any(type(value) is not bool for value in data["continuous"].values())
            ):
                raise HomeAssistantError(
                    "Saved timer preferences are invalid; they were not replaced."
                )
            self._choices = dict(data["continuous"])
        self._loaded = True

    def update(self, choices: dict[str, bool]) -> None:
        if set(choices) != _KEYS or any(type(value) is not bool for value in choices.values()):
            raise ValueError("Both timer preferences must be booleans")
        if not self._loaded or self._closing or self._removed:
            return
        if choices != self._choices:
            self._choices = dict(choices)
            self._revision += 1
        self._start_writer()

    def _start_writer(self) -> None:
        if self._writer is None and self._saved_revision != self._revision:
            self._writer = asyncio.create_task(
                self._write_pending(), name="svago_timer_preferences"
            )

    async def _persist(self, choices: dict[str, bool]) -> None:
        if self._hass.state in (CoreState.stopping, CoreState.final_write):
            raise HomeAssistantError(
                "Home Assistant is stopping; the timer preference was not saved."
            )
        data = {"continuous": choices}
        await self._store.async_save(data)
        confirmed = await Store(self._hass, 1, self._key).async_load()
        if confirmed != data:
            raise HomeAssistantError("The timer preference could not be verified on disk.")

    async def _write_pending(self) -> None:
        try:
            while self._saved_revision != self._revision:
                revision, choices = self._revision, self.choices
                try:
                    await self._persist(choices)
                except Exception:
                    # Retain a useful, identifier-free problem for diagnostics.
                    # Explicit selection/flush raises it to the caller as well.
                    self._last_error = (
                        "Timer preferences could not be saved. Try selecting the timer again."
                    )
                    return
                self._saved_revision = revision
                self._last_error = None
        finally:
            self._writer = None
            self._changed()

    async def async_flush(self) -> None:
        if not self._loaded or self._removed:
            return
        self._start_writer()
        writer = self._writer
        if writer is not None:
            # Cancelling a chair action must not interrupt its disk commit.
            await asyncio.shield(writer)
        if self._last_error is not None:
            raise HomeAssistantError(self._last_error)

    async def async_close(self) -> None:
        self._closing = True
        await self._finish_io(self.async_flush())

    @staticmethod
    async def _finish_io(action) -> None:
        """Finish shutdown/removal I/O before allowing a cancelled caller to leave."""
        task = asyncio.create_task(action)
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def async_remove(self) -> None:
        self._closing = True
        if self._removed:
            return

        async def remove():
            if self._writer is not None:
                await asyncio.shield(self._writer)
            await self._store.async_remove()
            self._removed = True

        await self._finish_io(remove())
