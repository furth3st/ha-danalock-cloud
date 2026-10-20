"""The Danalock Cloud integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .api import DanalockApiClient, DanalockApiClientError
from .const import (
    ACCESS_TOKEN,
    CONF_PASSWORD,
    CONF_UPDATE_INTERVAL,
    CONF_USERNAME,
    DOMAIN,
    PLATFORMS,
    REFRESH_TOKEN,
    SERVICE_REFRESH_DEVICES,
    TOKEN_EXPIRES_AT,
    UPDATE_INTERVAL,
)
from .coordinator import DanalockDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
DEFAULT_UPDATE_MINUTES = int(UPDATE_INTERVAL.total_seconds() / 60)
CARD_FILENAME = "danalock-lock-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the lock card and load it in every dashboard (no manual resource needed).

    Purely cosmetic: any problem here is logged and never blocks the integration.
    """
    if getattr(hass, "http", None) is None or "frontend" not in hass.config.components:
        return
    try:
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.components.http import StaticPathConfig

        integration = await async_get_integration(hass, DOMAIN)
        path = Path(__file__).parent / "frontend" / CARD_FILENAME
        await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(path), True)])
        add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}")
    except Exception:  # noqa: BLE001 - the card must never break the lock integration
        _LOGGER.warning("Could not register the Danalock lock card", exc_info=True)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration-wide refresh service once."""
    hass.data.setdefault(DOMAIN, {})

    async def handle_refresh_devices(call: ServiceCall) -> None:
        coordinators = [
            data["coordinator"]
            for data in hass.data.get(DOMAIN, {}).values()
            if isinstance(data, dict) and "coordinator" in data
        ]
        await asyncio.gather(*(c.async_refresh_everything() for c in coordinators))

    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_DEVICES, handle_refresh_devices, schema=vol.Schema({})
    )
    await _async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Danalock Cloud from a config entry."""
    api_client = DanalockApiClient(
        hass,
        entry=entry,
        username=entry.data[CONF_USERNAME],
        password=entry.data.get(CONF_PASSWORD),
        access_token=entry.data.get(ACCESS_TOKEN),
        refresh_token=entry.data.get(REFRESH_TOKEN),
        token_expires_at=entry.data.get(TOKEN_EXPIRES_AT),
    )

    try:
        await api_client.async_ensure_token_valid()
        locks = await api_client.get_locks()
    except ConfigEntryAuthFailed:
        raise  # Home Assistant shows a "re-authenticate" repair; no retries, no lockout
    except DanalockApiClientError as err:
        raise ConfigEntryNotReady(f"Danalock is not reachable right now: {err}") from err

    if not locks:
        _LOGGER.warning("No Danalock locks found for %s", entry.title)

    coordinator = DanalockDataUpdateCoordinator(
        hass,
        config_entry=entry,
        api_client=api_client,
        locks=locks,
        update_interval_minutes=entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_MINUTES),
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api_client": api_client,
        "coordinator": coordinator,
        "locks": locks,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply a new polling interval live; other options are read on use."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    coordinator: DanalockDataUpdateCoordinator = data["coordinator"]
    minutes = max(1, int(entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_MINUTES)))
    coordinator.update_interval = timedelta(minutes=minutes)
