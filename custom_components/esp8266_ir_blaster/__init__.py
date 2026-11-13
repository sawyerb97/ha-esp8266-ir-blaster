"""ESP8266 IR Blaster (mdhiggins HTTP firmware) integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import IRBlasterClient

_LOGGER = logging.getLogger(__name__)


async def _mdns_resolver(hass: HomeAssistant):
    """Resolve hostnames (incl. name.local) with HA's shared mDNS-aware resolver."""
    try:
        from aiohttp_asyncmdnsresolver.api import AsyncMDNSResolver
        from homeassistant.components import zeroconf

        resolver = AsyncMDNSResolver(async_zeroconf=await zeroconf.async_get_async_instance(hass))
    except Exception as err:  # noqa: BLE001 - optional speed-up only
        _LOGGER.debug("mDNS resolver unavailable, using plain hostnames: %s", err)
        return None

    async def resolve(host: str) -> str | None:
        infos = await resolver.resolve(host, 0)
        return infos[0]["host"] if infos else None

    return resolve
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


_SUFFIXES = ("_out1", "_out2", "_out3", "_out4", "_last_received", "_last_sent")


def _migrate_registry(hass: HomeAssistant, entry: IRBlasterConfigEntry) -> None:
    """v0.1.0 keyed IDs on host:port; move to entry_id so host changes don't duplicate.

    Keeps the oldest entity per suffix (preserving user renames/areas) and drops duplicates.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    eid = entry.entry_id
    entities = sorted(er.async_entries_for_config_entry(ent_reg, eid), key=lambda e: e.created_at)
    keep_device: str | None = None
    for ent in entities:
        suffix = next((s for s in _SUFFIXES if ent.unique_id.endswith(s)), None)
        if suffix is None or ent.unique_id == f"{eid}{suffix}":
            continue
        new_uid = f"{eid}{suffix}"
        if ent_reg.async_get_entity_id(ent.domain, DOMAIN, new_uid):
            ent_reg.async_remove(ent.entity_id)
            continue
        ent_reg.async_update_entity(ent.entity_id, new_unique_id=new_uid)
        keep_device = keep_device or ent.device_id
    devices = dr.async_entries_for_config_entry(dev_reg, eid)
    if keep_device is None and devices:
        keep_device = min(devices, key=lambda d: d.created_at).id
    for dev in devices:
        if dev.id == keep_device:
            if (DOMAIN, eid) not in dev.identifiers:
                dev_reg.async_update_device(dev.id, new_identifiers={(DOMAIN, eid)})
        elif not er.async_entries_for_device(ent_reg, dev.id, include_disabled_entities=True):
            dev_reg.async_remove_device(dev.id)


async def async_setup_entry(hass: HomeAssistant, entry: IRBlasterConfigEntry) -> bool:
    """Set up from a config entry."""
    _migrate_registry(hass, entry)
    client = IRBlasterClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
        entry.data.get(CONF_PASSCODE),
        await _mdns_resolver(hass),
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
