"""Firmware version reporting for Danalock Cloud (read-only: updates are installed in the Danalock app)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOCK_FIRMWARE, LOCK_LATEST_FIRMWARE, LOCK_SERIAL
from .coordinator import DanalockDataUpdateCoordinator

FIRMWARE_HELP_URL = "https://support.saltosystems.com/danalock/danalock/general-information/#firmware"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one firmware entity per lock."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DanalockDataUpdateCoordinator = data["coordinator"]
    async_add_entities(
        DanalockFirmwareUpdate(coordinator, lock) for lock in data["locks"] if lock.get(LOCK_SERIAL)
    )


class DanalockFirmwareUpdate(CoordinatorEntity[DanalockDataUpdateCoordinator], UpdateEntity):
    """Installed vs. latest published lock firmware (checked about twice a day)."""

    _attr_has_entity_name = True
    _attr_translation_key = "firmware"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_release_url = FIRMWARE_HELP_URL

    def __init__(self, coordinator: DanalockDataUpdateCoordinator, lock_info: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._serial: str = lock_info[LOCK_SERIAL]
        self._attr_unique_id = f"danalock_cloud_{self._serial}_firmware"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, self._serial)})

    @property
    def _data(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._serial, {})

    @property
    def installed_version(self) -> str | None:
        return self._data.get(LOCK_FIRMWARE)

    @property
    def latest_version(self) -> str | None:
        # Unknown latest -> report "up to date" rather than a false "update available".
        return self._data.get(LOCK_LATEST_FIRMWARE) or self.installed_version

    @property
    def release_summary(self) -> str | None:
        if self.installed_version and self.latest_version != self.installed_version:
            return "Install the update with the Danalock app (Devices > your lock > Firmware)."
        return None
