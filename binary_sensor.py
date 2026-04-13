"""Water.io binary sensor platform – cap closed, charging."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, LOGGER,
    FIELD_MANUFACTURER, FIELD_HARDWARE, FIELD_FIRMWARE, FIELD_SERIAL,
    FIELD_CAP_CLOSED, FIELD_IS_CHARGING, FIELD_IS_ACTIVE, FIELD_NEED_DRINK,
)
from .waterio import WaterioCoordinator


@dataclass(frozen=True, kw_only=True)
class WaterioBinarySensorDescription(BinarySensorEntityDescription):
    """Extends BinarySensorEntityDescription with the coordinator field key."""
    field: str = ""
    enabled_default: bool = True


BINARY_SENSOR_DESCRIPTIONS: tuple[WaterioBinarySensorDescription, ...] = (
    WaterioBinarySensorDescription(
        key="cap_closed",
        field=FIELD_CAP_CLOSED,
        name="Cap Closed",
        device_class=BinarySensorDeviceClass.DOOR,   # ON = open, OFF = closed
        # Note: HA DOOR class: ON = open, OFF = closed.
        # We invert below so the state is intuitive.
        icon="mdi:bottle-tonic-plus",
    ),
    WaterioBinarySensorDescription(
        key="is_charging",
        field=FIELD_IS_CHARGING,
        name="Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        icon="mdi:battery-charging",
    ),
    WaterioBinarySensorDescription(
        key="is_active",
        field=FIELD_IS_ACTIVE,
        name="Active Mode",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:bottle-tonic-outline",
    ),
    WaterioBinarySensorDescription(
        key="need_to_drink",
        field=FIELD_NEED_DRINK,
        name="Drink Reminder",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        icon="mdi:cup-water",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Water.io binary sensors from a config entry."""
    LOGGER.debug("binary_sensor async_setup_entry  entry_id=%s", entry.entry_id)
    coordinator: WaterioCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WaterioBinarySensor(coordinator, entry, desc)
        for desc in BINARY_SENSOR_DESCRIPTIONS
    )


class WaterioBinarySensor(CoordinatorEntity[WaterioCoordinator], BinarySensorEntity):
    """A single Water.io binary sensor entity backed by the coordinator."""

    entity_description: WaterioBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WaterioCoordinator,
        entry: ConfigEntry,
        description: WaterioBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.mac}-{description.key}"
        self._attr_entity_registry_enabled_default = description.enabled_default

    @property
    def device_info(self) -> DeviceInfo:
        data: dict[str, Any] = self.coordinator.data or {}
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.mac)},
            name=self.coordinator.device_name,
            manufacturer=data.get(FIELD_MANUFACTURER, "Water.io"),
            model=data.get(FIELD_HARDWARE),
            sw_version=data.get(FIELD_FIRMWARE),
            serial_number=data.get(FIELD_SERIAL),
        )

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self.entity_description.field)
        if value is None:
            return None

        raw = bool(value)

        # For DOOR class: HA convention is ON=open, OFF=closed.
        # The cap stores is_closed=True when closed, so invert for DOOR.
        if self.entity_description.key == "cap_closed":
            return not raw   # closed cap → OFF (door closed), open cap → ON (door open)

        return raw
