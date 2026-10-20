"""Lock platform for Danalock Cloud."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime
from statistics import median
from time import monotonic
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import DanalockApiClient, DanalockApiClientError, DanalockJobError
from .const import (
    API_STATE_LOCKED,
    API_STATE_UNLOCKED,
    AUTO_LOCK_CHECK_MARGIN,
    CONF_OPTIMISTIC_MODE,
    DEFAULT_COMMAND_SECONDS,
    DOMAIN,
    EVENT_LOCK_COMMAND_FAILURE,
    EVENT_LOCK_COMMAND_SUCCESS,
    LOCK_BATTERY,
    LOCK_FIRMWARE,
    LOCK_JAMMED,
    LOCK_NAME,
    LOCK_SERIAL,
    LOCK_SETTINGS,
    LOCK_STATE,
    LOCK_STATE_UPDATED,
)
from .coordinator import DanalockDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Danalock lock entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DanalockDataUpdateCoordinator = data["coordinator"]
    api_client: DanalockApiClient = data["api_client"]
    async_add_entities(
        DanalockLockEntity(coordinator, api_client, lock)
        for lock in data["locks"]
        if lock.get(LOCK_SERIAL)
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


class DanalockLockEntity(CoordinatorEntity[DanalockDataUpdateCoordinator], LockEntity):
    """A Danalock lock."""

    _attr_has_entity_name = True
    _attr_name = None  # the lock is the device's main feature: use the device name
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(
        self,
        coordinator: DanalockDataUpdateCoordinator,
        api_client: DanalockApiClient,
        lock_info: dict[str, Any],
    ) -> None:
        """Initialize the lock entity."""
        super().__init__(coordinator)
        self._api_client = api_client
        self._serial: str = lock_info[LOCK_SERIAL]
        self._attr_unique_id = f"danalock_cloud_{self._serial}"
        self._device_name: str = lock_info.get(LOCK_NAME) or f"Danalock {self._serial}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=self._device_name,
            manufacturer="Danalock",
            model="V3 (Cloud)",
            serial_number=self._serial,
            sw_version=self._lock_data.get(LOCK_FIRMWARE),
        )
        self._attr_is_locked = None
        self._attr_is_jammed = False
        # command bookkeeping, exposed as attributes for dashboards / the custom card
        self._command: str | None = None
        self._command_started: datetime | None = None
        self._durations: deque[float] = deque(maxlen=10)
        self._last_command: str | None = None
        self._last_result: str | None = None
        self._last_error: str | None = None
        self._last_duration: float | None = None
        self._last_command_at: datetime | None = None
        self._cancel_auto_lock_check: CALLBACK_TYPE | None = None
        # one command per lock at a time (e.g. an automation and a tap): the second waits
        self._command_lock = asyncio.Lock()
        self._apply_coordinator_state()

    @property
    def _lock_data(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._serial, {})

    def _apply_coordinator_state(self) -> None:
        data = self._lock_data
        state = data.get(LOCK_STATE)
        if state == API_STATE_LOCKED:
            self._attr_is_locked = True
        elif state == API_STATE_UNLOCKED:
            self._attr_is_locked = False
        # anything else (None while the bridge is busy): keep the last known state
        self._attr_is_jammed = bool(data.get(LOCK_JAMMED))

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._command is None:  # don't let a poll overwrite an in-flight command's state
            self._apply_coordinator_state()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available once we have ever known the state, even if the latest poll hiccupped."""
        return self._attr_is_locked is not None or self.coordinator.last_update_success

    @property
    def expected_duration(self) -> float:
        """Typical seconds a command takes on this lock (median of recent ones)."""
        return round(median(self._durations), 1) if self._durations else DEFAULT_COMMAND_SECONDS

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._lock_data
        settings = data.get(LOCK_SETTINGS) or {}
        return {
            "command_in_progress": self._command,
            "command_started": _iso(self._command_started),
            "expected_duration": self.expected_duration,
            "last_command": self._last_command,
            "last_command_result": self._last_result,
            "last_command_error": self._last_error,
            "last_command_duration": self._last_duration,
            "last_command_at": _iso(self._last_command_at),
            "state_updated": _iso(data.get(LOCK_STATE_UPDATED)),
            "state_fresh": data.get("state_fresh"),
            "battery_level": data.get(LOCK_BATTERY),
            "auto_lock": settings.get("auto_lock"),
        }

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_pending_check()
        await super().async_will_remove_from_hass()

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the device."""
        await self._run_command("lock", target_locked=True)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the device."""
        await self._run_command("unlock", target_locked=False)

    async def async_open(self, **kwargs: Any) -> None:
        """Open (unlatch). Danalock V3 has no separate unlatch over the cloud: unlock."""
        await self._run_command("unlock", target_locked=False)

    async def _run_command(self, command: str, *, target_locked: bool) -> None:
        async with self._command_lock:
            await self._run_command_locked(command, target_locked=target_locked)

    async def _run_command_locked(self, command: str, *, target_locked: bool) -> None:
        optimistic = self.coordinator.config_entry.options.get(CONF_OPTIMISTIC_MODE, False)
        previous = self._attr_is_locked
        self._cancel_pending_check()
        self._command = command
        self._command_started = dt_util.utcnow()
        started = monotonic()
        if optimistic:
            self._attr_is_locked = target_locked
        self._attr_is_jammed = False
        self._attr_is_locking = target_locked
        self._attr_is_unlocking = not target_locked
        self.async_write_ha_state()

        call = self._api_client.lock if target_locked else self._api_client.unlock
        error: Exception | None = None
        result: dict[str, Any] = {}
        try:
            result = await call(self._serial)
        except (DanalockApiClientError, ConfigEntryAuthFailed) as err:
            error = err
        finally:
            self._command = None
            self._command_started = None
            self._attr_is_locking = False
            self._attr_is_unlocking = False
            self._last_command = command
            self._last_command_at = dt_util.utcnow()
            self._last_duration = round(monotonic() - started, 1)

        if error is None:
            self._durations.append(self._last_duration)
            self._last_result = "success"
            self._last_error = None
            self._attr_is_locked = target_locked
            # updates every entity of this lock (state + battery) without another bridge read
            self.coordinator.apply_command_result(
                self._serial,
                state=API_STATE_LOCKED if target_locked else API_STATE_UNLOCKED,
                battery=(result or {}).get(LOCK_BATTERY),
            )
            self.async_write_ha_state()
            self.hass.bus.async_fire(
                EVENT_LOCK_COMMAND_SUCCESS,
                {"entity_id": self.entity_id, "command": command, "duration": self._last_duration},
            )
            if not target_locked:
                self._schedule_auto_lock_check()
            return

        # Failure: never leave an optimistic (wrong) state on a door lock.
        message = error.user_message if isinstance(error, DanalockJobError) else str(error)
        self._last_result = "failed"
        self._last_error = message
        self._attr_is_locked = previous
        if isinstance(error, DanalockJobError) and error.jammed:
            self._attr_is_jammed = True
            self.coordinator.apply_command_result(self._serial, jammed=True)
        self.async_write_ha_state()
        self.hass.bus.async_fire(
            EVENT_LOCK_COMMAND_FAILURE,
            {"entity_id": self.entity_id, "command": command, "error": message},
        )
        self.hass.async_create_background_task(
            self.coordinator.async_request_refresh(), f"{DOMAIN}_recheck_{self._serial}"
        )
        raise HomeAssistantError(f"Could not {command} {self._device_name}: {message}") from error

    def _schedule_auto_lock_check(self) -> None:
        """If the lock auto-locks, read it again right after that should have happened."""
        auto_lock = (self._lock_data.get(LOCK_SETTINGS) or {}).get("auto_lock")
        if not auto_lock or auto_lock <= 0:
            return

        @callback
        def _check(_now: datetime) -> None:
            self._cancel_auto_lock_check = None
            self.hass.async_create_background_task(
                self.coordinator.async_request_refresh(), f"{DOMAIN}_autolock_{self._serial}"
            )

        delay = auto_lock + AUTO_LOCK_CHECK_MARGIN.total_seconds()
        self._cancel_auto_lock_check = async_call_later(self.hass, delay, _check)

    def _cancel_pending_check(self) -> None:
        if self._cancel_auto_lock_check is not None:
            self._cancel_auto_lock_check()
            self._cancel_auto_lock_check = None
