"""Water.io switch platform – boolean settings via TypeConfig (0x4D) and dedicated opcodes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, LOGGER,
    FIELD_MANUFACTURER, FIELD_HARDWARE, FIELD_FIRMWARE, FIELD_SERIAL,
    FIELD_SILENT_MODE,
    FIELD_REMINDER_LOGIC,
    FIELD_OPEN_CLOSE_LED,
    FIELD_DAR, FIELD_DBR,
    FIELD_STATUS_LED, FIELD_DEMO_MODE,
    FIELD_LED_OFF_HOURS, FIELD_SHABBAT_MODE, FIELD_LED_OFF_CHARGER,
)
from .waterio import WaterioCoordinator


@dataclass(frozen=True, kw_only=True)
class WaterioSwitchDescription(SwitchEntityDescription):
    """Extends SwitchEntityDescription with waterio-specific metadata."""
    field: str = ""
    # Use silent-mode dedicated command instead of TypeConfig 0x4D
    use_silent_cmd: bool = False
    enabled_default: bool = True


SWITCH_DESCRIPTIONS: tuple[WaterioSwitchDescription, ...] = (
    # ── Silence / reminders ───────────────────────────────────────────────
    WaterioSwitchDescription(
        key="silent_mode",
        field=FIELD_SILENT_MODE,
        name="Silent Mode",
        icon="mdi:bell-off",
        use_silent_cmd=True,
    ),
    WaterioSwitchDescription(
        key="reminder_logic",
        field=FIELD_REMINDER_LOGIC,
        name="Persistent Reminders",
        icon="mdi:bell-alert",
    ),
    # ── LEDs ──────────────────────────────────────────────────────────────
    # NOTE: Reminder LED on/off + color is controlled by the
    # 'Reminder LED' light entity (light.waterio). Do NOT add
    # enable_reminder_led here — it would duplicate that control.
    WaterioSwitchDescription(
        key="enable_status_led",
        field=FIELD_STATUS_LED,
        name="Status LED",
        icon="mdi:led-outline",
    ),
    WaterioSwitchDescription(
        key="enable_open_close_led",
        field=FIELD_OPEN_CLOSE_LED,
        name="Open/Close LED",
        icon="mdi:led-variant-on",
    ),
    WaterioSwitchDescription(
        key="led_off_outside_hours",
        field=FIELD_LED_OFF_HOURS,
        name="LED Off Outside Active Hours",
        icon="mdi:led-off",
    ),
    WaterioSwitchDescription(
        key="led_off_in_charger",
        field=FIELD_LED_OFF_CHARGER,
        name="LED Off While Charging",
        icon="mdi:led-off",
    ),
    # ── DAR / DBR ─────────────────────────────────────────────────────────
    WaterioSwitchDescription(
        key="dar_drink_after_refill",
        field=FIELD_DAR,
        name="Drink-After-Refill (DAR)",
        icon="mdi:cup-water",
    ),
    WaterioSwitchDescription(
        key="dbr_drink_before_refill",
        field=FIELD_DBR,
        name="Drink-Before-Refill (DBR)",
        icon="mdi:cup-outline",
    ),
    # ── Misc ──────────────────────────────────────────────────────────────
    WaterioSwitchDescription(
        key="enable_demo_mode",
        field=FIELD_DEMO_MODE,
        name="Demo Mode",
        icon="mdi:presentation",
    ),
    WaterioSwitchDescription(
        key="enable_shabbat_mode",
        field=FIELD_SHABBAT_MODE,
        name="Shabbat Mode",
        icon="mdi:candelabra",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Water.io switch entities from a config entry."""
    coordinator: WaterioCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WaterioSwitch(coordinator, entry, desc)
        for desc in SWITCH_DESCRIPTIONS
    )


class WaterioSwitch(CoordinatorEntity[WaterioCoordinator], SwitchEntity):
    """A writable boolean setting entity for a Water.io cap."""

    entity_description: WaterioSwitchDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WaterioCoordinator,
        entry: ConfigEntry,
        description: WaterioSwitchDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.mac}-{description.key}"
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
    def available(self) -> bool:
        """Unavailable when BLE device is not reachable (can't write settings)."""
        data = self.coordinator.data
        if not data:
            return False
        return bool(data.get("ble_reachable", False))

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self.entity_description.field)
        if value is None:
            return None
        return bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on (enable the setting)."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off (disable the setting)."""
        await self._set(False)

    async def _set(self, enabled: bool) -> None:
        if self.entity_description.use_silent_cmd:
            await self.coordinator.async_set_silent_mode(enabled)
        else:
            await self.coordinator.async_set_typeconfig_by_key(
                self.entity_description.field, enabled
            )
        LOGGER.info(
            "Switch SET %s = %s for %s",
            self.entity_description.field, enabled, self.coordinator.mac,
        )
