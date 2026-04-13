"""Water.io select platform – enum settings via TypeConfig (0x4D)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, LOGGER,
    FIELD_MANUFACTURER, FIELD_HARDWARE, FIELD_FIRMWARE, FIELD_SERIAL,
    FIELD_REMINDER_PATTERN, FIELD_BOTTLE_VOLUME,
)
from .waterio import WaterioCoordinator


@dataclass(frozen=True, kw_only=True)
class WaterioSelectDescription(SelectEntityDescription):
    """Extends SelectEntityDescription with waterio-specific metadata."""
    field: str = ""
    # Maps option label (str) -> raw integer value sent to device
    option_values: dict[str, int] = None   # type: ignore[assignment]
    enabled_default: bool = True


# reminder_pattern: 1=bounce, 2=pulse, 3=snake
_PATTERN_OPTIONS: dict[str, int] = {
    "bounce": 1,
    "pulse":  2,
    "snake":  3,
}

# bottle_volume_type: 0=500mL, 1=750mL
_BOTTLE_OPTIONS: dict[str, int] = {
    "500 mL": 0,
    "750 mL": 1,
}


SELECT_DESCRIPTIONS: tuple[WaterioSelectDescription, ...] = (
    WaterioSelectDescription(
        key="reminder_pattern",
        field=FIELD_REMINDER_PATTERN,
        name="Reminder LED Pattern",
        options=list(_PATTERN_OPTIONS.keys()),
        option_values=_PATTERN_OPTIONS,
        icon="mdi:led-strip-variant",
    ),
    WaterioSelectDescription(
        key="bottle_volume_type",
        field=FIELD_BOTTLE_VOLUME,
        name="Bottle Volume",
        options=list(_BOTTLE_OPTIONS.keys()),
        option_values=_BOTTLE_OPTIONS,
        icon="mdi:bottle-wine",
    ),
)


# Reverse maps: raw device integer -> friendly option label
_PATTERN_REVERSE: dict[int, str] = {v: k for k, v in _PATTERN_OPTIONS.items()}
_BOTTLE_REVERSE: dict[int, str] = {v: k for k, v in _BOTTLE_OPTIONS.items()}
_REVERSE_MAPS: dict[str, dict[int, str]] = {
    "reminder_pattern":  _PATTERN_REVERSE,
    "bottle_volume_type": _BOTTLE_REVERSE,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Water.io select entities from a config entry."""
    coordinator: WaterioCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WaterioSelect(coordinator, entry, desc)
        for desc in SELECT_DESCRIPTIONS
    )


class WaterioSelect(CoordinatorEntity[WaterioCoordinator], SelectEntity):
    """A writable enum setting entity for a Water.io cap."""

    entity_description: WaterioSelectDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WaterioCoordinator,
        entry: ConfigEntry,
        description: WaterioSelectDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.mac}-{description.key}"
        self._attr_options = description.options
        self._attr_entity_registry_enabled_default = description.enabled_default
        self._attr_entity_category = EntityCategory.CONFIG

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
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        raw_value = self.coordinator.data.get(self.entity_description.field)
        if raw_value is None:
            return None
        reverse_map = _REVERSE_MAPS.get(self.entity_description.key, {})
        # raw_value may be stored as int or as the label string
        if isinstance(raw_value, str) and raw_value in self.entity_description.options:
            return raw_value
        try:
            return reverse_map.get(int(raw_value))
        except (TypeError, ValueError):
            return None

    async def async_select_option(self, option: str) -> None:
        """Handle user selecting a new option."""
        option_values = self.entity_description.option_values or {}
        if option not in option_values:
            LOGGER.warning("Unknown option '%s' for %s", option, self.entity_description.key)
            return
        raw_int = option_values[option]
        await self.coordinator.async_set_typeconfig_by_key(
            self.entity_description.field, raw_int
        )
        LOGGER.info(
            "Select SET %s = %s (%d) for %s",
            self.entity_description.field, option, raw_int, self.coordinator.mac,
        )
