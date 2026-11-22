"""Durable, entry-scoped storage for independent Home Assistant memories."""

import asyncio
import logging
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .memory_protocol import MemoryProfile, validate_slot

_LOGGER = logging.getLogger(__name__)
_STORAGE_VERSION = 1


def _has_existing_storage(path: str) -> bool:
    """Include HA's quarantined files when deciding whether a store is new."""
    target = Path(path)
    try:
        with os.scandir(target.parent) as entries:
            return any(
                entry.name == target.name or entry.name.startswith(f"{target.name}.corrupt.")
                for entry in entries
            )
    except FileNotFoundError:
        return False


class MemoryStore:
    """Publish immutable profiles only after a confirmed disk commit."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, changed: Callable[[], None] = lambda: None
    ) -> None:
        if not isinstance(entry_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", entry_id):
            raise ValueError("A valid configuration entry ID is required")
        self._hass = hass
        self._key = f"svago.memories.{entry_id}"
        self._store = Store(hass, _STORAGE_VERSION, self._key, atomic_writes=True)
        self._changed = changed
        self._lock = asyncio.Lock()
        self._profiles: dict[int, MemoryProfile] = {}
        self._applied_migrations: frozenset[str] = frozenset()
        self._loaded = False
        self._import_allowed = False
        self._closing = False
        self._removed = False

    def get(self, slot: int) -> MemoryProfile | None:
        validate_slot(slot)
        return self._profiles.get(slot)

    async def async_load(self) -> None:
        """Load once without writes or chair commands; retain only valid slots."""
        async with self._lock:
            if self._loaded or self._closing or self._removed:
                return
            # Read failures propagate. An unread file must never be overwritten
            # by an apparently empty cache after a transient read error.
            existed = await self._hass.async_add_executor_job(
                _has_existing_storage, self._store.path
            )
            data = await self._store.async_load()
            profiles = {}
            applied_migrations = frozenset()
            # HA can return None after quarantining malformed JSON. Such a file
            # must not silently become new defaults, including on later loads.
            import_allowed = data is None and not existed
            if data is not None:
                if (
                    type(data) is not dict
                    or set(data) not in ({"profiles"}, {"profiles", "applied_migrations"})
                    or type(data["profiles"]) is not dict
                ):
                    _LOGGER.warning("Ignoring invalid saved Svago memory data for %s", self._key)
                else:
                    import_allowed = True
                    migrations = data.get("applied_migrations", [])
                    if (
                        type(migrations) is not list
                        or any(not self._valid_migration_id(value) for value in migrations)
                        or len(set(migrations)) != len(migrations)
                    ):
                        import_allowed = False
                        _LOGGER.warning("Ignoring invalid Svago memory migrations in %s", self._key)
                    else:
                        applied_migrations = frozenset(migrations)
                    for slot, value in data["profiles"].items():
                        if type(slot) is not str or slot not in ("1", "2", "3"):
                            import_allowed = False
                            _LOGGER.warning("Ignoring invalid Svago memory slot in %s", self._key)
                            continue
                        try:
                            profiles[int(slot)] = MemoryProfile.from_dict(value)
                        except ValueError as exc:
                            import_allowed = False
                            _LOGGER.warning("Ignoring invalid Svago Memory %s: %s", slot, exc)
            self._profiles = profiles
            self._applied_migrations = applied_migrations
            self._import_allowed = import_allowed
            self._loaded = True
            self._changed()

    async def _persist(self, data: dict) -> None:
        if self._hass.state in (CoreState.stopping, CoreState.final_write):
            raise HomeAssistantError("Home Assistant is stopping; the memory was not saved.")
        await self._store.async_save(data)
        # Store logs some write failures instead of raising them. A fresh Store
        # has no pending in-memory data and reads the atomically written file.
        confirmed = await Store(self._hass, _STORAGE_VERSION, self._key).async_load()
        if confirmed != data:
            raise HomeAssistantError("The saved memory could not be verified on disk.")

    @staticmethod
    async def _finish_commit(action) -> bool:
        """Hold serialization through executor completion, even if caller cancels."""
        task = asyncio.create_task(action)
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                return cancelled
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True

    async def async_save(self, slot: int, profile: MemoryProfile) -> None:
        validate_slot(slot)
        if not isinstance(profile, MemoryProfile):
            raise ValueError("A validated memory profile is required")
        async with self._lock:
            self._check_writable()
            await self._commit_profiles({**self._profiles, slot: profile})

    async def async_import_missing(self, profiles: Mapping[int, MemoryProfile]) -> None:
        """Fill absent slots only in new or fully validated persisted data."""
        if not isinstance(profiles, Mapping):
            raise ValueError("A mapping of memory slots to validated profiles is required")
        imported = {}
        # Validate and detach the entire request before awaiting anything. Even
        # an invalid entry for an already occupied slot must not be overlooked.
        for slot, profile in profiles.items():
            validate_slot(slot)
            if not isinstance(profile, MemoryProfile):
                raise ValueError("A validated memory profile is required")
            imported[slot] = profile
        async with self._lock:
            self._check_writable()
            if not self._import_allowed:
                return
            missing = {
                slot: profile for slot, profile in imported.items() if slot not in self._profiles
            }
            if not missing:
                return
            await self._commit_profiles({**self._profiles, **missing})

    @staticmethod
    def _valid_migration_id(value: object) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None

    async def async_restore_once(
        self, migration_id: str, profiles: Mapping[int, MemoryProfile]
    ) -> None:
        """Restore authorized slots and their completion marker in one commit."""
        if not self._valid_migration_id(migration_id):
            raise ValueError("A valid memory migration ID is required")
        if not isinstance(profiles, Mapping) or not profiles:
            raise ValueError("At least one validated memory profile is required")
        restored = {}
        for slot, profile in profiles.items():
            validate_slot(slot)
            if not isinstance(profile, MemoryProfile):
                raise ValueError("A validated memory profile is required")
            restored[slot] = profile
        async with self._lock:
            self._check_writable()
            if not self._import_allowed or migration_id in self._applied_migrations:
                return
            await self._commit_profiles(
                {**self._profiles, **restored},
                applied_migrations=self._applied_migrations | {migration_id},
            )

    def _check_writable(self) -> None:
        if not self._loaded or self._closing or self._removed:
            raise HomeAssistantError("Saved memories are not available for writing.")

    async def _commit_profiles(
        self,
        updated: dict[int, MemoryProfile],
        *,
        applied_migrations: frozenset[str] | None = None,
    ) -> None:
        """Commit under the caller's lock, sharing save/import failure semantics."""
        if applied_migrations is None:
            applied_migrations = self._applied_migrations
        data = {"profiles": {str(key): value.to_dict() for key, value in updated.items()}}
        if applied_migrations:
            data["applied_migrations"] = sorted(applied_migrations)
        try:
            cancelled = await self._finish_commit(self._persist(data))
        except BaseException:
            # The write may have reached disk before verification failed.
            # Require a successful reload before rebuilding another save.
            self._loaded = False
            raise
        self._profiles = updated
        self._applied_migrations = applied_migrations
        self._import_allowed = True
        self._changed()
        if cancelled:
            raise asyncio.CancelledError

    async def async_close(self) -> None:
        """Reject queued saves and drain a commit before unload/reload/removal."""
        self._closing = True
        async with self._lock:
            pass

    async def async_remove(self) -> None:
        """Delete this entry's data and prevent queued saves from recreating it."""
        self._closing = True
        async with self._lock:
            if self._removed:
                return
            cancelled = await self._finish_commit(self._store.async_remove())
            self._profiles = {}
            self._applied_migrations = frozenset()
            self._removed = True
            self._changed()
            if cancelled:
                raise asyncio.CancelledError
