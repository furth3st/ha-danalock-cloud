"""Bridge job handling: batching, retries, priorities and command results."""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.danalock_cloud import api as api_module
from custom_components.danalock_cloud.const import DOMAIN, LOCK_STATE

from .conftest import SERIAL


async def _setup(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity(hass: HomeAssistant, platform: str, suffix: str = "") -> str:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"danalock_cloud_{SERIAL}{suffix}")


async def _call(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call("lock", service, {ATTR_ENTITY_ID: _entity(hass, "lock")}, blocking=True)


async def test_startup_reads_everything_in_one_batch_job(hass: HomeAssistant, danalock, config_entry) -> None:
    await _setup(hass, config_entry)
    executes = [c for c in danalock.calls if c.startswith("execute:")]
    assert executes == ["execute:batch"]  # one Bluetooth session, not four
    assert hass.states.get(_entity(hass, "lock")).state == "locked"
    assert hass.states.get(_entity(hass, "sensor", "_battery")).state == "87"
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)
    assert [d.sw_version for d in devices if (DOMAIN, SERIAL) in d.identifiers] == ["0.25.0"]
    update = hass.states.get(_entity(hass, "update", "_firmware"))
    assert update.state == "on"  # 0.25.0 installed, 0.26.0 published
    assert update.attributes["latest_version"] == "0.26.0"


async def test_manual_refresh_picks_up_installed_firmware(hass: HomeAssistant, danalock, config_entry) -> None:
    """After updating in the Danalock app, 'Refresh devices' clears 'update available' at once."""
    from custom_components.danalock_cloud.const import SERVICE_REFRESH_DEVICES

    await _setup(hass, config_entry)
    assert hass.states.get(_entity(hass, "update", "_firmware")).state == "on"
    danalock.firmware = "0.26.0"  # user installed the update with the app
    await hass.services.async_call(DOMAIN, SERVICE_REFRESH_DEVICES, blocking=True)
    await hass.async_block_till_done()
    update = hass.states.get(_entity(hass, "update", "_firmware"))
    assert update.state == "off"
    assert update.attributes["installed_version"] == "0.26.0"


async def test_falls_back_to_single_reads_without_batch(hass: HomeAssistant, danalock, config_entry) -> None:
    danalock.batch_supported = False
    await _setup(hass, config_entry)
    assert hass.states.get(_entity(hass, "lock")).state == "locked"
    assert hass.states.get(_entity(hass, "sensor", "_battery")).state == "87"
    assert "execute:afi.lock.get-state" in danalock.calls
    client = hass.data[DOMAIN][config_entry.entry_id]["api_client"]
    assert client._batch_supported is False  # remembered: no more batch attempts


async def test_already_in_position_counts_as_success(hass: HomeAssistant, danalock, config_entry) -> None:
    """Bridge answers 'nothing changed' (e.g. auto-lock already locked): not an error."""
    await _setup(hass, config_entry)
    await _call(hass, "lock")  # already locked
    state = hass.states.get(_entity(hass, "lock"))
    assert state.state == "locked"
    assert state.attributes["last_command_result"] == "success"


async def test_command_retried_once_after_bridge_timeout(hass: HomeAssistant, danalock, config_entry) -> None:
    await _setup(hass, config_entry)
    danalock.operate_timeouts = 1
    await _call(hass, "unlock")
    assert hass.states.get(_entity(hass, "lock")).state == "unlocked"
    assert danalock.calls.count("execute:afi.lock.operate") == 2


async def test_lock_loading_is_waited_out(hass: HomeAssistant, danalock, config_entry) -> None:
    await _setup(hass, config_entry)
    danalock.lock_loading_once = True
    await _call(hass, "unlock")
    assert hass.states.get(_entity(hass, "lock")).state == "unlocked"


async def test_jammed_lock_is_reported(hass: HomeAssistant, danalock, config_entry) -> None:
    await _setup(hass, config_entry)
    danalock.jam = True
    with pytest.raises(HomeAssistantError, match="jammed"):
        await _call(hass, "unlock")
    state = hass.states.get(_entity(hass, "lock"))
    assert state.state == "jammed"
    assert "jammed" in state.attributes["last_command_error"]


async def test_command_result_updates_battery_without_extra_read(hass: HomeAssistant, danalock, config_entry) -> None:
    await _setup(hass, config_entry)
    danalock.calls.clear()
    danalock.battery = 55
    await _call(hass, "unlock")
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, "sensor", "_battery")).state == "55"
    assert [c for c in danalock.calls if c.startswith("execute:")] == ["execute:afi.lock.operate"]
    attrs = hass.states.get(_entity(hass, "lock")).attributes
    assert attrs["last_command"] == "unlock"
    assert attrs["last_command_duration"] is not None
    assert attrs["command_in_progress"] is None


