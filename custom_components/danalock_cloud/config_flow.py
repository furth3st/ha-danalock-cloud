"""Config flow for Danalock Cloud."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .api import DanalockApiAuthError, DanalockApiClient, DanalockApiClientError
from .const import (
    ACCESS_TOKEN,
    CONF_OPTIMISTIC_MODE,
    CONF_PASSWORD,
    CONF_TOTP,
    CONF_UPDATE_INTERVAL,
    CONF_USERNAME,
    DOMAIN,
    REFRESH_TOKEN,
    TOKEN_EXPIRES_AT,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_TOTP): str,
    }
)
STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_TOTP): str,
    }
)

DEFAULT_UPDATE_MINUTES = int(UPDATE_INTERVAL.total_seconds() / 60)


async def _validate_credentials(
    flow: ConfigFlow, username: str, password: str, totp: str | None = None
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """Try to log in. Returns (token_data, errors)."""
    client = DanalockApiClient(flow.hass, username=username, password=password)
    try:
        tokens = await client.authenticate(username, password, totp)
    except DanalockApiAuthError as err:
        _LOGGER.info("Danalock login rejected for %s: %s", username, err)
        return None, {"base": "invalid_auth"}
    except DanalockApiClientError as err:
        _LOGGER.warning("Could not reach Danalock while validating %s: %s", username, err)
        return None, {"base": "cannot_connect"}
    except Exception:  # noqa: BLE001 - never let an unexpected error crash the UI
        _LOGGER.exception("Unexpected error validating Danalock credentials")
        return None, {"base": "unknown"}
    return tokens, {}


class DanalockConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Danalock Cloud."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DanalockOptionsFlowHandler:
        """Return the options flow handler."""
        return DanalockOptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()

            tokens, errors = await _validate_credentials(
                self, username, password, user_input.get(CONF_TOTP)
            )
            if tokens is not None:
                return self.async_create_entry(
                    title=username,
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        ACCESS_TOKEN: tokens[ACCESS_TOKEN],
                        REFRESH_TOKEN: tokens.get(REFRESH_TOKEN),
                        TOKEN_EXPIRES_AT: tokens[TOKEN_EXPIRES_AT],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, {CONF_USERNAME: (user_input or {}).get(CONF_USERNAME, "")}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start re-authentication after Danalock rejected the stored credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the current password and validate it."""
        entry = self._get_reauth_entry()
        username = entry.data[CONF_USERNAME]
        errors: dict[str, str] = {}
        if user_input is not None:
            tokens, errors = await _validate_credentials(
                self, username, user_input[CONF_PASSWORD], user_input.get(CONF_TOTP)
            )
            if tokens is not None:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        ACCESS_TOKEN: tokens[ACCESS_TOKEN],
                        REFRESH_TOKEN: tokens.get(REFRESH_TOKEN),
                        TOKEN_EXPIRES_AT: tokens[TOKEN_EXPIRES_AT],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={"username": username},
            errors=errors,
        )


class DanalockOptionsFlowHandler(OptionsFlow):
    """Options flow. `self.config_entry` is provided by Home Assistant (do not assign it)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_UPDATE_INTERVAL,
                    default=options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_MINUTES),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
                vol.Optional(
                    CONF_OPTIMISTIC_MODE,
                    default=options.get(CONF_OPTIMISTIC_MODE, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
