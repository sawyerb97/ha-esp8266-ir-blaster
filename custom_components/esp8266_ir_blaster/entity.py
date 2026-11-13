"""Shared entity helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, MANUFACTURER, MODEL

if TYPE_CHECKING:
    from . import IRBlasterConfigEntry, IRBlasterData


def device_info(entry: IRBlasterConfigEntry, data: IRBlasterData) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model=MODEL,
        configuration_url=data.client.base_url,
    )
