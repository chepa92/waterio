"""Water.io light platform – Reminder LED (on/off + RGB color picker)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, LOGGER,
    FIELD_MANUFACTURER, FIELD_HARDWARE, FIELD_FIRMWARE, FIELD_SERIAL,
    FIELD_REMINDER_LED, FIELD_REMINDER_COLOR,
)
from .waterio import WaterioCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Water.io light entities from a config entry."""
    coordinator: WaterioCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WaterioReminderLight(coordinator, entry)])


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert '#RRGGBB' to (R, G, B) tuple. Returns red on error."""
    try:
        h = hex_color.strip().lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return 255, 0, 0


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert (R, G, B) to '#RRGGBB' string."""
    return f"#{r:02X}{g:02X}{b:02X}"


class WaterioReminderLight(CoordinatorEntity[WaterioCoordinator], LightEntity):
    """
    Represents the reminder LED on the Water.io cap.

    - On/Off maps to the Reminder LED enable TypeConfig (type 44).
    - RGB color maps to TypeConfig type 30 (reminder_color_rgb).

    This gives a full Lovelace color-wheel picker for the LED color.
    """

    _attr_has_entity_name = True
    _attr_name = "Reminder LED"
    _attr_icon = "mdi:led-on"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}

    def __init__(
        self,
        coordinator: WaterioCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.mac}-reminder_led_light"

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
        val = self.coordinator.data.get(FIELD_REMINDER_LED)
        if val is None:
            return None
        return bool(val)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        if self.coordinator.data is None:
            return None
        raw = self.coordinator.data.get(FIELD_REMINDER_COLOR)
        if raw is None:
            return None
        # _typeconfig_decode("rgb") returns list like ["#FF0000"]
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not raw:
            return None
        return _hex_to_rgb(str(raw))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn reminder LED on. If rgb_color kwarg is present, also set color."""
        rgb = kwargs.get(ATTR_RGB_COLOR)
        if rgb is not None:
            hex_color = _rgb_to_hex(*rgb)
            LOGGER.info("Setting reminder LED color to %s on %s", hex_color, self.coordinator.mac)
            await self.coordinator.async_set_typeconfig_by_key(FIELD_REMINDER_COLOR, hex_color)
        # Enable the reminder LED if it isn't already on
        if not self.is_on:
            LOGGER.info("Enabling reminder LED on %s", self.coordinator.mac)
            await self.coordinator.async_set_typeconfig_by_key(FIELD_REMINDER_LED, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the reminder LED."""
        LOGGER.info("Disabling reminder LED on %s", self.coordinator.mac)
        await self.coordinator.async_set_typeconfig_by_key(FIELD_REMINDER_LED, False)
