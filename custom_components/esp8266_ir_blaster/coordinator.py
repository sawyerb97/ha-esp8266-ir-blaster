"""Polling coordinator: device availability + received-code detection."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import IRBlasterAuthError, IRBlasterClient, IRBlasterError, IRCode, IRStatus
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN, EVENT_CODE_RECEIVED

_LOGGER = logging.getLogger(__name__)


class IRBlasterCoordinator(DataUpdateCoordinator[IRStatus]):
    """Polls the status page; fires an event when a new IR code is received."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: IRBlasterClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {client.host}",
            update_interval=timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        )
        self.client = client
        self._last_top: IRCode | None = None
        self._primed = False
        self.last_raw: list[int] | None = None

    async def _async_update_data(self) -> IRStatus:
        try:
            status = await self.client.get_status()
        except IRBlasterAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except IRBlasterError as err:
            raise UpdateFailed(str(err)) from err

        top = status.received[0] if status.received else None
        if self._primed and top is not None and top != self._last_top:
            # Raw timings help users capture codes the firmware can't name.
            try:
                self.last_raw = await self.client.get_received_raw(1)
            except IRBlasterError:
                self.last_raw = None
            self.hass.bus.async_fire(
                EVENT_CODE_RECEIVED,
                {
                    "entry_id": self.config_entry.entry_id,
                    "host": self.client.host,
                    "code": top.code,
                    "data": top.data,
                    "type": top.type,
                    "bits": top.bits,
                    "address": top.address,
                },
            )
        self._last_top = top
        self._primed = True
        return status
