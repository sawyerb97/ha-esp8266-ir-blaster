"""Remote entities: one per transmitter output (1-4)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.components.remote import (
    ATTR_COMMAND,
    ATTR_DELAY_SECS,
    ATTR_DEVICE,
    ATTR_NUM_REPEATS,
    ATTR_TIMEOUT,
    DEFAULT_DELAY_SECS,
    RemoteEntity,
    RemoteEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import IRBlasterConfigEntry, IRBlasterData
from .api import IRBlasterError
from .const import DEFAULT_LEARN_TIMEOUT, NUM_OUTPUTS, SENDABLE_TYPES
from .coordinator import IRBlasterCoordinator
from .entity import device_info

_LOGGER = logging.getLogger(__name__)

SERVICE_SEND_JSON = "send_json"


async def async_setup_entry(hass: HomeAssistant, entry: IRBlasterConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    """Create Output 1-4 remotes (2-4 disabled by default)."""
    data = entry.runtime_data
    async_add_entities(IRBlasterRemote(entry, data, out) for out in range(1, NUM_OUTPUTS + 1))

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SEND_JSON,
        {vol.Required("commands"): vol.Any(cv.string, [dict])},
        "async_send_json",
    )


def _parse_json(value: str | list) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except ValueError as err:
        raise ServiceValidationError(f"Invalid JSON: {err}") from err
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ServiceValidationError("JSON must be an object or array of objects")
    return parsed


class IRBlasterRemote(CoordinatorEntity[IRBlasterCoordinator], RemoteEntity):
    """IR transmitter output."""

    _attr_has_entity_name = True
    _attr_supported_features = RemoteEntityFeature.LEARN_COMMAND | RemoteEntityFeature.DELETE_COMMAND

    def __init__(self, entry: IRBlasterConfigEntry, data: IRBlasterData, out: int) -> None:
        super().__init__(data.coordinator)
        self._data = data
        self._out = out
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_out{out}"
        self._attr_translation_key = "output"
        self._attr_translation_placeholders = {"output": str(out)}
        self._attr_entity_registry_enabled_default = out == 1
        self._attr_device_info = device_info(entry, data)
        self._attr_is_on = True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        gpio = (self.coordinator.data.gpio if self.coordinator.data else {}).get(f"Transmitter {self._out}")
        return {
            "output": self._out,
            "gpio": gpio,
            "learned_devices": {dev: sorted(cmds) for dev, cmds in self._data.codes.items()},
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()

    # --- sending ---------------------------------------------------------

    def _resolve(self, device: str | None, command: str) -> str | list[dict[str, Any]]:
        command = command.strip()
        if device:
            try:
                return self._data.codes[device][command]  # type: ignore[return-value]
            except KeyError:
                raise ServiceValidationError(f"No learned command '{command}' for device '{device}'") from None
        if command.startswith(("[", "{")):
            return _parse_json(command)
        if command.count(":") >= 2:
            return command  # DATA:TYPE:BITS
        raise ServiceValidationError(
            f"'{command}' is not a code (DATA:TYPE:BITS) or JSON; pass `device:` to send a learned command"
        )

    async def _send_one(self, code: str | list[dict[str, Any]], repeats: int) -> None:
        client = self._data.client
        if isinstance(code, str):
            await client.send_code(code, out=self._out, repeat=repeats, rdelay=100 if repeats > 1 else None)
        else:
            payload = [dict(c, repeat=repeats) if repeats > 1 and c.get("type") != "delay" else c for c in code]
            await client.send_json(payload, out=self._out)

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        if not self._attr_is_on:
            _LOGGER.debug("%s is off; ignoring send_command", self.entity_id)
            return
        device = kwargs.get(ATTR_DEVICE)
        repeats = int(kwargs.get(ATTR_NUM_REPEATS, 1))
        delay = float(kwargs.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS))
        codes = [self._resolve(device, c) for c in command]
        try:
            for i, code in enumerate(codes):
                if i:
                    await asyncio.sleep(delay)
                await self._send_one(code, repeats)
        except IRBlasterError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_send_json(self, commands: str | list) -> None:
        try:
            await self._data.client.send_json(_parse_json(commands), out=self._out)
        except IRBlasterError as err:
            raise HomeAssistantError(str(err)) from err

    # --- learning --------------------------------------------------------

    async def async_learn_command(self, **kwargs: Any) -> None:
        device = kwargs.get(ATTR_DEVICE)
        commands = kwargs.get(ATTR_COMMAND) or []
        timeout = int(kwargs.get(ATTR_TIMEOUT) or DEFAULT_LEARN_TIMEOUT)
        if not device:
            raise ServiceValidationError("`device` is required to learn commands")
        client = self._data.client
        notif_id = f"{self.entity_id}_learn"

        for command in commands:
            try:
                status = await client.get_status()
                baseline = status.received[0] if status.received else None
                persistent_notification.async_create(
                    self.hass,
                    f"Point the remote at the IR blaster and press **{command}** for `{device}` (within {timeout}s).",
                    title="Learn IR command",
                    notification_id=notif_id,
                )
                new = None
                for _ in range(timeout):
                    await asyncio.sleep(1)
                    status = await client.get_status()
                    top = status.received[0] if status.received else None
                    if top is not None and top != baseline:
                        new = top
                        break
                if new is None:
                    raise HomeAssistantError(f"No IR code received for '{command}' within {timeout}s")

                if new.type.lower() in SENDABLE_TYPES:
                    stored: str | list[dict[str, Any]] = new.code
                else:
                    raw = await client.get_received_raw(1)
                    if not raw:
                        raise HomeAssistantError(f"Received {new.type} code but could not read its raw timings")
                    stored = [{"type": "raw", "data": raw, "khz": 38}]
            except IRBlasterError as err:
                raise HomeAssistantError(str(err)) from err
            finally:
                persistent_notification.async_dismiss(self.hass, notif_id)

            self._data.codes.setdefault(device, {})[command] = stored
            await self._data.store.async_save(self._data.codes)
            _LOGGER.info("Learned %s/%s = %s", device, command, stored if isinstance(stored, str) else "raw")
            self.async_write_ha_state()

    async def async_delete_command(self, **kwargs: Any) -> None:
        device = kwargs.get(ATTR_DEVICE)
        commands = kwargs.get(ATTR_COMMAND) or []
        if not device or device not in self._data.codes:
            raise ServiceValidationError(f"Unknown device '{device}'")
        missing = [c for c in commands if c not in self._data.codes[device]]
        for c in commands:
            self._data.codes[device].pop(c, None)
        if not self._data.codes[device]:
            del self._data.codes[device]
        await self._data.store.async_save(self._data.codes)
        self.async_write_ha_state()
        if missing:
            raise ServiceValidationError(f"Commands not found for '{device}': {', '.join(missing)}")
