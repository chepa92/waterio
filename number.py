"""Water.io number platform – writable numeric settings via TypeConfig (0x4D)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, LOGGER,
    FIELD_MANUFACTURER, FIELD_HARDWARE, FIELD_FIRMWARE, FIELD_SERIAL,
    FIELD_DAILY_GOAL_ML, FIELD_EXTRA_GOAL_ML,
    FIELD_WORK_START, FIELD_WORK_END, FIELD_TZ_OFFSET,
    FIELD_REMINDER_INTERVAL, FIELD_REMINDER_ROUND, FIELD_REMINDER_CYCLES,
    FIELD_MAR, FIELD_MBR, FIELD_IGR,
)
from .waterio import WaterioCoordinator


@dataclass(frozen=True, kw_only=True)
class WaterioNumberDescription(NumberEntityDescription):
    """Extends NumberEntityDescription with waterio-specific metadata."""
    field: str = ""
    # If True, uses coordinator.async_set_extra_goal() instead of TypeConfig
    use_extra_goal_cmd: bool = False
    enabled_default: bool = True


NUMBER_DESCRIPTIONS: tuple[WaterioNumberDescription, ...] = (
    # ── Hydration goals ───────────────────────────────────────────────────
    WaterioNumberDescription(
        key="daily_goal_ml",
        field=FIELD_DAILY_GOAL_ML,
        name="Daily Hydration Goal",
        native_min_value=500,
        native_max_value=5000,
        native_step=50,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=NumberDeviceClass.VOLUME,
        mode=NumberMode.BOX,
        icon="mdi:flag-checkered",
    ),
    WaterioNumberDescription(
        key="extra_goal_ml",
        field=FIELD_EXTRA_GOAL_ML,
        name="Extra Daily Goal",
        native_min_value=0,
        native_max_value=2000,
        native_step=50,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=NumberDeviceClass.VOLUME,
        mode=NumberMode.BOX,
        icon="mdi:flag-plus",
        use_extra_goal_cmd=True,
        enabled_default=True,
    ),
    # ── Schedule ──────────────────────────────────────────────────────────
    WaterioNumberDescription(
        key="working_hours_start",
        field=FIELD_WORK_START,
        name="Active Hours Start",
        native_min_value=0,
        native_max_value=23,
        native_step=1,
        native_unit_of_measurement="h",
        mode=NumberMode.BOX,
        icon="mdi:clock-start",
    ),
    WaterioNumberDescription(
        key="working_hours_end",
        field=FIELD_WORK_END,
        name="Active Hours End",
        native_min_value=0,
        native_max_value=23,
        native_step=1,
        native_unit_of_measurement="h",
        mode=NumberMode.BOX,
        icon="mdi:clock-end",
    ),
    WaterioNumberDescription(
        key="timezone_offset_min",
        field=FIELD_TZ_OFFSET,
        name="Timezone Offset",
        native_min_value=-720,
        native_max_value=840,
        native_step=30,
        native_unit_of_measurement="min",
        mode=NumberMode.BOX,
        icon="mdi:clock-time-eight",
    ),
    # ── Reminders ─────────────────────────────────────────────────────────
    WaterioNumberDescription(
        key="reminder_interval_min",
        field=FIELD_REMINDER_INTERVAL,
        name="Reminder Interval",
        native_min_value=15,
        native_max_value=480,
        native_step=15,
        native_unit_of_measurement="min",
        mode=NumberMode.BOX,
        icon="mdi:bell-ring",
    ),
    WaterioNumberDescription(
        key="reminder_round_min",
        field=FIELD_REMINDER_ROUND,
        name="Reminder Round Duration",
        native_min_value=1,
        native_max_value=120,
        native_step=5,
        native_unit_of_measurement="min",
        mode=NumberMode.BOX,
        icon="mdi:bell-sleep",
    ),
    WaterioNumberDescription(
        key="reminder_cycles",
        field=FIELD_REMINDER_CYCLES,
        name="Reminder Cycles",
        native_min_value=1,
        native_max_value=20,
        native_step=1,
        native_unit_of_measurement="cycles",
        mode=NumberMode.BOX,
        icon="mdi:reload",
    ),
    # ── DAR / DBR thresholds ──────────────────────────────────────────────
    WaterioNumberDescription(
        key="mar_after_refill_ml",
        field=FIELD_MAR,
        name="MAR Threshold (after refill)",
        native_min_value=0,
        native_max_value=2000,
        native_step=10,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=NumberDeviceClass.VOLUME,
        mode=NumberMode.BOX,
        icon="mdi:water-plus",
    ),
    WaterioNumberDescription(
        key="mbr_before_refill_ml",
        field=FIELD_MBR,
        name="MBR Threshold (before refill)",
        native_min_value=0,
        native_max_value=2000,
        native_step=10,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=NumberDeviceClass.VOLUME,
        mode=NumberMode.BOX,
        icon="mdi:water-minus",
    ),
    WaterioNumberDescription(
        key="igr_refill_gap_ml",
        field=FIELD_IGR,
        name="IGR Refill Gap",
        native_min_value=0,
        native_max_value=250,
        native_step=5,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        device_class=NumberDeviceClass.VOLUME,
        mode=NumberMode.BOX,
        icon="mdi:water-sync",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Water.io number entities from a config entry."""
    coordinator: WaterioCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WaterioNumber(coordinator, entry, desc)
        for desc in NUMBER_DESCRIPTIONS
    )


class WaterioNumber(CoordinatorEntity[WaterioCoordinator], NumberEntity):
    """A writable numeric setting entity for a Water.io cap."""

    entity_description: WaterioNumberDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WaterioCoordinator,
        entry: ConfigEntry,
        description: WaterioNumberDescription,
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
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self.entity_description.field)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Handle user setting a new value."""
        int_value = int(value)
        field = self.entity_description.field

        if self.entity_description.use_extra_goal_cmd:
            await self.coordinator.async_set_extra_goal(int_value)
        elif field == FIELD_DAILY_GOAL_ML:
            # Daily goal is managed in HA only — TypeConfig type 3 corrupts
            # the device's goal index on pv=12 firmware.  Persist in options.
            self.coordinator.data[field] = int_value
            self.coordinator.async_set_updated_data(dict(self.coordinator.data))
            new_opts = dict(self.coordinator.config_entry.options)
            new_opts[field] = int_value
            self.coordinator.hass.config_entries.async_update_entry(
                self.coordinator.config_entry, options=new_opts
            )
        else:
            await self.coordinator.async_set_typeconfig_by_key(field, int_value)
        LOGGER.info(
            "Number SET %s = %s for %s",
            field, int_value, self.coordinator.mac,
        )
