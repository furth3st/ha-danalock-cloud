"""Battery sensor for Danalock Cloud."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOCK_BATTERY, LOCK_SERIAL
from .coordinator import DanalockDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one battery sensor per lock."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DanalockDataUpdateCoordinator = data["coordinator"]
    async_add_entities(
        DanalockBatterySensor(coordinator, lock) for lock in data["locks"] if lock.get(LOCK_SERIAL)
    )


class DanalockBatterySensor(CoordinatorEntity[DanalockDataUpdateCoordinator], SensorEntity):
    """Battery level of a Danalock (refreshed about hourly; kept between polls)."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DanalockDataUpdateCoordinator, lock_info: dict[str, Any]) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._serial: str = lock_info[LOCK_SERIAL]
        self._attr_unique_id = f"danalock_cloud_{self._serial}_battery"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, self._serial)})

    @property
    def native_value(self) -> int | None:
        """Return the last known battery percentage."""
        return (self.coordinator.data or {}).get(self._serial, {}).get(LOCK_BATTERY)

    @property
    def available(self) -> bool:
        """Available whenever a reading exists."""
        return self.native_value is not None or self.coordinator.last_update_success
