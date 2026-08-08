"""Config flow: ask for the bridge URL, prove it answers, done."""

from __future__ import annotations

from typing import Any

import aiohttp
import async_timeout
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CHAT_PATH, CONF_TIMEOUT, CONF_URL, DEFAULT_TIMEOUT, DEFAULT_URL, DOMAIN


async def _probe(hass, url: str, timeout: float) -> None:
    """Raise if the bridge isn't there. Catching this at setup beats
    discovering it when you're mid-sentence with earbuds in."""
    session = async_get_clientsession(hass)
    async with async_timeout.timeout(timeout):
        resp = await session.post(url.rstrip("/") + CHAT_PATH, json={
            "model": "probe",
            "messages": [],          # empty: the shim replies without injecting
            "stream": False,
        })
        resp.raise_for_status()
        await resp.json()


class VoiceBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL]
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            try:
                await _probe(self.hass, url, user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))
            except (TimeoutError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "bad_reply"
            if not errors:
                return self.async_create_entry(title="Voice Bridge", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_URL, default=DEFAULT_URL): str,
                vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): int,
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return VoiceBridgeOptionsFlow()


class VoiceBridgeOptionsFlow(OptionsFlow):
    """Let the URL and timeout be edited without re-adding the entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_URL,
                             default=current.get(CONF_URL, DEFAULT_URL)): str,
                vol.Optional(CONF_TIMEOUT,
                             default=current.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)): int,
            }),
        )
