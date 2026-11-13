"""Sensors: last received / last sent IR code."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import IRBlasterConfigEntry, IRBlasterData
from .coordinator import IRBlasterCoordinator
from .entity import device_info


async def async_setup_entry(hass: HomeAssistant, entry: IRBlasterConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    data = entry.runtime_data
    async_add_entities([LastCodeSensor(entry, data, received=True), LastCodeSensor(entry, data, received=False)])


class LastCodeSensor(CoordinatorEntity[IRBlasterCoordinator], SensorEntity):
    """Most recent received or transmitted code (DATA:TYPE:BITS)."""

    _attr_has_entity_name = True

    def __init__(self, entry: IRBlasterConfigEntry, data: IRBlasterData, received: bool) -> None:
        super().__init__(data.coordinator)
        self._received = received
        key = "last_received" if received else "last_sent"
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{key}"
        self._attr_icon = "mdi:remote" if received else "mdi:led-on"
        self._attr_device_info = device_info(entry, data)
        if not received:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def _codes(self):
        d = self.coordinator.data
        if d is None:
            return []
        return d.received if self._received else d.sent

    @property
    def native_value(self) -> str | None:
        codes = self._codes()
        return codes[0].code if codes else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        codes = self._codes()
        attrs: dict[str, Any] = {}
        if codes:
            c = codes[0]
            attrs.update(time=c.time, data=c.data, type=c.type, bits=c.bits, address=c.address)
            attrs["history"] = [x.code for x in codes]
        if self._received:
            if self.coordinator.last_raw:
                attrs["raw"] = self.coordinator.last_raw
            if self.coordinator.data:
                attrs["gpio"] = self.coordinator.data.gpio
        return attrs
