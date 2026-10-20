"""Config / reauth / options flow tests."""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.danalock_cloud.const import (
    ACCESS_TOKEN,
    CONF_OPTIMISTIC_MODE,
    CONF_PASSWORD,
    CONF_UPDATE_INTERVAL,
    CONF_USERNAME,
    DOMAIN,
    TOKEN_EXPIRES_AT,
)

from .conftest import PASSWORD, USERNAME


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_user_flow_creates_entry_with_expiry(hass: HomeAssistant, danalock) -> None:
    """Regression for #11: Danalock only returns expires_in; setup must still succeed."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: f"  {USERNAME} ", CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME  # whitespace trimmed
    assert result["data"][ACCESS_TOKEN] == "access-1"
    assert result["data"][TOKEN_EXPIRES_AT] > 0
    assert result["result"].unique_id == USERNAME.lower()


async def test_user_flow_expires_in_as_string(hass: HomeAssistant, danalock) -> None:
    danalock.expires_in = "3600"
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_invalid_auth(hass: HomeAssistant, danalock) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_outage_is_cannot_connect(hass: HomeAssistant, danalock) -> None:
    danalock.token_outage = True
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_with_two_factor(hass: HomeAssistant, danalock) -> None:
    """2FA accounts work: the code is sent once and never stored."""
    danalock.require_totp = "123456"
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD, "totp": "123 456"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert danalock.token_forms[-1]["totp"] == "123456"
    assert "totp" not in result["data"]


async def test_user_flow_already_configured(hass: HomeAssistant, danalock, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME.upper(), CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow(hass: HomeAssistant, danalock, config_entry) -> None:
    """Regression: reauth used an undefined schema and crashed."""
    config_entry.add_to_hass(hass)
    danalock.password = "new password"
    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "still wrong"})
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new password"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new password"
    assert config_entry.data[TOKEN_EXPIRES_AT] > 0


async def test_options_flow(hass: HomeAssistant, danalock, config_entry) -> None:
    """Regression for #8: opening options returned HTTP 500 on HA >= 2025.12."""
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UPDATE_INTERVAL: 10, CONF_OPTIMISTIC_MODE: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_UPDATE_INTERVAL: 10, CONF_OPTIMISTIC_MODE: True}
