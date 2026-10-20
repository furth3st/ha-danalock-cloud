"""Data update coordinator for Danalock Cloud."""
from __future__ import annotations

import logging
from datetime import timedelta
from time import monotonic
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import DanalockApiClient, DanalockApiClientError
from .const import (
    BATTERY_REFRESH_INTERVAL,
    DOMAIN,
    LOCK_BATTERY,
    LOCK_FIRMWARE,
    LOCK_JAMMED,
    LOCK_LATEST_FIRMWARE,
    LOCK_NAME,
    LOCK_SERIAL,
    LOCK_SETTINGS,
    LOCK_STATE,
    LOCK_STATE_UPDATED,
    SETTINGS_REFRESH_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class DanalockDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Poll every lock on the account, one at a time (they share a single bridge)."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api_client: DanalockApiClient,
        locks: list[dict[str, Any]],
        update_interval_minutes: int,
    ) -> None:
        """Initialize."""
        self.api_client = api_client
        self.locks = {lock[LOCK_SERIAL]: lock for lock in locks if lock.get(LOCK_SERIAL)}
        self._battery_checked_at: dict[str, float] = {}
        self._details_checked_at: dict[str, float] = {}
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=max(1, int(update_interval_minutes))),
        )

    @staticmethod
    def _due(checked: dict[str, float], serial: str, interval: timedelta) -> bool:
        last = checked.get(serial)
        return last is None or monotonic() - last >= interval.total_seconds()

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch fresh data, keeping the last known value for anything that can't be read right now."""
        previous = self.data or {}
        results: dict[str, dict[str, Any]] = {}
        reached_any = False

        for serial, info in self.locks.items():
            old = previous.get(serial, {})
            fetch_battery = self._due(self._battery_checked_at, serial, BATTERY_REFRESH_INTERVAL)
            # While an update is pending, look again hourly so "update available" clears soon
            # after the user installs it in the Danalock app.
            update_pending = bool(old.get(LOCK_LATEST_FIRMWARE)) and old.get(LOCK_FIRMWARE) != old.get(LOCK_LATEST_FIRMWARE)
            details_interval = BATTERY_REFRESH_INTERVAL if update_pending else SETTINGS_REFRESH_INTERVAL
            fetch_details = self._due(self._details_checked_at, serial, details_interval)
            try:
                fresh = await self.api_client.get_lock_data(
                    serial, include_battery=fetch_battery, include_details=fetch_details
                )
            except ConfigEntryAuthFailed:
                raise  # Home Assistant turns this into a re-authentication prompt
            except DanalockApiClientError as err:
                _LOGGER.debug("Update for lock %s failed: %s", serial, err)
                fresh = {}

            state = fresh.get(LOCK_STATE)
            battery = fresh.get(LOCK_BATTERY)
            settings = fresh.get(LOCK_SETTINGS)
            firmware = fresh.get(LOCK_FIRMWARE)
            latest = old.get(LOCK_LATEST_FIRMWARE)
            if state is not None:
                reached_any = True
            if battery is not None:
                self._battery_checked_at[serial] = monotonic()
            if fetch_details and (settings is not None or firmware is not None):
                self._details_checked_at[serial] = monotonic()
                latest = await self.api_client.get_latest_firmware(serial) or latest
                if firmware:
                    self._update_device_firmware(serial, firmware)

            results[serial] = {
                LOCK_NAME: info.get(LOCK_NAME, serial),
                # A transient "BridgeBusy"/timeout must not wipe a known state.
                LOCK_STATE: state if state is not None else old.get(LOCK_STATE),
                LOCK_BATTERY: battery if battery is not None else old.get(LOCK_BATTERY),
                LOCK_SETTINGS: settings if settings is not None else old.get(LOCK_SETTINGS),
                LOCK_FIRMWARE: firmware or old.get(LOCK_FIRMWARE),
                LOCK_LATEST_FIRMWARE: latest,
                # a successful read means the bolt moved, so it is no longer jammed
                LOCK_JAMMED: False if state is not None else old.get(LOCK_JAMMED, False),
                LOCK_STATE_UPDATED: dt_util.utcnow() if state is not None else old.get(LOCK_STATE_UPDATED),
                "state_fresh": state is not None,
            }

        if self.locks and not reached_any and not previous:
            # Nothing has ever been readable: surface it instead of showing stale "unknown".
            raise UpdateFailed("Could not read any Danalock lock (bridge offline or Danalock unreachable)")
        return results

    async def async_refresh_everything(self) -> None:
        """Manual refresh: re-read battery, settings and firmware too, not just the state."""
        self._battery_checked_at.clear()
        self._details_checked_at.clear()
        await self.async_request_refresh()

    def apply_command_result(
        self,
        serial: str,
        *,
        state: str | None = None,
        battery: int | None = None,
        jammed: bool | None = None,
    ) -> None:
        """Push what a lock/unlock told us to every entity right away (no extra bridge read)."""
        data = {key: dict(value) for key, value in (self.data or {}).items()}
        lock = data.setdefault(serial, {LOCK_NAME: self.locks.get(serial, {}).get(LOCK_NAME, serial)})
        if state is not None:
            lock[LOCK_STATE] = state
            lock[LOCK_STATE_UPDATED] = dt_util.utcnow()
            lock["state_fresh"] = True
            lock[LOCK_JAMMED] = False
        if battery is not None:
            lock[LOCK_BATTERY] = battery
            self._battery_checked_at[serial] = monotonic()
        if jammed is not None:
            lock[LOCK_JAMMED] = jammed
        self.async_set_updated_data(data)

    def _update_device_firmware(self, serial: str, firmware: str) -> None:
        # async_get_device(identifiers=...) is deprecated since HA 2026.x; walking this entry's
        # devices works on every supported version (2024.12+).
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            if (DOMAIN, serial) in device.identifiers and device.sw_version != firmware:
                registry.async_update_device(device.id, sw_version=firmware)
