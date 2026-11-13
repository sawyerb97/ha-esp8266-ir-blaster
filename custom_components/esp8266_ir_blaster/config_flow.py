"""Config flow for ESP8266 IR Blaster."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import IRBlasterAuthError, IRBlasterClient, IRBlasterError
from .ircodes import convert_file
from .library import LibraryError, get_library
from .const import CONF_PASSCODE, CONF_SCAN_INTERVAL, DEFAULT_PORT, DEFAULT_SCAN_INTERVAL, DOMAIN

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class IRBlasterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup."""

    VERSION = 1

    async def _validate(self, host: str, port: int, passcode: str | None) -> tuple[dict[str, str], str]:
        client = IRBlasterClient(async_get_clientsession(self.hass), host, port, passcode)
        try:
            status = await client.get_status()
        except IRBlasterAuthError:
            return {"base": "invalid_auth"}, ""
        except IRBlasterError:
            return {"base": "cannot_connect"}, ""
        return {}, status.title

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            errors, title = await self._validate(host, port, user_input.get(CONF_PASSCODE))
            if not errors:
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME) or title or f"IR Blaster {host}",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_PASSCODE: user_input.get(CONF_PASSCODE, ""),
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Optional(CONF_PASSCODE, default=""): PASSWORD,
                vol.Optional(CONF_NAME, default=""): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(schema, user_input), errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, _ = await self._validate(entry.data[CONF_HOST], entry.data[CONF_PORT], user_input[CONF_PASSCODE])
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_PASSCODE: user_input[CONF_PASSCODE]})
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=vol.Schema({vol.Required(CONF_PASSCODE): PASSWORD}), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = user_input[CONF_HOST].strip(), int(user_input[CONF_PORT])
            errors, _ = await self._validate(host, port, user_input.get(CONF_PASSCODE))
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=f"{host}:{port}",
                    data_updates={CONF_HOST: host, CONF_PORT: port, CONF_PASSCODE: user_input.get(CONF_PASSCODE, "")},
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
                vol.Required(CONF_PORT, default=entry.data[CONF_PORT]): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Optional(CONF_PASSCODE, default=entry.data.get(CONF_PASSCODE, "")): PASSWORD,
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return IRBlasterOptionsFlow()


