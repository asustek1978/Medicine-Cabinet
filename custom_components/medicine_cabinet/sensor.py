"""Summary sensors for Home Medicine Cabinet."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .storage import MedicineCabinetStore


@dataclass(frozen=True, kw_only=True)
class CabinetSensorDescription(SensorEntityDescription):
    summary_key: str


SENSORS = (
    CabinetSensorDescription(key="total", name="Упаковок", icon="mdi:package-variant", summary_key="total"),
    CabinetSensorDescription(key="expired", name="Просрочено", icon="mdi:calendar-remove", summary_key="expired"),
    CabinetSensorDescription(key="expiring", name="Скоро истекает", icon="mdi:calendar-alert", summary_key="expiring"),
    CabinetSensorDescription(key="low_stock", name="Заканчивается", icon="mdi:package-down", summary_key="low_stock"),
    CabinetSensorDescription(key="shopping", name="Нужно купить", icon="mdi:cart", summary_key="shopping"),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    store: MedicineCabinetStore = hass.data[DOMAIN]["entries"][entry.entry_id]
    async_add_entities(CabinetSensor(store, entry, description) for description in SENSORS)


class CabinetSensor(SensorEntity):
    _attr_has_entity_name = False

    def __init__(self, store: MedicineCabinetStore, entry: ConfigEntry, description: CabinetSensorDescription) -> None:
        self.store = store
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._handle_update))
        self._handle_update()

    @callback
    def _handle_update(self) -> None:
        summary = self.store.snapshot()["summary"]
        self._attr_native_value = summary.get(self.entity_description.summary_key, 0)
        self.async_write_ha_state()
