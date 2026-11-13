"""ESP8266 IR Blaster (mdhiggins HTTP firmware) integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import IRBlasterClient
from .const import CONF_PASSCODE, DEFAULT_PORT, DOMAIN
from .coordinator import IRBlasterCoordinator

PLATFORMS = [Platform.REMOTE, Platform.SENSOR]
STORAGE_VERSION = 1


@dataclass
class IRBlasterData:
    """Runtime data for a config entry."""

    client: IRBlasterClient
    coordinator: IRBlasterCoordinator
    store: Store
    codes: dict[str, dict[str, object]]  # {device: {command: code-str | json-list}}


type IRBlasterConfigEntry = ConfigEntry[IRBlasterData]


async def async_setup_entry(hass: HomeAssistant, entry: IRBlasterConfigEntry) -> bool:
    """Set up from a config entry."""
    client = IRBlasterClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
        entry.data.get(CONF_PASSCODE),
    )
    coordinator = IRBlasterCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_codes_{entry.entry_id}")
    codes = await store.async_load() or {}

    entry.runtime_data = IRBlasterData(client, coordinator, store, codes)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: IRBlasterConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: IRBlasterConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: IRBlasterConfigEntry) -> None:
    """Delete learned codes when the entry is removed."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}_codes_{entry.entry_id}").async_remove()
