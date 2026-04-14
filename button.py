"""Water.io button platform – action buttons (Find My Bottle, Force Sync)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, LOGGER,
    FIELD_MANUFACTURER, FIELD_HARDWARE, FIELD_FIRMWARE, FIELD_SERIAL,
)
from .waterio import WaterioCoordinator


@dataclass(frozen=True, kw_only=True)
class WaterioButtonDescription(ButtonEntityDescription):
    """Extends ButtonEntityDescription with action identification."""
    action: str = ""


BUTTON_DESCRIPTIONS: tuple[WaterioButtonDescription, ...] = (
    WaterioButtonDescription(
        key="find_bottle",
        name="Find My Bottle",
        icon="mdi:magnify",
        action="find",
    ),
    WaterioButtonDescription(
        key="force_sync",
        name="Force Sync",
        icon="mdi:sync",
        action="sync",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Water.io button entities from a config entry."""
    coordinator: WaterioCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WaterioButton(coordinator, entry, desc)
        for desc in BUTTON_DESCRIPTIONS
    )


class WaterioButton(CoordinatorEntity[WaterioCoordinator], ButtonEntity):
    """A Water.io action button entity."""

    entity_description: WaterioButtonDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WaterioCoordinator,
        entry: ConfigEntry,
        description: WaterioButtonDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.mac}-{description.key}"

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
    def available(self) -> bool:
        """Sync stays pressable always; other buttons need BLE reachable."""
        if self.entity_description.action == "sync":
            return True
        data = self.coordinator.data
        if not data:
            return False
        return bool(data.get("ble_reachable", False))

    async def async_press(self) -> None:
        """Handle button press."""
        action = self.entity_description.action
        if action == "find":
            LOGGER.info("Find My Bottle pressed for %s", self.coordinator.mac)
            await self.coordinator.async_find_bottle()
        elif action == "sync":
            LOGGER.info("Force Sync pressed for %s", self.coordinator.mac)
            await self.coordinator.async_request_refresh()


