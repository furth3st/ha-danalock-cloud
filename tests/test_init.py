"""Setup, runtime and resilience tests."""
from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.danalock_cloud.const import (
    CONF_OPTIMISTIC_MODE,
    CONF_PASSWORD,
    DOMAIN,
    REFRESH_TOKEN,
    SERVICE_REFRESH_DEVICES,
)

from .conftest import PASSWORD, SERIAL


async def _setup(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _lock_entity(hass: HomeAssistant) -> str:
    return er.async_get(hass).async_get_entity_id("lock", DOMAIN, f"danalock_cloud_{SERIAL}")


def _battery_entity(hass: HomeAssistant) -> str:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"danalock_cloud_{SERIAL}_battery")


async def test_setup_creates_lock_and_battery(hass: HomeAssistant, danalock, config_entry) -> None:
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(_lock_entity(hass)).state == "locked"
    assert hass.states.get(_battery_entity(hass)).state == "87"
    assert len(hass.states.async_entity_ids("lock")) == 1  # the "mwm" lock is skipped
    # expired token was refreshed (no password login needed)
    assert danalock.calls[0] == "token:refresh_token"


async def test_outage_at_startup_retries_and_keeps_credentials(hass: HomeAssistant, danalock, config_entry) -> None:
    """Regression: a Danalock outage used to delete the stored password permanently."""
    danalock.token_outage = True
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert config_entry.data[CONF_PASSWORD] == PASSWORD
    assert config_entry.data[REFRESH_TOKEN] == "old-refresh"
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)  # no bogus reauth prompt


async def test_rejected_refresh_falls_back_to_password(hass: HomeAssistant, danalock, config_entry) -> None:
    danalock.refresh_valid = False
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert "token:password" in danalock.calls


async def test_rejected_password_asks_for_reauth_once(hass: HomeAssistant, danalock, config_entry) -> None:
    danalock.refresh_valid = False
    danalock.password = "changed elsewhere"
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    # exactly one password attempt: no hammering that could lock the account
    assert danalock.calls.count("token:password") == 1


async def test_expired_access_token_is_refreshed_transparently(hass: HomeAssistant, danalock, config_entry) -> None:
    danalock.reject_access_token_once = True
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED


async def test_danalock_server_error_on_locks_retries(hass: HomeAssistant, danalock, config_entry) -> None:
    danalock.locks_status = 502
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_bridge_busy_is_retried(hass: HomeAssistant, danalock, config_entry) -> None:
    danalock.busy_polls = 2
    await _setup(hass, config_entry)
    assert hass.states.get(_lock_entity(hass)).state == "locked"


async def test_busy_poll_keeps_last_known_state(hass: HomeAssistant, danalock, config_entry) -> None:
    await _setup(hass, config_entry)
    danalock.busy_polls = 100  # bridge stays busy for the next refresh
    await hass.services.async_call(DOMAIN, SERVICE_REFRESH_DEVICES, blocking=True)
    await hass.async_block_till_done()
    state = hass.states.get(_lock_entity(hass))
    assert state.state == "locked"  # not "unknown"/"unavailable"


async def test_unlock_and_lock(hass: HomeAssistant, danalock, config_entry) -> None:
    await _setup(hass, config_entry)
    entity_id = _lock_entity(hass)
    await hass.services.async_call("lock", "unlock", {ATTR_ENTITY_ID: entity_id}, blocking=True)
    assert hass.states.get(entity_id).state == "unlocked"
    await hass.services.async_call("lock", "lock", {ATTR_ENTITY_ID: entity_id}, blocking=True)
    assert hass.states.get(entity_id).state == "locked"


async def test_failed_command_raises_and_rolls_back_optimistic_state(
    hass: HomeAssistant, danalock, config_entry
) -> None:
    """A door lock must never silently pretend it locked/unlocked."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_OPTIMISTIC_MODE: True})
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    entity_id = _lock_entity(hass)
    danalock.operate_fails = True
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("lock", "unlock", {ATTR_ENTITY_ID: entity_id}, blocking=True)
    assert hass.states.get(entity_id).state == "locked"


async def test_unload(hass: HomeAssistant, danalock, config_entry) -> None:
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert hass.services.has_service(DOMAIN, SERVICE_REFRESH_DEVICES)