async def test_command_jumps_ahead_of_queued_background_read(hass: HomeAssistant, danalock, config_entry) -> None:
    await _setup(hass, config_entry)
    client = hass.data[DOMAIN][config_entry.entry_id]["api_client"]
    danalock.calls.clear()
    await client._bridge_lock.acquire()  # bridge busy with something else
    read = hass.async_create_task(client.get_lock_data(SERIAL, include_battery=False))
    await asyncio.sleep(0)
    command = hass.async_create_task(client.unlock(SERIAL))
    await asyncio.sleep(0)
    client._bridge_lock.release()
    data = await read
    await command
    assert data[LOCK_STATE] is None  # the read gave way (caller keeps its last known state)
    assert danalock.calls == ["execute:afi.lock.operate"]


async def test_our_own_poll_timeout_is_not_retried(
    hass: HomeAssistant, danalock, config_entry, monkeypatch
) -> None:
    await _setup(hass, config_entry)
    monkeypatch.setattr(api_module, "JOB_POLL_TIMEOUT", 0.05)
    danalock.stuck = True
    with pytest.raises(HomeAssistantError, match="timed out"):
        await _call(hass, "unlock")
    assert danalock.calls.count("execute:afi.lock.operate") == 1
    assert hass.states.get(_entity(hass, "lock")).state == "locked"


async def test_server_hiccup_while_polling_does_not_fail_the_command(
    hass: HomeAssistant, danalock, config_entry
) -> None:
    """The unlock happened; a 502 while *asking* about it must not report failure."""
    await _setup(hass, config_entry)
    danalock.poll_errors = 2
    await _call(hass, "unlock")
    assert hass.states.get(_entity(hass, "lock")).state == "unlocked"
    assert danalock.calls.count("execute:afi.lock.operate") == 1


async def test_simultaneous_commands_run_one_after_another(hass: HomeAssistant, danalock, config_entry) -> None:
    await _setup(hass, config_entry)
    entity_id = _entity(hass, "lock")
    seen: list[tuple] = []
    def _record(event) -> None:
        new = event.data.get("new_state")
        if event.data["entity_id"] == entity_id and new is not None:
            seen.append((new.attributes.get("last_command"), new.attributes.get("command_in_progress")))

    unsub = hass.bus.async_listen("state_changed", _record)
    await asyncio.gather(_call(hass, "unlock"), _call(hass, "lock"))
    unsub()
    # after the unlock finished, the waiting lock command must show as in progress
    # (not be wiped by the first command's cleanup)
    assert ("unlock", "lock") in seen
    state = hass.states.get(entity_id)
    assert state.state == "locked"  # second command won, nothing left "in progress"
    assert state.attributes["command_in_progress"] is None
    assert danalock.calls.count("execute:afi.lock.operate") == 2


async def test_batch_not_disabled_when_the_lock_itself_fails(hass: HomeAssistant, danalock, config_entry) -> None:
    await _setup(hass, config_entry)
    client = hass.data[DOMAIN][config_entry.entry_id]["api_client"]
    client._batch_supported = None  # as if batch had never been tried
    danalock.reads_fail = True
    data = await client.get_lock_data(SERIAL, include_battery=True)
    assert data[LOCK_STATE] is None
    assert client._batch_supported is None  # singles failed too: the lock, not batch, was the problem
    danalock.reads_fail = False
    data = await client.get_lock_data(SERIAL, include_battery=True)
    assert data[LOCK_STATE] == "Locked"
    assert client._batch_supported is True


async def test_rotated_refresh_token_is_saved_immediately(hass: HomeAssistant, danalock, config_entry) -> None:
    from custom_components.danalock_cloud.const import REFRESH_TOKEN

    await _setup(hass, config_entry)
    assert config_entry.data[REFRESH_TOKEN] == "refresh-1"  # not the old, possibly revoked one


async def test_diagnostics_show_job_timings_without_secrets(hass: HomeAssistant, danalock, config_entry) -> None:
    from custom_components.danalock_cloud.diagnostics import async_get_config_entry_diagnostics

    await _setup(hass, config_entry)
    await _call(hass, "unlock")
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    jobs = diag["bridge"]["recent_jobs"]
    assert [j["operation"] for j in jobs] == ["batch", "afi.lock.operate unlock"]
    assert all(j["outcome"] == "ok" and j["seconds"] >= 0 and j["polls"] >= 1 for j in jobs)
    assert diag["bridge"]["batch_supported"] is True
    assert diag["locks"][0]["firmware"] == "0.25.0"
    text = repr(diag)
    for secret in ("correct horse", "access-", "refresh-", "old-refresh"):
        assert secret not in text


async def test_auto_lock_setting_schedules_a_recheck(hass: HomeAssistant, danalock, config_entry) -> None:
    danalock.auto_lock = 30
    await _setup(hass, config_entry)
    entity = hass.data["lock"].get_entity(_entity(hass, "lock"))
    assert hass.states.get(_entity(hass, "lock")).attributes["auto_lock"] == 30
    await _call(hass, "unlock")
    assert entity._cancel_auto_lock_check is not None
    await _call(hass, "lock")  # a new command cancels the pending re-check
    assert entity._cancel_auto_lock_check is None