class IRBlasterOptionsFlow(OptionsFlow):
    """Menu: add codes from the public library, paste a .ir file, delete, settings."""

    def __init__(self) -> None:
        self._category: str | None = None
        self._brand: str | None = None
        self._path: str | None = None
        self._codes: dict[str, Any] = {}
        self._skipped: list[str] = []
        self._result = ""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["library", "paste", "delete", "settings"])

    # --- settings ------------------------------------------------------
    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=2, max=300)),
                }
            ),
        )

    # --- library browsing ------------------------------------------------
    async def async_step_library(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        try:
            index = await get_library(self.hass).async_index()
        except LibraryError as err:
            return self.async_abort(reason="library_unavailable", description_placeholders={"error": str(err)})
        if user_input is not None:
            self._category = user_input["category"]
            return await self.async_step_library_brand()
        opts = [SelectOptionDict(value=c, label=f"{c} ({sum(len(m) for m in b.values())})") for c, b in sorted(index.items())]
        return self.async_show_form(
            step_id="library",
            data_schema=vol.Schema({vol.Required("category"): SelectSelector(SelectSelectorConfig(options=opts, mode=SelectSelectorMode.DROPDOWN))}),
        )

    async def async_step_library_brand(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        index = await get_library(self.hass).async_index()
        brands = index[self._category]
        if user_input is not None:
            self._brand = user_input["brand"]
            return await self.async_step_library_model()
        opts = [SelectOptionDict(value=b, label=f"{b} ({len(m)})") for b, m in sorted(brands.items(), key=lambda x: x[0].lower())]
        return self.async_show_form(
            step_id="library_brand",
            data_schema=vol.Schema({vol.Required("brand"): SelectSelector(SelectSelectorConfig(options=opts, mode=SelectSelectorMode.DROPDOWN))}),
            description_placeholders={"category": self._category},
        )

    async def async_step_library_model(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        index = await get_library(self.hass).async_index()
        models = index[self._category][self._brand]
        errors: dict[str, str] = {}
        if user_input is not None:
            self._path = models[user_input["model"]]
            try:
                text = await get_library(self.hass).async_fetch(self._path)
            except LibraryError:
                errors["base"] = "fetch_failed"
            else:
                self._codes, self._skipped = convert_file(text)
                if not self._codes:
                    errors["base"] = "no_usable_codes"
                else:
                    self._result = ""
                    return await self.async_step_review()
        opts = [SelectOptionDict(value=m, label=m) for m in sorted(models, key=str.lower)]
        return self.async_show_form(
            step_id="library_model",
            data_schema=vol.Schema({vol.Required("model"): SelectSelector(SelectSelectorConfig(options=opts, mode=SelectSelectorMode.DROPDOWN))}),
            description_placeholders={"brand": self._brand, "category": self._category},
            errors=errors,
        )

    # --- paste a .ir file ----------------------------------------------
    async def async_step_paste(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._codes, self._skipped = convert_file(user_input["ir_text"])
            if not self._codes:
                errors["base"] = "no_usable_codes"
            else:
                self._path, self._category, self._brand, self._result = "pasted", "", "", ""
                return await self.async_step_review()
        return self.async_show_form(
            step_id="paste",
            data_schema=vol.Schema({vol.Required("ir_text"): TextSelector(TextSelectorConfig(multiline=True))}),
            errors=errors,
        )

    # --- review / test / save --------------------------------------------
    def _default_name(self) -> str:
        if not self._brand:
            return "my_device"
        cat = (self._category or "device").split(" ")[0].lower().rstrip("s")
        return f"{self._brand}_{cat}".lower().replace(" ", "_")

    async def async_step_review(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        data = self.config_entry.runtime_data
        if user_input is not None:
            action = user_input["action"]
            if action == "back":
                return await (self.async_step_library_model() if self._brand else self.async_step_paste())
            if action == "test":
                fw = self._codes[user_input["test_button"]]
                try:
                    await data.client.send_json(fw, out=int(user_input["output"]))
                    self._result = f"Sent **{user_input['test_button']}** on output {user_input['output']}. Did the device respond?"
                except IRBlasterError as err:
                    self._result = f"Send failed: {err}"
                return self._review_form(user_input)
            device = user_input["device"].strip()
            data.codes[device] = {**data.codes.get(device, {}), **self._codes}
            await data.store.async_save(data.codes)
            data.coordinator.async_update_listeners()
            return self.async_create_entry(data=dict(self.config_entry.options))
        return self._review_form(None)

    def _review_form(self, prev: dict[str, Any] | None) -> ConfigFlowResult:
        names = list(self._codes)
        power = next((n for n in names if n.lower().startswith("power")), names[0])
        prev = prev or {}
        schema = vol.Schema(
            {
                vol.Required("device", default=prev.get("device", self._default_name())): str,
                vol.Required("test_button", default=prev.get("test_button", power)): SelectSelector(
                    SelectSelectorConfig(options=names, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Required("output", default=prev.get("output", "1")): SelectSelector(
                    SelectSelectorConfig(options=["1", "2", "3", "4"], mode=SelectSelectorMode.LIST)
                ),
                vol.Required("action", default="test"): SelectSelector(
                    SelectSelectorConfig(options=["test", "save", "back"], translation_key="review_action", mode=SelectSelectorMode.LIST)
                ),
            }
        )
        return self.async_show_form(
            step_id="review",
            data_schema=schema,
            description_placeholders={
                "source": self._path or "",
                "count": str(len(self._codes)),
                "buttons": ", ".join(names[:40]) + (" …" if len(names) > 40 else ""),
                "skipped": str(len(self._skipped)),
                "result": self._result or "Pick a button and choose **Test** to send it, then **Save**.",
            },
        )

    # --- delete ------------------------------------------------------
    async def async_step_delete(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        data = self.config_entry.runtime_data
        if not data.codes:
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            for dev in user_input["devices"]:
                data.codes.pop(dev, None)
            await data.store.async_save(data.codes)
            data.coordinator.async_update_listeners()
            return self.async_create_entry(data=dict(self.config_entry.options))
        return self.async_show_form(
            step_id="delete",
            data_schema=vol.Schema(
                {vol.Required("devices"): SelectSelector(SelectSelectorConfig(options=sorted(data.codes), multiple=True, mode=SelectSelectorMode.LIST))}
            ),
        )
