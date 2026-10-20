"""Diagnostics support for Danalock Cloud."""
from __future__ import annotations

import time
import logging
from typing import Any, Dict, Optional

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, ACCESS_TOKEN, REFRESH_TOKEN, TOKEN_EXPIRES_AT, LOCK_NAME as DIAG_LOCK_NAME, LOCK_STATE as DIAG_LOCK_STATE, LOCK_BATTERY as DIAG_LOCK_BATTERY
from .const import LOCK_FIRMWARE, LOCK_JAMMED, LOCK_LATEST_FIRMWARE, LOCK_SETTINGS

_LOGGER = logging.getLogger(__name__)

TO_REDACT = {
    CONF_PASSWORD,
    ACCESS_TOKEN,
    REFRESH_TOKEN,
    TOKEN_EXPIRES_AT,
    "password",
    "token",
    "tokens",
    "access_token",
    "refresh_token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    diag_data: Dict[str, Any] = {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "version": entry.version,
            "domain": entry.domain,
            "source": entry.source,
            "state": entry.state.value, # Use .value for enum
            "options": async_redact_data(entry.options, TO_REDACT),
            "data_keys": list(entry.data.keys()), # Show keys, not values for sensitive data
        },
        "diagnostics_version": "1.0.2", # Version of this diagnostics structure
    }

    if DOMAIN not in hass.data or entry.entry_id not in hass.data.get(DOMAIN, {}):
        diag_data["error"] = "Integration data not found. Setup may have failed or is in progress."
        return diag_data

    data = hass.data[DOMAIN][entry.entry_id]
    
    coordinator: Optional[DataUpdateCoordinator] = data.get("coordinator")
    if coordinator:
        # A plain DataUpdateCoordinator has no success timestamp (only the Timestamp* variant
        # does); reading it unguarded crashed "Download diagnostics" in every earlier release.
        last_success = getattr(coordinator, "last_update_success_time", None)
        last_update_timestamp_iso = last_success.isoformat() if last_success else None
        
        diag_data["coordinator"] = {
            "last_update_success": coordinator.last_update_success,
            "last_update_timestamp": last_update_timestamp_iso,
            "update_interval": str(coordinator.update_interval),
            "has_data": bool(coordinator.data),
            "data_keys_count": len(coordinator.data.keys()) if coordinator.data else 0,
        }
        if coordinator.data:
            safe_data_sample = {}
            for serial, lock_item_data in coordinator.data.items():
                if isinstance(lock_item_data, dict):
                    safe_data_sample[serial] = {
                        "name": lock_item_data.get(DIAG_LOCK_NAME, "Unknown"),
                        "has_state_info": DIAG_LOCK_STATE in lock_item_data,
                        "has_battery_info": DIAG_LOCK_BATTERY in lock_item_data,
                        # Do not include actual state or battery values here
                    }
            diag_data["coordinator"]["data_sample_structure"] = safe_data_sample
    
    locks_info = data.get("locks", [])
    diag_data["discovered_locks_at_setup"] = [
        {"name": lock.get(DIAG_LOCK_NAME, "Unknown")} for lock in locks_info
    ]
    diag_data["discovered_locks_count"] = len(locks_info)
    
    api_client = data.get("api_client")
    if api_client:
        token_expiry = getattr(api_client, "_token_expires_at", 0.0)
        expiry_info = {}
        if token_expiry > 0:
            current_time_val = time.time()
            expiry_info = {
                "expires_in_seconds": max(0, round(token_expiry - current_time_val)),
                "is_expired": token_expiry < current_time_val,
            }
        
        diag_data["api_client_status"] = {
            "has_access_token": bool(getattr(api_client, "_access_token", None)),
            "has_refresh_token": bool(getattr(api_client, "_refresh_token", None)),
            "has_password_stored_in_client": bool(getattr(api_client, "_password", None)),
            "token_expiry_info": expiry_info,
        }
        # Bridge timing: the most useful thing when commands feel slow or fail now and then.
        diag_data["bridge"] = {
            "batch_supported": getattr(api_client, "_batch_supported", None),
            "recent_jobs": list(getattr(api_client, "job_history", [])),
        }

    if coordinator and coordinator.data:
        diag_data["locks"] = [
            {
                "state": item.get(DIAG_LOCK_STATE),
                "state_fresh": item.get("state_fresh"),
                "battery": item.get(DIAG_LOCK_BATTERY),
                "settings": item.get(LOCK_SETTINGS),
                "firmware": item.get(LOCK_FIRMWARE),
                "latest_firmware": item.get(LOCK_LATEST_FIRMWARE),
                "jammed": item.get(LOCK_JAMMED),
            }
            for item in coordinator.data.values()
            if isinstance(item, dict)
        ]

    return diag_data
