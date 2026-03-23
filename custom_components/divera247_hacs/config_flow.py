"""Config flow for DIVERA 24/7."""
from __future__ import annotations

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode

from .const import BASE_URL, CONF_ACCESS_KEY, CONF_UCR_ID, CONF_UCR_NAME, DOMAIN, JWT_URL


class DiveraConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DIVERA 24/7."""

    VERSION = 1

    def __init__(self) -> None:
        self._access_key: str = ""
        self._ucr_options: dict[str, str] = {}  # ucr_id -> display name

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Step 1: collect and validate the access key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            access_key = user_input[CONF_ACCESS_KEY].strip()
            ucr_options, error = await self._fetch_ucr(access_key)

            if error:
                errors["base"] = error
            else:
                self._access_key = access_key
                self._ucr_options = ucr_options
                return await self.async_step_select_ucr()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ACCESS_KEY): str}),
            errors=errors,
        )

    async def async_step_select_ucr(self, user_input=None) -> FlowResult:
        """Step 2: let the user pick a unit (UCR)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            ucr_id = user_input[CONF_UCR_ID]
            ucr_name = self._ucr_options.get(ucr_id, ucr_id)

            await self.async_set_unique_id(ucr_id)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"DIVERA – {ucr_name}",
                data={
                    CONF_ACCESS_KEY: self._access_key,
                    CONF_UCR_ID: ucr_id,
                    CONF_UCR_NAME: ucr_name,
                },
            )

        if not self._ucr_options:
            return self.async_abort(reason="no_units")

        ucr_selector = SelectSelector(
            SelectSelectorConfig(
                options=[
                    {"value": uid, "label": name}
                    for uid, name in self._ucr_options.items()
                ],
                mode=SelectSelectorMode.LIST,
            )
        )

        return self.async_show_form(
            step_id="select_ucr",
            data_schema=vol.Schema({vol.Required(CONF_UCR_ID): ucr_selector}),
            errors=errors,
        )

    async def _fetch_ucr(self, access_key: str) -> tuple[dict[str, str], str | None]:
        """Access Key validieren: JWT holen und UCR-Einheiten zurückgeben."""
        # Schritt 1: JWT holen – das validiert gleichzeitig den Access Key
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    JWT_URL,
                    params={"accesskey": access_key},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 401:
                        return {}, "invalid_auth"
                    if resp.status != 200:
                        return {}, "cannot_connect"
        except aiohttp.ClientError:
            return {}, "cannot_connect"

        # Schritt 2: Einheitenliste per pull/all laden
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    BASE_URL,
                    params={"accesskey": access_key},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 401:
                        return {}, "invalid_auth"
                    if resp.status != 200:
                        return {}, "cannot_connect"
                    payload = await resp.json()
        except aiohttp.ClientError:
            return {}, "cannot_connect"

        ucr_raw = payload.get("data", {}).get("ucr", {})
        if not isinstance(ucr_raw, dict) or not ucr_raw:
            # No units found – still valid key; create a single unnamed entry
            return {"0": "Standard"}, None

        options: dict[str, str] = {}
        for ucr_id, ucr_data in ucr_raw.items():
            name = ucr_data.get("name") or ucr_data.get("shortname") or str(ucr_id)
            options[str(ucr_id)] = name

        return options, None
